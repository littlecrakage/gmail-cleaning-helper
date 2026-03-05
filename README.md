# Gmail Helper

A local Python CLI to manage your Gmail inbox: analyze senders, bulk delete, trash, and mark emails read/unread.

## Features

- **Sender analysis** - See how many emails each sender has sent, then act on them
- **Search & bulk action** - Use any Gmail search query, then delete / trash / mark read
- **Label listing** - View all your Gmail labels and their IDs
- **Inbox stats** - Total messages, threads, and email address

## Setup

### 1. Create Google Cloud credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or select an existing one)
3. Enable the **Gmail API**: APIs & Services > Library > search "Gmail API" > Enable
4. Create credentials: APIs & Services > Credentials > Create Credentials > **OAuth 2.0 Client ID**
   - Application type: **Desktop app**
5. Download the JSON file and save it as `credentials.json` in this directory
6. Add your Gmail address as a **Test User**: APIs & Services > OAuth consent screen > Test users

### 2. Run setup

```bash
bash setup.sh
```

This creates a `venv/` virtual environment and installs all dependencies.

### 3. Run

```bash
source venv/Scripts/activate   # Windows (Git Bash / WSL)
# or
source venv/bin/activate        # macOS / Linux

python gmail_helper.py
```

On first run, a browser window will open asking you to authorize the app. After that, a `token.json` is saved locally and reused automatically.

## Files

| File | Description |
|------|-------------|
| `credentials.json` | OAuth client secrets (you provide this — do not commit) |
| `token.json` | Saved auth token (auto-generated — do not commit) |
| `auth.py` | OAuth2 login flow |
| `gmail_helper.py` | Main interactive script |
| `requirements.txt` | Python dependencies |
| `setup.sh` | Virtual env setup script |

## Security notes

- This script uses **OAuth 2.0** — your password is never stored or sent to this script.
- `credentials.json` and `token.json` give access to your Gmail. Add them to `.gitignore` if you use git.
- The Gmail API scopes used: `gmail.readonly` + `gmail.modify` (no send/compose permissions).
