"""
Slack bot (Bolt + Socket Mode) that auto-deletes channel and DM messages after
a configurable delay, unless the message text starts with "!!!".
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
SLACK_ADMIN_USER_TOKEN = os.environ["SLACK_ADMIN_USER_TOKEN"]
DELETE_AFTER_SECONDS = int(os.environ.get("DELETE_AFTER_SECONDS", "3600"))
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pending.db")
SCHEDULER_INTERVAL_SECONDS = 60

# DM / multi-party IM channel types delivered via user event subscriptions.
DM_CHANNEL_TYPES = frozenset({"im", "mpim"})

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Bot listens with the bot token; deletions use a workspace admin user token.
app = App(token=SLACK_BOT_TOKEN)
admin_client = WebClient(token=SLACK_ADMIN_USER_TOKEN)

# Resolve once: in DMs the user token can only delete this user's own messages.
_auth = admin_client.auth_test()
ADMIN_USER_ID: str = _auth["user_id"]
logger.info(
    "Admin user token owner user_id=%s team=%s",
    ADMIN_USER_ID,
    _auth.get("team"),
)

_db_lock = threading.Lock()


# ---------------------------------------------------------------------------
# SQLite storage for pending deletions (survives restarts)
# ---------------------------------------------------------------------------
def init_db() -> None:
    """Create the pending_deletions table if it does not exist."""
    with _db_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_deletions (
                    channel_id TEXT NOT NULL,
                    message_ts TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    delete_at REAL NOT NULL,
                    PRIMARY KEY (channel_id, message_ts)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_pending_delete_at
                ON pending_deletions (delete_at)
                """
            )
            conn.commit()
        finally:
            conn.close()


def schedule_deletion(
    channel_id: str, message_ts: str, user_id: str, delete_at: float
) -> None:
    """Insert or replace a pending deletion row."""
    with _db_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO pending_deletions
                    (channel_id, message_ts, user_id, delete_at)
                VALUES (?, ?, ?, ?)
                """,
                (channel_id, message_ts, user_id, delete_at),
            )
            conn.commit()
        finally:
            conn.close()


def fetch_due_deletions(now: float) -> list[tuple[str, str, str, float]]:
    """Return rows whose delete_at is at or before now."""
    with _db_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            cursor = conn.execute(
                """
                SELECT channel_id, message_ts, user_id, delete_at
                FROM pending_deletions
                WHERE delete_at <= ?
                ORDER BY delete_at ASC
                """,
                (now,),
            )
            return cursor.fetchall()
        finally:
            conn.close()


def remove_pending(channel_id: str, message_ts: str) -> None:
    """Remove a pending deletion after success or when the message is gone."""
    with _db_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute(
                """
                DELETE FROM pending_deletions
                WHERE channel_id = ? AND message_ts = ?
                """,
                (channel_id, message_ts),
            )
            conn.commit()
        finally:
            conn.close()


def format_ts(epoch: float) -> str:
    """Human-readable UTC timestamp for logs."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


# ---------------------------------------------------------------------------
# Deletion worker
# ---------------------------------------------------------------------------
def delete_message(channel_id: str, message_ts: str) -> str:
    """
    Attempt chat.delete with the admin user token.

    Returns:
        "ok"              – deleted successfully
        "message_not_found" – already gone; safe to drop the row
        "ratelimited"     – leave the row and retry on a later tick
        "error:<code>"    – other Slack API error; leave the row
    """
    try:
        admin_client.chat_delete(channel=channel_id, ts=message_ts)
        return "ok"
    except SlackApiError as exc:
        error = (exc.response or {}).get("error", "unknown")
        if error == "message_not_found":
            return "message_not_found"
        if error == "ratelimited":
            return "ratelimited"
        logger.error(
            "chat.delete failed channel=%s ts=%s error=%s",
            channel_id,
            message_ts,
            error,
        )
        return f"error:{error}"


