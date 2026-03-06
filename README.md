# Gmail Helper

---

## ⚠️ BIG FAT WARNING ⚠️

> **THIS TOOL CAN PERMANENTLY DELETE EMAILS.**
> **DELETED EMAILS ARE GONE FOREVER AND CANNOT BE RECOVERED.**
> **USE AT YOUR OWN RISK. ALWAYS DOUBLE-CHECK BEFORE CONFIRMING ANY DELETE ACTION.**

---

A local Python CLI to manage your Gmail inbox: analyze senders, bulk delete, trash, and mark emails read/unread.

## Features

- **Sender analysis** - Scan emails, rank senders by count, then act on them (delete / trash / mark read / view emails). Results are cached locally so large scans don't need to be repeated.
- **Multi-select** - Select multiple senders at once using `1,3,5` or ranges like `2-6`, then apply one action to all of them
- **Smart tags** - Each sender is automatically tagged:
  - `newsletter` — has `List-Unsubscribe`/`List-Id` headers or is in Gmail's Promotions/Updates/Social category
  - `important` — at least one email was marked important by Gmail's ML
- **Email viewer** - Browse a sender's emails (subject + from) with pagination, and filter to important-only before deciding to delete
- **Search & bulk action** - Use any Gmail search query, then delete / trash / mark read
- **Resume support** - Large scans are checkpointed every 200 emails. If interrupted, resume from where you left off
- **Label listing** - View all your Gmail labels and their IDs
- **Inbox stats** - Total messages, threads, and email address
- **View / clear sender cache** - Browse cached results or force a fresh scan

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

## Menu options

| # | Option |
|---|--------|
| 1 | Analyze senders — scan inbox, rank by email count, act on each sender |
| 2 | Search & bulk action — Gmail query → delete / trash / mark read |
| 3 | List all labels |
| 4 | Inbox stats |
| 5 | View sender cache — browse cached results and act without re-scanning |
| 6 | Clear sender cache — force a fresh scan next time |
| 7 | Contact / Feedback — opens the GitHub repository |
| 8 | Support me — opens Ko-fi page |

### Sender list navigation

`[#]` select sender(s) · `[m]` next page · `[b]` back to top · `[0]` go back

**Multi-select examples:** `1` · `1,3,5` · `2-6` · `1,3,7-10`
Spaces after commas are fine: `1, 3, 5` works too.
"view" is only available when selecting a single sender.

### Email viewer navigation (after selecting a sender → view)
`[n]` next page · `[p]` prev page · `[i]` toggle important-only filter · `[q]` back to sender list

## Files

| File | Description |
|------|-------------|
| `credentials.json` | OAuth client secrets (you provide this — do not commit) |
| `token.json` | Saved auth token (auto-generated — do not commit) |
| `sender_cache.json` | Cached sender scan results (auto-generated — do not commit) |
| `api_errors.log` | API error log — 429s, 5xx, network failures (auto-generated — do not commit) |
| `auth.py` | OAuth2 login flow |
| `gmail_helper.py` | Main interactive script |
| `requirements.txt` | Python dependencies |
| `setup.sh` | Virtual env setup script |

## Performance

Sender analysis uses **8 parallel HTTP workers** with thread-local `AuthorizedSession` connections, fetching ~150 emails/second while staying within Gmail API quota limits.

- Retries automatically on 429 (rate limit), 5xx (server errors), and network exceptions — with exponential backoff up to 6 attempts
- All API failures are logged to `api_errors.log` for inspection
- Scan results are checkpointed every 200 emails to `sender_cache.json`. If interrupted, resume from where you left off on the next run. Use menu option **6** to clear the cache and force a fresh scan.

## Security notes

- This script uses **OAuth 2.0** — your password is never stored or sent to this script.
- `credentials.json` and `token.json` give access to your Gmail. They are listed in `.gitignore`.
- The Gmail API scope used: `https://mail.google.com/` — required for permanent email deletion. No send or compose permissions are used.

## Support

If this tool helped you, consider supporting development on [Ko-fi](https://ko-fi.com/crakage). Feedback and issues welcome on [GitHub](https://github.com/littlecrakage/gmail-cleaning-helper).
