"""
Slack bot (Bolt for Python, HTTP mode) with a /delete slash command.

/delete XX deletes the invoking user's own messages from the last XX hours
in the current conversation (channel, private channel, DM, or group DM),
including their thread replies. Other people's messages are never deleted.

Each user authorizes once. Their user token is stored in SQLite by Bolt's
OAuth installation store and used for conversations.history, conversations.replies,
and chat.delete.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import uuid
from typing import Any, Callable, Optional

from dotenv import load_dotenv
from flask import Flask, request
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from slack_bolt.oauth.oauth_settings import OAuthSettings
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.oauth.installation_store.sqlite3 import SQLite3InstallationStore
from slack_sdk.oauth.state_store.sqlite3 import SQLite3OAuthStateStore

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = 3000

BOT_SCOPES = ["commands", "chat:write"]
USER_SCOPES = [
    "chat:write",
    "channels:history",
    "groups:history",
    "im:history",
    "mpim:history",
]

# Slack system events that are not user-authored messages.
SYSTEM_SUBTYPES = frozenset(
    {
        "bot_add",
        "bot_message",
        "bot_remove",
        "channel_archive",
        "channel_join",
        "channel_leave",
        "channel_name",
        "channel_posting_permissions",
        "channel_purpose",
        "channel_topic",
        "channel_unarchive",
        "ekm_access_denied",
        "group_archive",
        "group_join",
        "group_leave",
        "group_name",
        "group_purpose",
        "group_topic",
        "group_unarchive",
        "message_changed",
        "message_deleted",
        "pinned_item",
        "reminder_add",
        "unpinned_item",
    }
)

# Tokens Slack reports when the stored user token can no longer be used.
AUTH_ERRORS = frozenset({"invalid_auth", "token_revoked"})

# XX must be a positive number of hours, at most 30 days.
MAX_HOURS = 720
HOURS_PATTERN = re.compile(r"^(\d+(\.\d+)?|\.\d+)$")

# Confirmations live in memory (button value is only the id; message lists
# can be far larger than Slack's 2000-character button value limit).
PENDING_TTL_SECONDS = 30 * 60

USAGE_TEXT = (
    "Usage: `/delete XX`, where XX is a number of hours greater than 0 and "
    "at most 720. Decimals are allowed, for example `/delete 0.5`."
)


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


SLACK_CLIENT_ID = require_env("SLACK_CLIENT_ID")
SLACK_CLIENT_SECRET = require_env("SLACK_CLIENT_SECRET")
SLACK_SIGNING_SECRET = require_env("SLACK_SIGNING_SECRET")
# Absolute origin of this server (ngrok URL in development). No trailing slash.
PUBLIC_BASE_URL = require_env("PUBLIC_BASE_URL").rstrip("/")
INSTALL_URL = f"{PUBLIC_BASE_URL}/slack/install"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


class UserAuthorizationError(Exception):
    """Raised when the stored user token is no longer accepted by Slack."""

    def __init__(self, error: str, deleted: int = 0) -> None:
        super().__init__(error)
        self.error = error
        self.deleted = deleted


# ---------------------------------------------------------------------------
# OAuth: one user token per authorizing user, stored in SQLite
# ---------------------------------------------------------------------------
installation_store = SQLite3InstallationStore(
    database=os.path.join(BASE_DIR, "installations.db"),
    client_id=SLACK_CLIENT_ID,
)
state_store = SQLite3OAuthStateStore(
    database=os.path.join(BASE_DIR, "oauth_states.db"),
    expiration_seconds=600,
)

oauth_settings = OAuthSettings(
    client_id=SLACK_CLIENT_ID,
    client_secret=SLACK_CLIENT_SECRET,
    scopes=BOT_SCOPES,
    user_scopes=USER_SCOPES,
    installation_store=installation_store,
    state_store=state_store,
    install_path="/slack/install",
    redirect_uri_path="/slack/oauth_redirect",
    redirect_uri=f"{PUBLIC_BASE_URL}/slack/oauth_redirect",
    # Keep per-user rows so each member's user token is saved.
    installation_store_bot_only=False,
)

app = App(
    signing_secret=SLACK_SIGNING_SECRET,
    oauth_settings=oauth_settings,
)


# ---------------------------------------------------------------------------
# Pending confirmation store
# ---------------------------------------------------------------------------
class PendingRemovals:
    """In-memory map of confirmation id -> messages waiting for Delete/Cancel."""

    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def put(self, payload: dict[str, Any]) -> str:
        pending_id = uuid.uuid4().hex
        stored = dict(payload)
        stored["created_at"] = time.time()
        with self._lock:
            self._purge_locked()
            self._items[pending_id] = stored
        return pending_id

    def pop_for_user(self, pending_id: str, user_id: str) -> tuple[str, Optional[dict[str, Any]]]:
        """
        Remove a pending request if it belongs to user_id.

        Returns ("ok", payload), ("missing", None), or ("forbidden", None).
        """
        with self._lock:
            self._purge_locked()
            item = self._items.get(pending_id)
            if item is None:
                return "missing", None
            if item.get("user_id") != user_id:
                return "forbidden", None
            return "ok", self._items.pop(pending_id)

    def _purge_locked(self) -> None:
        cutoff = time.time() - PENDING_TTL_SECONDS
        expired = [
            pending_id
            for pending_id, item in self._items.items()
            if item.get("created_at", 0) < cutoff
        ]
        for pending_id in expired:
            del self._items[pending_id]


pending_removals = PendingRemovals()


# ---------------------------------------------------------------------------
# Slack helpers
# ---------------------------------------------------------------------------
def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


def parse_hours(text: str) -> Optional[float]:
    """Return hours if text is a number in (0, 720], otherwise None."""
    raw = (text or "").strip()
    if not HOURS_PATTERN.match(raw):
        return None
    hours = float(raw)
    if hours <= 0 or hours > MAX_HOURS:
        return None
    return hours


def format_hours(hours: float) -> str:
    """Render 3.0 as 3 and 0.5 as 0.5."""
    if hours == int(hours):
        return str(int(hours))
    return f"{hours:g}"


def retry_after_seconds(error: SlackApiError) -> int:
    """Read Slack's Retry-After header, defaulting to 1 second."""
    headers = getattr(error.response, "headers", None) or {}
    raw = headers.get("Retry-After", headers.get("retry-after", "1"))
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 1


