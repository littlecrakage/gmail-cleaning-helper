"""Gmail Helper - Interactive CLI to manage your Gmail inbox."""

import json
import logging
import random
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from google.auth.transport.requests import AuthorizedSession
from googleapiclient.discovery import build
from rich.console import Console
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

from auth import get_credentials

CACHE_FILE = Path("sender_cache.json")
LOG_FILE = Path("api_errors.log")

console = Console()

# File logger for API errors (thread-safe, does not print to console)
_api_log = logging.getLogger("gmail_helper.api")
_api_log.setLevel(logging.WARNING)
_api_log.propagate = False
_log_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
_log_handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
_api_log.addHandler(_log_handler)


# ---------------------------------------------------------------------------
# Gmail API helpers
# ---------------------------------------------------------------------------

def build_service():
    creds = get_credentials()
    return build("gmail", "v1", credentials=creds), creds


def fetch_messages(service, query: str = "", max_results: int = 500) -> list[dict]:
    """Fetch message stubs matching a query (up to max_results)."""
    messages = []
    page_token = None

    while len(messages) < max_results:
        batch = min(500, max_results - len(messages))
        kwargs = {"userId": "me", "maxResults": batch, "q": query}
        if page_token:
            kwargs["pageToken"] = page_token

        result = service.users().messages().list(**kwargs).execute()
        chunk = result.get("messages", [])
        messages.extend(chunk)

        page_token = result.get("nextPageToken")
        if not page_token or not chunk:
            break

    return messages


_session_local = threading.local()


def _get_session(creds) -> AuthorizedSession:
    """Return a thread-local AuthorizedSession (reused across calls on same thread)."""
    if not hasattr(_session_local, "session"):
        _session_local.session = AuthorizedSession(creds)
    return _session_local.session


_NEWSLETTER_LABELS = {"CATEGORY_PROMOTIONS", "CATEGORY_UPDATES", "CATEGORY_SOCIAL"}


