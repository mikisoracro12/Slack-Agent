# Auto Delete Messages (Slack bot)

Python Slack bot built with **Bolt for Python** and **Socket Mode**. It watches every channel it is invited to and deletes messages **1 hour** after they are posted — unless the message starts with `!!!` (those are kept forever).

It also auto-deletes the installing admin's own **DM / MPIM** messages (via user event subscriptions on the `xoxp-` token).

Deletion uses a workspace **admin user token** (`xoxp-`), because a bot token cannot delete other users' messages in channels, and only the message owner can delete in DMs.

## How it works

| Message | Result |
| --- | --- |
| Starts with `!!!` (after leading whitespace) | Kept forever |
| Channel / private-channel human message (including thread replies) | Deleted after `DELETE_AFTER_SECONDS` (default 3600) |
| DM / MPIM message from the admin user token owner | Deleted after the same delay |
| DM / MPIM message from anyone else | Ignored (user token cannot delete them) |
| Bot messages, edits, joins, deletes, other subtypes | Ignored |

Pending deletions are stored in local SQLite (`pending.db`) so they survive restarts. A background thread checks every 60 seconds and also runs immediately on startup for overdue rows.

---

## 1. Create the Slack app from the manifest

1. Open [https://api.slack.com/apps](https://api.slack.com/apps) while signed into the target workspace.
2. Click **Create New App** → **From an app manifest**.
3. Pick your workspace → **Next**.
4. Paste the contents of [`manifest.json`](manifest.json) (or upload the file) → **Next** → **Create**.
5. Confirm the bot and user scopes look correct, then finish creation.

### What the manifest enables

- **Socket Mode** (no public URL / ngrok required)
- **Bot scopes:** `channels:history`, `groups:history`, `chat:write`
- **User scopes:** `chat:write`, `channels:history`, `groups:history`, `im:history`, `mpim:history`
- **Bot events:** `message.channels`, `message.groups`
- **User events:** `message.im`, `message.mpim`

If you already installed an older version of the app, update the manifest in the Slack app settings (or re-paste scopes/events), then **Reinstall to Workspace** so the new user scopes and events take effect. Copy the refreshed `xoxp-` token into `.env` if Slack rotates it.

---

## 2. Install the app and collect tokens

### Install to the workspace

1. In the app settings, open **OAuth & Permissions**.
2. Click **Install to Workspace** (or **Reinstall to Workspace** if you change scopes later).
3. Approve the bot **and** user scopes. The installing account should be a **workspace admin/owner** so `chat.delete` can remove other people's messages.

### `SLACK_BOT_TOKEN` (`xoxb-`)

1. **OAuth & Permissions**
2. Under **Bot User OAuth Token**, copy the `xoxb-...` value.

### `SLACK_ADMIN_USER_TOKEN` (`xoxp-`)

1. Same page: **OAuth & Permissions**
2. Under **User OAuth Token**, copy the `xoxp-...` value.
3. This must belong to an admin who can delete others' messages in the channels you care about.

### `SLACK_APP_TOKEN` (`xapp-`) — Socket Mode

1. Open **Basic Information**.
2. Scroll to **App-Level Tokens** → **Generate Token and Scopes**.
3. Name it (e.g. `socket`) and add scope **`connections:write`**.
4. Generate and copy the `xapp-...` token.

### Enable Socket Mode (if needed)

1. **Socket Mode** in the left sidebar.
2. Ensure Socket Mode is **On** (the manifest already sets this).

---

## 3. Local setup (Windows PowerShell)

Open PowerShell in this project folder:

```powershell
cd "D:\work miki\Slack App"
```

### Create and activate a virtual environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks the activate script:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
.\.venv\Scripts\Activate.ps1
```

### Install dependencies

```powershell
pip install -r requirements.txt
```

### Create your `.env` file

```powershell
Copy-Item .env.example .env
notepad .env
```

Fill in the three tokens. For a quick test, set a short delay:

```env
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_ADMIN_USER_TOKEN=xoxp-...
DELETE_AFTER_SECONDS=60
```

When you are ready for production behavior, set `DELETE_AFTER_SECONDS=3600` (1 hour).

---

## 4. Invite the bot to channels

The bot only receives messages from channels it has joined.

In each public or private channel:

```text
/invite @Auto Delete
```

(Use the bot display name from the Slack app if you renamed it.)

Private channels also need the admin user who authorized the user token to be a member if Slack requires that for deletion.

---

## 5. Run the bot

With the venv activated:

```powershell
python app.py
```

You should see a startup log with `DELETE_AFTER_SECONDS` and the path to `pending.db`. Leave this process running.

### Quick test

1. Set `DELETE_AFTER_SECONDS=60` and restart the bot.
2. Post a normal message in a channel where the bot is present → after ~60s it should disappear; the console logs the schedule and the delete.
3. Post `!!! keep this` → it should never be deleted.

---

## 6. Ops notes

- **`pending.db`** is created next to `app.py`. It is gitignored. Do not delete it while the bot is running if you care about in-flight schedules.
- On restart, overdue rows are processed immediately, then every 60 seconds.
- If Slack returns `message_not_found`, the row is dropped (message already gone).
- If Slack returns `ratelimited`, the row stays and is retried on a later tick.
- Other `chat.delete` errors are logged; the process keeps running.

---

## Project layout

```text
app.py              # Bolt app, SQLite store, deletion scheduler
requirements.txt    # slack_bolt, slack_sdk, python-dotenv
manifest.json       # Slack app manifest (import in api.slack.com)
.env.example        # Token / config template
.gitignore          # Ignores .env, pending.db, venv, etc.
README.md           # This file
```