def api_call(method: Callable[..., Any], payload: dict[str, Any], what: str) -> Any:
    """Call a Web API method, sleeping through rate limits."""
    while True:
        try:
            return method(**payload)
        except SlackApiError as exc:
            error = (exc.response or {}).get("error", "unknown")
            if error == "ratelimited":
                wait = retry_after_seconds(exc)
                logger.warning("%s rate limited; retrying in %ss", what, wait)
                time.sleep(wait)
                continue
            if error in AUTH_ERRORS:
                raise UserAuthorizationError(error) from exc
            raise


def install_context_from_command(command: dict[str, Any]) -> dict[str, Any]:
    return {
        "user_id": command["user_id"],
        "team_id": command.get("team_id"),
        "enterprise_id": command.get("enterprise_id"),
        "is_enterprise_install": as_bool(command.get("is_enterprise_install")),
        "channel_id": command["channel_id"],
    }


def user_client(ctx: dict[str, Any]) -> Optional[WebClient]:
    """Return a WebClient for this user's token, or None if they never authorized."""
    installation = installation_store.find_installation(
        enterprise_id=ctx.get("enterprise_id"),
        team_id=ctx.get("team_id"),
        user_id=ctx["user_id"],
        is_enterprise_install=ctx.get("is_enterprise_install", False),
    )
    if installation is None or not installation.user_token:
        return None
    return WebClient(token=installation.user_token)


def forget_installation(ctx: dict[str, Any]) -> None:
    """Drop the stored installation after Slack rejects the user token."""
    team_id = ctx.get("team_id")
    if ctx.get("is_enterprise_install") or not team_id:
        # Enterprise installs are stored with an empty team id.
        team_id = ""
    installation_store.delete_installation(
        enterprise_id=ctx.get("enterprise_id"),
        team_id=team_id,
        user_id=ctx["user_id"],
    )
    logger.info(
        "Removed stored installation user=%s team=%s enterprise=%s",
        ctx.get("user_id"),
        ctx.get("team_id"),
        ctx.get("enterprise_id"),
    )