def get_senders_concurrent(creds, msg_ids: list[str], max_workers: int = 8) -> dict[str, tuple[str, set]]:
    """Fetch From headers + tags for messages using parallel HTTP requests.
    Returns {mid: (sender, tags)} where tags is a set of strings like 'newsletter', 'important'."""

    def fetch_one(mid: str) -> tuple[str, str, set]:
        session = _get_session(creds)
        url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{mid}"
        for attempt in range(6):
            wait = (2 ** attempt) + random.uniform(0, 1)
            try:
                resp = session.get(url, params={
                    "format": "metadata",
                    "metadataHeaders": ["From", "List-Unsubscribe", "List-Id"],
                })
                if resp.status_code == 200:
                    data = resp.json()
                    hdrs = {
                        h["name"].lower(): h["value"]
                        for h in data.get("payload", {}).get("headers", [])
                    }
                    sender = hdrs.get("from", "Unknown")
                    tags: set[str] = set()
                    if "list-unsubscribe" in hdrs or "list-id" in hdrs:
                        tags.add("newsletter")
                    label_ids = set(data.get("labelIds", []))
                    if label_ids & _NEWSLETTER_LABELS:
                        tags.add("newsletter")
                    if "IMPORTANT" in label_ids:
                        tags.add("important")
                    return mid, sender, tags
                elif resp.status_code == 429:
                    _api_log.warning(f"429 rate_limit  mid={mid}  attempt={attempt}  wait={wait:.1f}s")
                    time.sleep(wait)
                elif resp.status_code >= 500:
                    _api_log.warning(f"{resp.status_code} server_error  mid={mid}  attempt={attempt}  wait={wait:.1f}s  body={resp.text[:120]!r}")
                    time.sleep(wait)
                else:
                    _api_log.warning(f"{resp.status_code} unexpected  mid={mid}  body={resp.text[:120]!r}")
                    break
            except Exception as exc:
                _api_log.warning(f"exception  mid={mid}  attempt={attempt}  wait={wait:.1f}s  error={exc}")
                time.sleep(wait)
        else:
            _api_log.warning(f"all_retries_exhausted  mid={mid}")
        return mid, "Unknown", set()

    result: dict[str, tuple[str, set]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for mid, sender, tags in executor.map(fetch_one, msg_ids):
            result[mid] = (sender, tags)
    return result


def batch_delete(service, message_ids: list[str]) -> int:
    """Move messages to trash in batches of 1000. Returns count deleted."""
    deleted = 0
    for i in range(0, len(message_ids), 1000):
        chunk = message_ids[i : i + 1000]
        service.users().messages().batchDelete(
            userId="me", body={"ids": chunk}
        ).execute()
        deleted += len(chunk)
    return deleted


def batch_modify(service, message_ids: list[str], add_labels=None, remove_labels=None) -> int:
    """Modify labels on messages in batches of 1000. Returns count modified."""
    body = {}
    if add_labels:
        body["addLabelIds"] = add_labels
    if remove_labels:
        body["removeLabelIds"] = remove_labels

    modified = 0
    for i in range(0, len(message_ids), 1000):
        chunk = message_ids[i : i + 1000]
        service.users().messages().batchModify(
            userId="me", body={**body, "ids": chunk}
        ).execute()
        modified += len(chunk)
    return modified


def get_labels(service) -> list[dict]:
    """Return all labels for the authenticated user."""
    result = service.users().labels().list(userId="me").execute()
    return result.get("labels", [])


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_key(query: str, limit: int) -> str:
    return f"{limit}|{query}"


def _age_str(timestamp: float) -> str:
    age = time.time() - timestamp
    if age < 3600:
        return f"{int(age // 60)}m ago"
    if age < 86400:
        return f"{age / 3600:.1f}h ago"
    return f"{age / 86400:.1f}d ago"


def _load_cache(query: str, limit: int) -> dict | None:
    """Return raw cache dict if key matches, else None."""
    if not CACHE_FILE.exists():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    if data.get("key") != _cache_key(query, limit):
        return None
    return data


def _save_complete_cache(query: str, limit: int, sorted_senders: list, sender_ids: dict, sender_tags: dict):
    data = {
        "key": _cache_key(query, limit),
        "partial": False,
        "timestamp": time.time(),
        "query": query,
        "limit": limit,
        "sorted_senders": sorted_senders,
        "sender_ids": sender_ids,
        "sender_tags": {k: list(v) for k, v in sender_tags.items()},
    }
    CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _save_checkpoint(
    query: str, limit: int,
    all_ids: list, processed: int,
    sender_counts: dict, sender_ids: dict, sender_tags: dict,
):
    """Save mid-scan progress so it can be resumed later."""
    data = {
        "key": _cache_key(query, limit),
        "partial": True,
        "timestamp": time.time(),
        "query": query,
        "limit": limit,
        "all_ids": all_ids,
        "processed": processed,
        "sender_counts": dict(sender_counts),
        "sender_ids": {k: list(v) for k, v in sender_ids.items()},
        "sender_tags": {k: list(v) for k, v in sender_tags.items()},
    }
    CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_cache():
    if CACHE_FILE.exists():
        CACHE_FILE.unlink()
        console.print("[green]Cache cleared.[/green]")
    else:
        console.print("[yellow]No cache file found.[/yellow]")


# ---------------------------------------------------------------------------
# Feature: Sender analysis
# ---------------------------------------------------------------------------

def _fmt_duration(secs: float) -> str:
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    return f"{secs // 60}m{secs % 60:02d}s"


def analyze_senders(service, creds):
    console.print("\n[bold cyan]Analyzing senders...[/bold cyan]")
    limit = IntPrompt.ask("Max emails to scan", default=500)
    query = Prompt.ask("Optional Gmail search filter (leave blank for all)", default="")

    cached = _load_cache(query, limit)

    # --- Resume from partial checkpoint ---
    if cached and cached.get("partial"):
        processed = cached["processed"]
        total = len(cached["all_ids"])
        console.print(
            f"[yellow]Interrupted scan found[/yellow] — {processed}/{total} emails done "
            f"({_age_str(cached['timestamp'])}). Resume?"
        )
        if Confirm.ask("Resume", default=True):
            all_ids = cached["all_ids"]
            sender_counts: dict[str, int] = defaultdict(int, cached["sender_counts"])
            sender_ids: dict[str, list[str]] = defaultdict(list, {k: list(v) for k, v in cached["sender_ids"].items()})
            sender_tags: dict[str, set[str]] = defaultdict(set, {k: set(v) for k, v in cached.get("sender_tags", {}).items()})
            start_index = processed
        else:
            all_ids = None
            sender_counts = defaultdict(int)
            sender_ids = defaultdict(list)
            sender_tags = defaultdict(set)
            start_index = 0

    # --- Load completed cache ---
    elif cached and not cached.get("partial"):
        console.print(
            f"[yellow]Completed scan found[/yellow] — {cached['limit']} emails, "
            f"filter: '{cached['query'] or 'none'}', saved {_age_str(cached['timestamp'])}."
        )
        if Confirm.ask("Load from cache", default=True):
            sorted_senders = [tuple(x) for x in cached["sorted_senders"]]
            cached_tags = {k: set(v) for k, v in cached.get("sender_tags", {}).items()}
            _display_and_act(service, sorted_senders, cached["sender_ids"], sender_tags=cached_tags, creds=creds)
            return
        all_ids = None
        sender_counts = defaultdict(int)
        sender_ids = defaultdict(list)
        sender_tags = defaultdict(set)
        start_index = 0

    else:
        all_ids = None
        sender_counts = defaultdict(int)
        sender_ids = defaultdict(list)
        sender_tags = defaultdict(set)
        start_index = 0

    # --- Fetch message list if not resuming ---
    if all_ids is None:
        with console.status("Fetching message list..."):
            messages = fetch_messages(service, query=query, max_results=limit)
        if not messages:
            console.print("[yellow]No messages found.[/yellow]")
            return
        all_ids = [m["id"] for m in messages]
        console.print(f"Fetched [bold]{len(all_ids)}[/bold] messages.")

    total = len(all_ids)
    batch_size = 200
    processed = start_index

    # --- Calibration: time the first batch to give an upfront ETA ---
    if start_index < total:
        calib_chunk = all_ids[start_index : start_index + batch_size]
        with console.status("Measuring speed..."):
            t0 = time.time()
            calib_senders = get_senders_concurrent(creds, calib_chunk)
            calib_elapsed = time.time() - t0

        for mid, (sender, tags) in calib_senders.items():
            sender_counts[sender] += 1
            sender_ids[sender].append(mid)
            sender_tags[sender].update(tags)

        processed = start_index + len(calib_chunk)
        rate_calib = len(calib_chunk) / calib_elapsed if calib_elapsed > 0 else 1
        remaining_after_calib = total - processed
        eta_secs = remaining_after_calib / rate_calib
        _save_checkpoint(query, limit, all_ids, processed, sender_counts, sender_ids, sender_tags)

        console.print(
            f"Speed: [cyan]{rate_calib:.0f} emails/s[/cyan] — "
            f"[bold]{total - processed}[/bold] emails remaining — "
            f"estimated [bold]{_fmt_duration(eta_secs)}[/bold] to finish."
        )
        if not Confirm.ask("Continue scan", default=True):
            console.print("[yellow]Paused. Progress saved — resume anytime.[/yellow]")
            return

    scan_start = time.time()
    emails_at_start = processed  # already processed before this session

    console.print(
        f"Reading headers from [bold]{processed}[/bold] to [bold]{total}[/bold]... "
        "[dim](Ctrl+C to pause and save progress)[/dim]"
    )

    try:
        with console.status("") as status:
            for i in range(processed, total, batch_size):
                chunk = all_ids[i : i + batch_size]
                senders = get_senders_concurrent(creds, chunk)
                for mid, (sender, tags) in senders.items():
                    sender_counts[sender] += 1
                    sender_ids[sender].append(mid)
                    sender_tags[sender].update(tags)

                processed = i + len(chunk)
                elapsed = time.time() - scan_start
                session_done = processed - emails_at_start
                rate = session_done / elapsed if elapsed > 0 else 0
                remaining = total - processed
                eta = f"~{_fmt_duration(remaining / rate)}" if rate > 0 else "?"
                status.update(
                    f"Reading headers... [bold]{processed}/{total}[/bold] "
                    f"| [cyan]{rate:.0f} emails/s[/cyan] "
                    f"| ETA {eta} "
                    f"| elapsed {_fmt_duration(elapsed)}"
                )
                # Save checkpoint after every batch
                _save_checkpoint(query, limit, all_ids, processed, sender_counts, sender_ids, sender_tags)

    except KeyboardInterrupt:
        console.print(
            f"\n[yellow]Paused at {processed}/{total} emails.[/yellow] "
            "Progress saved — run again and choose Resume to continue."
        )
        return

    elapsed_total = time.time() - scan_start + (calib_elapsed if start_index < total else 0)
    console.print(
        f"[green]Done.[/green] Scanned {total} emails in [bold]{_fmt_duration(elapsed_total)}[/bold] "
        f"([cyan]{total / elapsed_total:.0f} emails/s[/cyan])"
    )

    sorted_senders = sorted(sender_counts.items(), key=lambda x: x[1], reverse=True)
    _save_complete_cache(query, limit, sorted_senders, sender_ids, sender_tags)
    console.print("[dim]Results cached to sender_cache.json[/dim]")
    _display_and_act(service, sorted_senders, sender_ids, sender_tags=sender_tags, creds=creds)


PAGE_SIZE = 30


def _fmt_tags(tags: set) -> str:
    parts = []
    if "important" in tags:
        parts.append("[bold yellow]important[/bold yellow]")
    if "newsletter" in tags:
        parts.append("[dim]newsletter[/dim]")
    return "  ".join(parts)


def _print_sender_page(sorted_senders: list, start: int, end: int, title_suffix: str = "", sender_tags: dict | None = None):
    end = min(end, len(sorted_senders))
    title = f"Senders {start + 1}–{end} of {len(sorted_senders)}{title_suffix}"
    table = Table(title=title, show_lines=False)
    table.add_column("#", style="dim", width=5)
    table.add_column("Sender", style="cyan", no_wrap=False)
    table.add_column("Emails", justify="right", style="green")
    table.add_column("Tags", no_wrap=True)
    for idx, (sender, count) in enumerate(sorted_senders[start:end], start + 1):
        tags = (sender_tags or {}).get(sender, set())
        table.add_row(str(idx), sender, str(count), _fmt_tags(tags))
    console.print(table)


_EMAIL_VIEW_LIMIT = 50


def _view_sender_emails(creds, sender: str, msg_ids: list[str]):
    """Fetch and display subject + date for a sender's messages."""
    sample = msg_ids[:_EMAIL_VIEW_LIMIT]

    def fetch_subject(mid: str) -> tuple[str, str]:
        session = _get_session(creds)
        url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{mid}"
        for attempt in range(6):
            wait = (2 ** attempt) + random.uniform(0, 1)
            try:
                resp = session.get(url, params={
                    "format": "metadata",
                    "metadataHeaders": ["Subject", "From"],
                })
                if resp.status_code == 200:
                    data = resp.json()
                    hdrs = {h["name"].lower(): h["value"] for h in data.get("payload", {}).get("headers", [])}
                    return hdrs.get("subject", "(no subject)"), hdrs.get("from", "")
                elif resp.status_code == 429:
                    _api_log.warning(f"429 rate_limit  mid={mid}  attempt={attempt}  wait={wait:.1f}s")
                    time.sleep(wait)
                elif resp.status_code >= 500:
                    _api_log.warning(f"{resp.status_code} server_error  mid={mid}  attempt={attempt}  wait={wait:.1f}s")
                    time.sleep(wait)
                else:
                    _api_log.warning(f"{resp.status_code} unexpected  mid={mid}  body={resp.text[:120]!r}")
                    break
            except Exception as exc:
                _api_log.warning(f"exception  mid={mid}  attempt={attempt}  wait={wait:.1f}s  error={exc}")
                time.sleep(wait)
        else:
            _api_log.warning(f"all_retries_exhausted  mid={mid}")
        return "(error)", ""

    with console.status(f"Fetching {len(sample)} subjects..."):
        with ThreadPoolExecutor(max_workers=15) as executor:
            rows = list(executor.map(fetch_subject, sample))

    table = Table(title=f"Emails from: {sender}", show_lines=False)
    table.add_column("#", style="dim", width=4)
    table.add_column("From", style="dim", no_wrap=True)
    table.add_column("Subject", style="cyan", no_wrap=False)
    for i, (subject, from_hdr) in enumerate(rows, 1):
        table.add_row(str(i), from_hdr, subject)
    console.print(table)
    if len(msg_ids) > _EMAIL_VIEW_LIMIT:
        console.print(f"[dim]Showing first {_EMAIL_VIEW_LIMIT} of {len(msg_ids)} emails.[/dim]")


def _remove_sender_from_cache(sender: str):
    """Remove a sender from the cache file (both complete and partial formats)."""
    if not CACHE_FILE.exists():
        return
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return

    if data.get("partial"):
        data["sender_counts"].pop(sender, None)
        data["sender_ids"].pop(sender, None)
        data.get("sender_tags", {}).pop(sender, None)
    else:
        data["sorted_senders"] = [s for s in data.get("sorted_senders", []) if s[0] != sender]
        data["sender_ids"].pop(sender, None)
        data.get("sender_tags", {}).pop(sender, None)

    CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _display_and_act(service, sorted_senders: list, sender_ids: dict, title_suffix: str = "", sender_tags: dict | None = None, creds=None):
    page_start = 0
    redisplay = True

    while True:
        # (Re)display current page only when needed
        if redisplay and page_start < len(sorted_senders):
            _print_sender_page(sorted_senders, page_start, page_start + PAGE_SIZE, title_suffix, sender_tags)
        redisplay = True

        shown_up_to = min(page_start + PAGE_SIZE, len(sorted_senders))
        has_more = shown_up_to < len(sorted_senders)
        if has_more:
            console.print(
                f"[dim]Showing {shown_up_to} of {len(sorted_senders)} senders.[/dim]"
            )

        console.print(
            "\n[bold]Actions:[/bold] enter a sender [bold]#[/bold] to act on it"
            + (" | [bold]m[/bold] more senders" if has_more else "")
            + " | [bold]0[/bold] to go back"
            + (" | [bold]b[/bold] back to top" if page_start > 0 else "")
        )
        raw = Prompt.ask("Choice")

        if raw.strip() == "0":
            return
        if raw.strip().lower() == "m":
            if has_more:
                page_start += PAGE_SIZE
                continue
            else:
                console.print("[yellow]All senders already shown.[/yellow]")
                continue
        if raw.strip().lower() == "b":
            page_start = 0
            continue

        try:
            choice = int(raw)
        except ValueError:
            console.print("[red]Enter a number or 'm'.[/red]")
            continue

        if choice < 1 or choice > len(sorted_senders):
            console.print(f"[red]Enter a number between 1 and {len(sorted_senders)}.[/red]")
            continue

        idx = choice - 1
        sender, count = sorted_senders[idx]
        ids = sender_ids[sender]
        console.print(f"\nSelected: [cyan]{sender}[/cyan] ({count} emails)")

        action_choices = ["view", "delete", "trash", "mark_read", "mark_unread", "back"] if creds else ["delete", "trash", "mark_read", "mark_unread", "back"]
        action = Prompt.ask(
            "Action",
            choices=action_choices,
            default="back",
        )

        if action == "back":
            continue
        elif action == "view":
            _view_sender_emails(creds, sender, ids)
            redisplay = False
            continue
        elif action == "delete":
            if Confirm.ask(f"Permanently delete all {count} emails from this sender?"):
                with console.status("Deleting..."):
                    n = batch_delete(service, ids)
                console.print(f"[green]Deleted {n} emails.[/green]")
                sorted_senders.pop(idx)
                sender_ids.pop(sender, None)
                if sender_tags:
                    sender_tags.pop(sender, None)
                _remove_sender_from_cache(sender)
                # Keep page_start; clamp if we're now past end of list
                page_start = min(page_start, max(0, len(sorted_senders) - 1))
        elif action == "trash":
            if Confirm.ask(f"Move {count} emails from this sender to Trash?"):
                with console.status("Moving to Trash..."):
                    n = batch_modify(service, ids, add_labels=["TRASH"], remove_labels=["INBOX"])
                console.print(f"[green]Moved {n} emails to Trash.[/green]")
                sorted_senders.pop(idx)
                sender_ids.pop(sender, None)
                if sender_tags:
                    sender_tags.pop(sender, None)
                _remove_sender_from_cache(sender)
                page_start = min(page_start, max(0, len(sorted_senders) - 1))
        elif action == "mark_read":
            with console.status("Marking as read..."):
                n = batch_modify(service, ids, remove_labels=["UNREAD"])
            console.print(f"[green]Marked {n} emails as read.[/green]")
        elif action == "mark_unread":
            with console.status("Marking as unread..."):
                n = batch_modify(service, ids, add_labels=["UNREAD"])
            console.print(f"[green]Marked {n} emails as unread.[/green]")



# ---------------------------------------------------------------------------
# Feature: Search & bulk action
# ---------------------------------------------------------------------------

def search_and_act(service):
    console.print("\n[bold cyan]Search & Bulk Action[/bold cyan]")
    console.print("Use Gmail search syntax, e.g.:")
    console.print("  [dim]older_than:1y  |  label:newsletters  |  from:noreply@example.com  |  is:unread[/dim]\n")

    query = Prompt.ask("Gmail search query")
    if not query.strip():
        console.print("[yellow]Empty query, returning.[/yellow]")
        return

    limit = IntPrompt.ask("Max emails to fetch", default=500)
    with console.status("Searching..."):
        messages = fetch_messages(service, query=query, max_results=limit)

    if not messages:
        console.print("[yellow]No messages found.[/yellow]")
        return

    console.print(f"Found [bold]{len(messages)}[/bold] messages.")

    action = Prompt.ask(
        "Action",
        choices=["delete", "trash", "mark_read", "mark_unread", "back"],
        default="back",
    )

    if action == "back":
        return

    ids = [m["id"] for m in messages]

    if action == "delete":
        if Confirm.ask(f"Permanently delete {len(ids)} emails?"):
            with console.status("Deleting..."):
                n = batch_delete(service, ids)
            console.print(f"[green]Deleted {n} emails.[/green]")
    elif action == "trash":
        if Confirm.ask(f"Move {len(ids)} emails to Trash?"):
            with console.status("Moving to Trash..."):
                n = batch_modify(service, ids, add_labels=["TRASH"], remove_labels=["INBOX"])
            console.print(f"[green]Moved {n} emails to Trash.[/green]")
    elif action == "mark_read":
        with console.status("Marking as read..."):
            n = batch_modify(service, ids, remove_labels=["UNREAD"])
        console.print(f"[green]Marked {n} emails as read.[/green]")
    elif action == "mark_unread":
        with console.status("Marking as unread..."):
            n = batch_modify(service, ids, add_labels=["UNREAD"])
        console.print(f"[green]Marked {n} emails as unread.[/green]")


# ---------------------------------------------------------------------------
# Feature: List labels
# ---------------------------------------------------------------------------

def list_labels(service):
    console.print("\n[bold cyan]Gmail Labels[/bold cyan]")
    with console.status("Fetching labels..."):
        labels = get_labels(service)

    system_labels = [l for l in labels if l["type"] == "system"]
    user_labels = [l for l in labels if l["type"] == "user"]

    table = Table(title="Labels", show_lines=False)
    table.add_column("Type", style="dim")
    table.add_column("Name", style="cyan")
    table.add_column("ID", style="dim")

    for lbl in sorted(system_labels, key=lambda x: x["name"]):
        table.add_row("system", lbl["name"], lbl["id"])
    for lbl in sorted(user_labels, key=lambda x: x["name"]):
        table.add_row("user", lbl["name"], lbl["id"])

    console.print(table)


# ---------------------------------------------------------------------------
# Feature: Inbox stats
# ---------------------------------------------------------------------------

def inbox_stats(service):
    console.print("\n[bold cyan]Inbox Stats[/bold cyan]")
    with console.status("Fetching stats..."):
        profile = service.users().getProfile(userId="me").execute()
        unread_result = service.users().messages().list(
            userId="me", q="is:unread", maxResults=1
        ).execute()
        # Gmail doesn't give a reliable total without paging, use profile data
        total_msgs = profile.get("messagesTotal", "?")
        total_threads = profile.get("threadsTotal", "?")
        email_address = profile.get("emailAddress", "?")
        history_id = profile.get("historyId", "?")

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Key", style="dim")
    table.add_column("Value", style="bold cyan")
    table.add_row("Email", email_address)
    table.add_row("Total messages", str(total_msgs))
    table.add_row("Total threads", str(total_threads))
    console.print(table)


def view_cache(service, creds=None):
    console.print("\n[bold cyan]Sender Cache Viewer[/bold cyan]")

    if not CACHE_FILE.exists():
        console.print("[yellow]No cache file found. Run a sender analysis first.[/yellow]")
        return

    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        console.print(f"[red]Failed to read cache:[/red] {e}")
        return

    is_partial = data.get("partial", False)
    saved = _age_str(data["timestamp"])
    query_label = data.get("query") or "none"
    limit = data.get("limit", "?")

    # Header info
    status_tag = "[yellow]PARTIAL (interrupted)[/yellow]" if is_partial else "[green]COMPLETE[/green]"
    console.print(f"Status : {status_tag}")
    console.print(f"Saved  : {saved}")
    console.print(f"Filter : {query_label}")
    console.print(f"Limit  : {limit}")

    if is_partial:
        processed = data.get("processed", 0)
        total = len(data.get("all_ids", []))
        pct = processed / total * 100 if total else 0
        console.print(f"Progress: [bold]{processed}/{total}[/bold] emails ({pct:.1f}%)")
        sender_counts = data.get("sender_counts", {})
        sender_ids = data.get("sender_ids", {})
        sender_tags = {k: set(v) for k, v in data.get("sender_tags", {}).items()}
        sorted_senders = sorted(sender_counts.items(), key=lambda x: x[1], reverse=True)
        console.print(f"Unique senders so far: [bold]{len(sorted_senders)}[/bold]\n")
    else:
        raw = data.get("sorted_senders", [])
        sorted_senders = sorted((tuple(x) for x in raw), key=lambda x: x[1], reverse=True)
        sender_ids = data.get("sender_ids", {})
        sender_tags = {k: set(v) for k, v in data.get("sender_tags", {}).items()}
        total_emails = sum(c for _, c in sorted_senders)
        console.print(f"Emails scanned : [bold]{total_emails}[/bold]")
        console.print(f"Unique senders : [bold]{len(sorted_senders)}[/bold]\n")

    if not sorted_senders:
        console.print("[yellow]No sender data in cache yet.[/yellow]")
        return

    title_suffix = " (partial scan)" if is_partial else ""
    _display_and_act(service, sorted_senders, sender_ids, title_suffix, sender_tags=sender_tags, creds=creds)


# ---------------------------------------------------------------------------
# Main menu
# ---------------------------------------------------------------------------

MENU_OPTIONS = {
    "1": ("Analyze senders (count emails per sender)", analyze_senders),
    "2": ("Search & bulk action (delete / trash / mark read)", search_and_act),
    "3": ("List all labels", list_labels),
    "4": ("Inbox stats", inbox_stats),
    "5": ("View sender cache", view_cache),
    "6": ("Clear sender cache", lambda _: clear_cache()),
    "0": ("Exit", None),
}


def main():
    console.print("[bold magenta]Gmail Helper[/bold magenta]", justify="center")
    console.print("[dim]Connecting to Gmail...[/dim]")

    try:
        service, creds = build_service()
    except FileNotFoundError as e:
        console.print(f"\n[red]Error:[/red] {e}")
        sys.exit(1)
    except Exception as e:
        console.print(f"\n[red]Authentication failed:[/red] {e}")
        sys.exit(1)

    console.print("[green]Connected.[/green]\n")

    # Wrap functions that need creds
    def _analyze(svc): analyze_senders(svc, creds)
    def _view_cache(svc): view_cache(svc, creds)

    menu = {
        **MENU_OPTIONS,
        "1": ("Analyze senders (count emails per sender)", _analyze),
        "5": ("View sender cache", _view_cache),
    }

    while True:
        console.print("\n[bold]Main Menu[/bold]")
        for key, (label, _) in menu.items():
            console.print(f"  [bold cyan]{key}[/bold cyan]  {label}")

        choice = Prompt.ask("\nChoice", choices=list(menu.keys()))

        if choice == "0":
            console.print("Bye!")
            break

        label, fn = menu[choice]
        try:
            fn(service)
        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted, back to menu.[/yellow]")
        except Exception as e:
            console.print(f"\n[red]Error:[/red] {e}")


if __name__ == "__main__":
    main()
