# Remove Messages (Slack bot)

Python Slack app built with **Bolt for Python**. In any conversation, a user can run:

```text
/delete 3
```

That finds **their own** messages posted in the last 3 hours in **that** channel, private channel, DM, or group DM (including their thread replies) and, after they confirm, deletes them. Messages from other people are left alone.

`/delete 0.5` is half an hour. The number must be greater than 0 and at most 720 (30 days). `/delete 3` at 15:00 covers messages from 12:00 through 15:00.

The app runs as an HTTP server on **port 3000** (not Socket Mode). Each person authorizes once; their user token is stored in local SQLite (`installations.db`) and used to read and delete only their messages.

## 1. Create the Slack app

1. Open [https://api.slack.com/apps](https://api.slack.com/apps).
2. **Create New App** → **From an app manifest** → choose the workspace.
3. Paste [`manifest.json`](manifest.json). The URLs still say `https://YOUR-PUBLIC-URL/...`. You will replace those after ngrok is running (step 4). If Slack refuses to save the manifest because it cannot verify those URLs, create the app with the placeholder, then update the three URLs in the UI in step 4.
4. Create the app.

### Scopes in the manifest

- **Bot:** `commands`, `chat:write`
- **User:** `chat:write`, `channels:history`, `groups:history`, `im:history`, `mpim:history`

Socket Mode is off. There are no event subscriptions. Interactivity and the `/delete` command both post to `/slack/events`.

If you still have the older auto-delete app, do not reuse its bot or user tokens. This version uses OAuth client credentials instead.

## 2. Local setup (Windows PowerShell)

```powershell
cd "D:\work miki\Slack App"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

If activation is blocked:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Fill in `.env` from the app's **Basic Information** page:

| Variable | Where |
| --- | --- |
| `SLACK_CLIENT_ID` | App Credentials → Client ID |
| `SLACK_CLIENT_SECRET` | App Credentials → Client Secret |
| `SLACK_SIGNING_SECRET` | App Credentials → Signing Secret |
| `PUBLIC_BASE_URL` | Your ngrok HTTPS URL, no trailing slash. Set this in step 4, then restart the bot. |

Leave `PUBLIC_BASE_URL` as a placeholder until ngrok is up, but the process will not start until it is a real value. You can start ngrok first, paste the URL into `.env`, then start the bot.

## 3. Run the bot

With the virtual environment activated:

```powershell
python app.py
```

The server listens on `http://0.0.0.0:3000`:

- `POST /slack/events` — slash command and buttons
- `GET /slack/install` — start user authorization
- `GET /slack/oauth_redirect` — OAuth callback

Leave this window open.

## 4. Expose it with ngrok

Install ngrok once ([https://ngrok.com/download](https://ngrok.com/download) or `winget install ngrok.ngrok`), add your authtoken, then in a second PowerShell window:

```powershell
ngrok http 3000
```

Copy the **https** forwarding URL, for example `https://abcd-123.ngrok-free.app`.

1. Put that origin in `.env` as `PUBLIC_BASE_URL` (no path, no trailing slash) and restart `python app.py`.
2. In the Slack app settings, set the same origin on all three URLs:
   - **Slash Commands** → `/delete` → Request URL: `https://abcd-123.ngrok-free.app/slack/events`
   - **Interactivity & Shortcuts** → Request URL: `https://abcd-123.ngrok-free.app/slack/events`
   - **OAuth & Permissions** → Redirect URLs: `https://abcd-123.ngrok-free.app/slack/oauth_redirect`

The bot must be running when you save those URLs so Slack can verify them.

A free ngrok URL changes every time you restart ngrok. Update `.env` and the three Slack URLs when it changes.

## 5. Install the app

1. **OAuth & Permissions** → **Install to Workspace** → **Allow**.
2. That stores a bot token and **your** user token in `installations.db`.
3. You can run `/delete` immediately. No `/invite` is required: the command uses your user token, not the bot's membership.

## 6. Each person authorizes once

Only the person who installed the app has a user token. Anyone else who runs `/delete` gets an ephemeral **Authorize** button pointing at:

```text
https://abcd-123.ngrok-free.app/slack/install
```

They open it while logged into Slack, click **Allow**, then run `/delete` again. Their user token is stored separately. The app can delete only that person's own messages.

If Slack later returns `invalid_auth` or `token_revoked`, the stored installation is deleted and the user is asked to authorize again.

## 7. Use `/delete`

In a channel, private channel, DM, or group DM:

```text
/delete 3
```

1. The app lists your messages in that conversation from the last 3 hours, including replies in threads whose parent message is also inside that window.
2. You get an ephemeral prompt: `Found N messages from the last 3 hours. Delete them?`
3. **Delete** removes them with your user token. **Cancel** leaves them in place.

Messages that start with nothing special are included. This bot no longer keeps or auto-deletes based on a `!!!` prefix.

## Project layout

```text
app.py              # Bolt app, OAuth, /delete, Flask server
requirements.txt    # slack_bolt, slack_sdk, python-dotenv, flask
manifest.json       # Slack app manifest (HTTP mode, /delete)
.env.example        # Client id, secret, signing secret, public URL
.gitignore          # Ignores .env and the SQLite OAuth databases
```

`installations.db` and `oauth_states.db` are created next to `app.py` and are gitignored.