def authorize_blocks(intro: str) -> list[dict[str, Any]]:
    """Ephemeral blocks that send the user to /slack/install."""
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": intro},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Authorize"},
                    "url": INSTALL_URL,
                    "action_id": "authorize_link",
                }
            ],
        },
    ]


def replace_message(respond: Callable[..., Any], text: str) -> None:
    """Replace the ephemeral confirmation (clears the buttons)."""
    respond(
        text=text,
        response_type="ephemeral",
        replace_original=True,
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
    )


def next_cursor(result: Any) -> str:
    metadata = result.get("response_metadata") or {}
    return metadata.get("next_cursor") or ""


def consider_message(
    message: dict[str, Any],
    user_id: str,
    oldest: float,
    found: dict[str, None],
) -> None:
    """Keep the message ts when it was posted by user_id inside the window."""
    ts = message.get("ts")
    if not ts or ts in found:
        return
    subtype = message.get("subtype")
    if subtype in SYSTEM_SUBTYPES:
        return
    if message.get("user") != user_id:
        return
    try:
        posted_at = float(ts)
    except (TypeError, ValueError):
        return
    # Window is [now - XX hours, now]. Slack's oldest filter is "after", so
    # the boundary is also checked here.
    if posted_at < oldest:
        return
    found[ts] = None


def collect_own_messages(
    client: WebClient, channel_id: str, user_id: str, oldest: float
) -> list[str]:
    """
    User's message timestamps in channel_id from `oldest` through now.

    conversations.history does not include thread replies, so every parent
    returned in that window that has replies is also expanded with
    conversations.replies. Results are de-duplicated by ts.
    """
    oldest_str = f"{oldest:.6f}"
    found: dict[str, None] = {}
    parents: list[str] = []
    seen_parents: set[str] = set()

    cursor = ""
    while True:
        payload: dict[str, Any] = {
            "channel": channel_id,
            "oldest": oldest_str,
            "limit": 200,
        }
        if cursor:
            payload["cursor"] = cursor
        result = api_call(client.conversations_history, payload, "conversations.history")
        for message in result.get("messages") or []:
            consider_message(message, user_id, oldest, found)
            parent_ts = message.get("ts")
            if parent_ts and message.get("reply_count", 0) and parent_ts not in seen_parents:
                seen_parents.add(parent_ts)
                parents.append(parent_ts)
        cursor = next_cursor(result)
        if not cursor:
            break

    for parent_ts in parents:
        cursor = ""
        while True:
            payload = {
                "channel": channel_id,
                "ts": parent_ts,
                "oldest": oldest_str,
                "limit": 200,
            }
            if cursor:
                payload["cursor"] = cursor
            result = api_call(client.conversations_replies, payload, "conversations.replies")
            for message in result.get("messages") or []:
                consider_message(message, user_id, oldest, found)
            cursor = next_cursor(result)
            if not cursor:
                break

    # Newest first so the conversation shrinks from the bottom.
    return sorted(found, key=float, reverse=True)


def delete_messages(client: WebClient, channel_id: str, timestamps: list[str]) -> tuple[int, int]:
    """
    chat.delete each timestamp with the user's token.

    message_not_found is ignored. ratelimited sleeps for Retry-After and retries.
    Returns (deleted_count, failed_count).
    """
    deleted = 0
    failed = 0
    for ts in timestamps:
        while True:
            try:
                client.chat_delete(channel=channel_id, ts=ts)
                deleted += 1
                logger.info("Deleted message channel=%s ts=%s", channel_id, ts)
                break
            except SlackApiError as exc:
                error = (exc.response or {}).get("error", "unknown")
                if error == "message_not_found":
                    logger.info("Message already gone channel=%s ts=%s", channel_id, ts)
                    break
                if error == "ratelimited":
                    wait = retry_after_seconds(exc)
                    logger.warning(
                        "chat.delete rate limited channel=%s ts=%s; retrying in %ss",
                        channel_id,
                        ts,
                        wait,
                    )
                    time.sleep(wait)
                    continue
                if error in AUTH_ERRORS:
                    raise UserAuthorizationError(error, deleted=deleted) from exc
                failed += 1
                logger.error(
                    "chat.delete failed channel=%s ts=%s error=%s",
                    channel_id,
                    ts,
                    error,
                )
                break
    return deleted, failed