def process_due_deletions() -> None:
    """Delete every message whose scheduled time has passed."""
    now = time.time()
    due = fetch_due_deletions(now)
    if not due:
        return

    logger.info("Processing %d due deletion(s)", len(due))
    for channel_id, message_ts, user_id, delete_at in due:
        result = delete_message(channel_id, message_ts)
        if result == "ok":
            remove_pending(channel_id, message_ts)
            logger.info(
                "Deleted message channel=%s ts=%s user=%s (scheduled %s)",
                channel_id,
                message_ts,
                user_id,
                format_ts(delete_at),
            )
        elif result == "message_not_found":
            remove_pending(channel_id, message_ts)
            logger.info(
                "Message already gone; removed row channel=%s ts=%s",
                channel_id,
                message_ts,
            )
        elif result == "ratelimited":
            logger.warning(
                "Rate limited; will retry later channel=%s ts=%s",
                channel_id,
                message_ts,
            )
            # Stop this tick so we do not burn through more rate limit.
            break
        else:
            # Leave the row for a later attempt; already logged above.
            pass


def scheduler_loop() -> None:
    """Background loop: check for due deletions every 60 seconds."""
    while True:
        try:
            process_due_deletions()
        except Exception:
            logger.exception("Unexpected error in deletion scheduler")
        time.sleep(SCHEDULER_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# Message listener
# ---------------------------------------------------------------------------
@app.event("message")
def handle_message(event: dict, logger: logging.Logger) -> None:
    """
    Schedule deletion for human messages that do not start with "!!!".

    Channels/groups (bot events): any human message is scheduled.
    DMs / MPIMs (user events message.im / message.mpim): only messages from
    ADMIN_USER_ID are scheduled, since the user token can only delete those.

    Ignores bots, any message with a subtype (edits/joins/deletes/etc.),
    and keep-forever messages. Thread replies are treated like normal messages.
    """
    # Bots (including this app) — skip
    if event.get("bot_id") or event.get("bot_profile"):
        return

    # Subtypes: message_changed, message_deleted, channel_join, etc.
    if event.get("subtype"):
        return

    text = (event.get("text") or "").lstrip()
    if text.startswith("!!!"):
        logger.debug(
            "Keeping forever (!!! prefix) channel=%s ts=%s",
            event.get("channel"),
            event.get("ts"),
        )
        return

    channel_id = event["channel"]
    message_ts = event["ts"]
    user_id = event.get("user") or ""
    channel_type = event.get("channel_type") or ""

    # User-token DM events: only the admin user's own messages are deletable.
    if channel_type in DM_CHANNEL_TYPES and user_id != ADMIN_USER_ID:
        logger.debug(
            "Skipping DM/MPIM from other user channel=%s type=%s user=%s",
            channel_id,
            channel_type,
            user_id,
        )
        return

    # Slack message ts is an epoch float as a string (e.g. "1710000000.000100").
    posted_at = float(message_ts)
    delete_at = posted_at + DELETE_AFTER_SECONDS

    schedule_deletion(channel_id, message_ts, user_id, delete_at)
    logger.info(
        "Scheduled deletion channel=%s type=%s ts=%s user=%s delete_at=%s (in %ss)",
        channel_id,
        channel_type or "unknown",
        message_ts,
        user_id,
        format_ts(delete_at),
        DELETE_AFTER_SECONDS,
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
def main() -> None:
    init_db()
    logger.info(
        "Starting bot; DELETE_AFTER_SECONDS=%s, admin_user=%s, db=%s",
        DELETE_AFTER_SECONDS,
        ADMIN_USER_ID,
        DB_PATH,
    )

    # Drain anything overdue from a previous run before accepting new events.
    process_due_deletions()

    worker = threading.Thread(
        target=scheduler_loop,
        name="deletion-scheduler",
        daemon=True,
    )
    worker.start()

    SocketModeHandler(app, SLACK_APP_TOKEN).start()


if __name__ == "__main__":
    main()
