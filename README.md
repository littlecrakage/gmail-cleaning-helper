# Gmail Helper

---

## ⚠️ BIG FAT WARNING ⚠️

> **THIS TOOL CAN PERMANENTLY DELETE EMAILS.**
> **DELETED EMAILS ARE GONE FOREVER AND CANNOT BE RECOVERED.**
> **USE AT YOUR OWN RISK. ALWAYS DOUBLE-CHECK BEFORE CONFIRMING ANY DELETE ACTION.**

---

A local Python CLI to manage your Gmail inbox: analyze senders, bulk delete, trash, and mark emails read/unread.

## Features

- **Sender analysis** - Scan emails, rank senders by count, then act on them (delete / trash / mark read). Results are cached locally so large scans don't need to be repeated.
- **Search & bulk action** - Use any Gmail search query, then delete / trash / mark read
- **Label listing** - View all your Gmail labels and their IDs
- **Inbox stats** - Total messages, threads, and email address
- **Clear sender cache** - Force a fresh scan on next run

## Setup

### 1. Create Google Cloud credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or select an existing one)
3. Enable the **Gmail API**: APIs & Services > Library > search "Gmail API" > Enable
4. Create credentials: APIs & Services > Credentials > Create Credentials > **OAuth 2.0 Client ID**
   - Application type: **Desktop app** (not Web application — this avoids redirect URI errors)
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
| `sender_cache.json` | Cached sender scan results (auto-generated — do not commit) |
| `auth.py` | OAuth2 login flow |
| `gmail_helper.py` | Main interactive script |
| `requirements.txt` | Python dependencies |
| `setup.sh` | Virtual env setup script |

## Performance

The sender analysis fetches headers using **Gmail API batch requests** (100 messages per HTTP call), making large inbox scans significantly faster than sequential calls.

Scan results are saved to `sender_cache.json`. On the next run with the same limit and filter, you'll be offered to reload from cache instead of re-scanning. Use menu option **5** to clear the cache and force a fresh scan.

## Security notes

- This script uses **OAuth 2.0** — your password is never stored or sent to this script.
- `credentials.json` and `token.json` give access to your Gmail. They are listed in `.gitignore`.
- The Gmail API scope used: `https://mail.google.com/` — required for permanent email deletion. No send or compose permissions are used.