def history_error_text(error: str) -> str:
    if error in {"not_in_channel", "channel_not_found"}:
        return "Slack could not read this conversation with your account. Open it and try `/delete` again."
    if error == "missing_scope":
        return (
            "Your authorization is missing a required permission. "
            f"Open {INSTALL_URL} and authorize again, then retry `/delete`."
        )
    return f"Could not read messages ({error}). Nothing was deleted."


# ---------------------------------------------------------------------------
# /delete
# ---------------------------------------------------------------------------
def ack_command(ack: Callable[..., None]) -> None:
    # Slack must receive this within 3 seconds. The work runs in the lazy listener.
    ack()


def handle_remove(command: dict[str, Any], respond: Callable[..., Any]) -> None:
    ctx = install_context_from_command(command)
    hours = parse_hours(command.get("text") or "")
    if hours is None:
        logger.info(
            "Rejected /delete user=%s channel=%s text=%r",
            ctx["user_id"],
            ctx["channel_id"],
            command.get("text"),
        )
        respond(text=USAGE_TEXT, response_type="ephemeral")
        return

    client = user_client(ctx)
    if client is None:
        logger.info(
            "No user token for user=%s team=%s; asking them to authorize",
            ctx["user_id"],
            ctx["team_id"],
        )
        intro = (
            "Authorize once so this app can delete *your* messages. "
            "After you allow access, run `/delete` again."
        )
        respond(
            text=intro,
            response_type="ephemeral",
            blocks=authorize_blocks(intro),
        )
        return

    # From (now - XX hours) through the current moment.
    now = time.time()
    oldest = now - (hours * 3600)
    hours_label = format_hours(hours)
    logger.info(
        "Scanning /delete user=%s channel=%s hours=%s oldest=%s",
        ctx["user_id"],
        ctx["channel_id"],
        hours_label,
        oldest,
    )

    try:
        timestamps = collect_own_messages(client, ctx["channel_id"], ctx["user_id"], oldest)
    except UserAuthorizationError as exc:
        forget_installation(ctx)
        intro = (
            "Your authorization is no longer valid "
            f"(`{exc.error}`). Authorize again, then run `/delete`."
        )
        respond(text=intro, response_type="ephemeral", blocks=authorize_blocks(intro))
        return
    except SlackApiError as exc:
        error = (exc.response or {}).get("error", "unknown")
        logger.error(
            "History scan failed user=%s channel=%s error=%s",
            ctx["user_id"],
            ctx["channel_id"],
            error,
        )
        respond(text=history_error_text(error), response_type="ephemeral")
        return

    count = len(timestamps)
    logger.info(
        "Found %s message(s) user=%s channel=%s hours=%s",
        count,
        ctx["user_id"],
        ctx["channel_id"],
        hours_label,
    )
    if count == 0:
        respond(
            text=f"No messages from you in the last {hours_label} hours.",
            response_type="ephemeral",
        )
        return

    pending_id = pending_removals.put(
        {
            "user_id": ctx["user_id"],
            "team_id": ctx["team_id"],
            "enterprise_id": ctx["enterprise_id"],
            "is_enterprise_install": ctx["is_enterprise_install"],
            "channel_id": ctx["channel_id"],
            "hours": hours,
            "timestamps": timestamps,
        }
    )
    prompt = f"Found {count} messages from the last {hours_label} hours. Delete them?"
    respond(
        text=prompt,
        response_type="ephemeral",
        blocks=[
            {"type": "section", "text": {"type": "mrkdwn", "text": prompt}},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "style": "danger",
                        "text": {"type": "plain_text", "text": "Delete"},
                        "action_id": "remove_delete",
                        "value": pending_id,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Cancel"},
                        "action_id": "remove_cancel",
                        "value": pending_id,
                    },
                ],
            },
        ],
    )


def ack_action(ack: Callable[..., None]) -> None:
    ack()


def handle_cancel(body: dict[str, Any], respond: Callable[..., Any]) -> None:
    user_id = body["user"]["id"]
    pending_id = body["actions"][0].get("value") or ""
    status, _payload = pending_removals.pop_for_user(pending_id, user_id)
    if status == "forbidden":
        replace_message(respond, "You can only cancel your own `/delete` request.")
        return
    if status == "missing":
        replace_message(respond, "This request expired. Run `/delete` again.")
        return
    logger.info("Cancelled /delete user=%s pending=%s", user_id, pending_id)
    replace_message(respond, "Cancelled.")


def handle_delete(body: dict[str, Any], respond: Callable[..., Any]) -> None:
    user_id = body["user"]["id"]
    pending_id = body["actions"][0].get("value") or ""
    status, payload = pending_removals.pop_for_user(pending_id, user_id)
    if status == "forbidden":
        replace_message(respond, "You can only confirm your own `/delete` request.")
        return
    if status == "missing" or payload is None:
        replace_message(respond, "This request expired. Run `/delete` again.")
        return

    timestamps = list(payload["timestamps"])
    ctx = {
        "user_id": payload["user_id"],
        "team_id": payload.get("team_id"),
        "enterprise_id": payload.get("enterprise_id"),
        "is_enterprise_install": payload.get("is_enterprise_install", False),
        "channel_id": payload["channel_id"],
    }
    replace_message(respond, f"Deleting {len(timestamps)} messages…")

    client = user_client(ctx)
    if client is None:
        intro = f"Your authorization is missing. Open {INSTALL_URL}, authorize, and run `/delete` again."
        replace_message(respond, intro)
        return

    logger.info(
        "Deleting %s message(s) user=%s channel=%s",
        len(timestamps),
        ctx["user_id"],
        ctx["channel_id"],
    )
    try:
        deleted, failed = delete_messages(client, ctx["channel_id"], timestamps)
    except UserAuthorizationError as exc:
        forget_installation(ctx)
        replace_message(
            respond,
            (
                f"Deleted {exc.deleted} messages, then authorization failed (`{exc.error}`). "
                f"Authorize again at {INSTALL_URL} and run `/delete` for anything left."
            ),
        )
        return

    if failed:
        replace_message(respond, f"Deleted {deleted} messages. {failed} could not be deleted.")
        return
    logger.info(
        "Finished /delete user=%s channel=%s deleted=%s",
        ctx["user_id"],
        ctx["channel_id"],
        deleted,
    )
    replace_message(respond, f"Deleted {deleted} messages.")


def handle_authorize_link(ack: Callable[..., None]) -> None:
    # Link buttons still post an action. Acknowledge it and let the URL open.
    ack()


app.command("/delete")(ack=ack_command, lazy=[handle_remove])
app.action("remove_cancel")(ack=ack_action, lazy=[handle_cancel])
app.action("remove_delete")(ack=ack_action, lazy=[handle_delete])
app.action("authorize_link")(handle_authorize_link)


# ---------------------------------------------------------------------------
# HTTP server (slash commands, interactivity, and OAuth share this process)
# ---------------------------------------------------------------------------
flask_app = Flask(__name__)
handler = SlackRequestHandler(app)


@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    return handler.handle(request)


@flask_app.route("/slack/install", methods=["GET"])
def slack_install():
    return handler.handle(request)


@flask_app.route("/slack/oauth_redirect", methods=["GET"])
def slack_oauth_redirect():
    return handler.handle(request)


def main() -> None:
    logger.info("Install URL: %s", INSTALL_URL)
    logger.info("OAuth redirect: %s/slack/oauth_redirect", PUBLIC_BASE_URL)
    logger.info("Listening on http://0.0.0.0:%s", PORT)
    flask_app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
