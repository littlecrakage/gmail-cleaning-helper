"""Gmail Helper - Interactive CLI to manage your Gmail inbox."""

import json
import logging
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
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


def get_or_create_label(service, name: str) -> str:
    """Return label ID for the given name, creating it if it doesn't exist.
    Retries up to 4 times on transient errors (503, 500, etc.)."""
    for attempt in range(4):
        try:
            labels = get_labels(service)
            for lbl in labels:
                if lbl["name"].lower() == name.lower():
                    return lbl["id"]
            new_label = service.users().labels().create(
                userId="me",
                body={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
            ).execute()
            return new_label["id"]
        except Exception as e:
            err_str = str(e)
            if attempt < 3 and ("503" in err_str or "500" in err_str or "backendError" in err_str):
                wait = 2 ** attempt + random.uniform(0, 1)
                _api_log.warning(f"get_or_create_label  attempt={attempt}  wait={wait:.1f}s  error={e}")
                time.sleep(wait)
            else:
                raise


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


_EMAIL_FETCH_CAP = 200
_EMAIL_PAGE_SIZE = 20


def _view_sender_emails(creds, sender: str, msg_ids: list[str]):
    """Interactive paginated email viewer for a sender."""
    sample = msg_ids[:_EMAIL_FETCH_CAP]

    def fetch_subject(mid: str) -> tuple[str, str, bool]:
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
                    is_important = "IMPORTANT" in data.get("labelIds", [])
                    return hdrs.get("subject", "(no subject)"), hdrs.get("from", ""), is_important
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
        return "(error)", "", False

    with console.status(f"Fetching {len(sample)} email subjects..."):
        with ThreadPoolExecutor(max_workers=8) as executor:
            all_rows: list[tuple[str, str, bool]] = list(executor.map(fetch_subject, sample))

    important_only = False
    page = 0

    while True:
        rows = [r for r in all_rows if not important_only or r[2]]
        total_pages = max(1, (len(rows) + _EMAIL_PAGE_SIZE - 1) // _EMAIL_PAGE_SIZE)
        page = min(page, total_pages - 1)
        start = page * _EMAIL_PAGE_SIZE
        end = min(start + _EMAIL_PAGE_SIZE, len(rows))

        filter_label = " [important only]" if important_only else ""
        title = f"Emails from: {sender}{filter_label} — {start + 1}–{end} of {len(rows)}"
        table = Table(title=title, show_lines=False)
        table.add_column("#", style="dim", width=4)
        table.add_column("From", style="dim", no_wrap=True)
        table.add_column("Subject", style="cyan", no_wrap=False)
        for i, (subject, from_hdr, is_imp) in enumerate(rows[start:end], start + 1):
            subj_display = f"[bold]{subject}[/bold]" if is_imp else subject
            table.add_row(str(i), from_hdr, subj_display)
        console.print(table)

        if len(msg_ids) > _EMAIL_FETCH_CAP:
            console.print(f"[dim]Fetched first {_EMAIL_FETCH_CAP} of {len(msg_ids)} emails.[/dim]")

        has_next = end < len(rows)
        has_prev = page > 0
        nav = []
        if has_next:
            nav.append("[bold cyan]\\[n][/bold cyan] next page")
        if has_prev:
            nav.append("[bold cyan]\\[p][/bold cyan] prev page")
        nav.append("[bold cyan]\\[i][/bold cyan] " + ("show all" if important_only else "important only"))
        nav.append("[bold cyan]\\[q][/bold cyan] back to sender list")
        console.print("\n" + "   ".join(nav))

        raw = Prompt.ask("Choice").strip().lower()
        if raw in ("q", "0", "o"):
            break
        elif raw == "n" and has_next:
            page += 1
        elif raw == "p" and has_prev:
            page -= 1
        elif raw == "i":
            important_only = not important_only
            page = 0
        else:
            console.print("[red]Invalid choice.[/red]")


def _sender_to_email(sender: str) -> str:
    """Extract bare email address from a 'Name <email>' or plain 'email' string."""
    m = re.search(r"<([^>]+)>", sender)
    return m.group(1).strip() if m else sender.strip()


def _parse_selection(raw: str, max_n: int) -> list[int] | None:
    """Parse a comma-separated / range selection string into a deduplicated list of 1-based indices.
    Returns None on parse error or out-of-range input."""
    indices: list[int] = []
    for token in raw.strip().split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token and not token.lstrip("-").isdigit():
            parts = token.split("-", 1)
            try:
                lo, hi = int(parts[0].strip()), int(parts[1].strip())
                indices.extend(range(lo, hi + 1))
            except ValueError:
                return None
        else:
            try:
                indices.append(int(token))
            except ValueError:
                return None

    if not indices:
        return None
    if any(n < 1 or n > max_n for n in indices):
        return None

    seen: set[int] = set()
    return [n for n in indices if not (n in seen or seen.add(n))]


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
    dry_run = False

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

        if dry_run:
            console.print("[bold yellow]DRY RUN — no changes will be made[/bold yellow]")

        nav_parts = ["[bold cyan]\\[#][/bold cyan] select sender(s)  [dim](e.g. 1  or  1,3,5  or  2-6)[/dim]"]
        if has_more:
            nav_parts.append("[bold cyan]\\[m][/bold cyan] more senders")
        if page_start > 0:
            nav_parts.append("[bold cyan]\\[b][/bold cyan] back to top")
        nav_parts.append(f"[bold cyan]\\[dr][/bold cyan] {'disable' if dry_run else 'enable'} dry run")
        nav_parts.append("[bold cyan]\\[0][/bold cyan] go back")
        console.print("\n" + "   ".join(nav_parts))
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
        if raw.strip().lower() == "dr":
            dry_run = not dry_run
            console.print(f"[yellow]Dry run {'enabled' if dry_run else 'disabled'}.[/yellow]")
            redisplay = False
            continue

        selected_indices = _parse_selection(raw, len(sorted_senders))
        if selected_indices is None:
            console.print("[red]Enter number(s), e.g. 1  or  1,3,5  or  2-6.[/red]")
            continue

        multi = len(selected_indices) > 1

        if multi:
            names = [sorted_senders[i - 1][0] for i in selected_indices]
            total_count = sum(sorted_senders[i - 1][1] for i in selected_indices)
            console.print(f"\nSelected [cyan]{len(selected_indices)} senders[/cyan] ({total_count} emails total):")
            for n, name in zip(selected_indices, names):
                console.print(f"  [dim]{n}.[/dim] {name}")
            action_choices = ["delete", "trash", "mark_read", "mark_unread", "label", "query", "back"]
        else:
            idx = selected_indices[0] - 1
            sender, count = sorted_senders[idx]
            ids = sender_ids[sender]
            console.print(f"\nSelected: [cyan]{sender}[/cyan] ({count} emails)")
            action_choices = ["view", "delete", "trash", "mark_read", "mark_unread", "label", "query", "back"] if creds else ["delete", "trash", "mark_read", "mark_unread", "label", "query", "back"]

        action = Prompt.ask(
            "Action",
            choices=action_choices,
            default="back",
        )

        if action == "back":
            continue

        if not multi and action == "view":
            _view_sender_emails(creds, sender, ids)
            continue

        # --- Multi or single action ---
        # Collect senders to act on (process in reverse index order for safe removal)
        targets = [(i - 1, sorted_senders[i - 1][0], sorted_senders[i - 1][1]) for i in selected_indices]

        if action == "delete":
            total = sum(c for _, _, c in targets)
            label = f"{len(targets)} senders ({total} emails)" if multi else f"all {targets[0][2]} emails from {targets[0][1]}"
            if dry_run:
                console.print(f"[yellow][DRY RUN][/yellow] Would permanently delete {label}.")
            elif Confirm.ask(f"Permanently delete {label}?"):
                with console.status("Deleting..."):
                    for _, s, _ in targets:
                        batch_delete(service, sender_ids[s])
                n_deleted = len(targets)
                console.print(f"[green]Deleted emails from {n_deleted} sender(s).[/green]")
                for idx_, s, _ in sorted(targets, key=lambda x: x[0], reverse=True):
                    sorted_senders.pop(idx_)
                    sender_ids.pop(s, None)
                    if sender_tags:
                        sender_tags.pop(s, None)
                    _remove_sender_from_cache(s)
                page_start = min(page_start, max(0, len(sorted_senders) - 1))
        elif action == "trash":
            total = sum(c for _, _, c in targets)
            label = f"{len(targets)} senders ({total} emails)" if multi else f"{targets[0][2]} emails from {targets[0][1]}"
            if dry_run:
                console.print(f"[yellow][DRY RUN][/yellow] Would move {label} to Trash.")
            elif Confirm.ask(f"Move {label} to Trash?"):
                with console.status("Moving to Trash..."):
                    for _, s, _ in targets:
                        batch_modify(service, sender_ids[s], add_labels=["TRASH"], remove_labels=["INBOX"])
                console.print(f"[green]Moved emails from {len(targets)} sender(s) to Trash.[/green]")
                for idx_, s, _ in sorted(targets, key=lambda x: x[0], reverse=True):
                    sorted_senders.pop(idx_)
                    sender_ids.pop(s, None)
                    if sender_tags:
                        sender_tags.pop(s, None)
                    _remove_sender_from_cache(s)
                page_start = min(page_start, max(0, len(sorted_senders) - 1))
        elif action == "mark_read":
            if dry_run:
                total = sum(c for _, _, c in targets)
                console.print(f"[yellow][DRY RUN][/yellow] Would mark {total} emails from {len(targets)} sender(s) as read.")
            else:
                with console.status("Marking as read..."):
                    for _, s, _ in targets:
                        batch_modify(service, sender_ids[s], remove_labels=["UNREAD"])
                console.print(f"[green]Marked emails from {len(targets)} sender(s) as read.[/green]")
        elif action == "mark_unread":
            if dry_run:
                total = sum(c for _, _, c in targets)
                console.print(f"[yellow][DRY RUN][/yellow] Would mark {total} emails from {len(targets)} sender(s) as unread.")
            else:
                with console.status("Marking as unread..."):
                    for _, s, _ in targets:
                        batch_modify(service, sender_ids[s], add_labels=["UNREAD"])
                console.print(f"[green]Marked emails from {len(targets)} sender(s) as unread.[/green]")
        elif action == "label":
            with console.status("Fetching labels..."):
                all_labels = get_labels(service)
            user_labels = sorted(
                [l for l in all_labels if l.get("type") == "user"],
                key=lambda l: l["name"].lower(),
            )
            if user_labels:
                console.print("\n[dim]Existing labels:[/dim]")
                for lbl in user_labels:
                    console.print(f"  [dim]·[/dim] {lbl['name']}")
                console.print()
            label_name = Prompt.ask("Label name (existing or new)").strip()
            if not label_name:
                console.print("[yellow]No label name entered, skipping.[/yellow]")
                continue
            if dry_run:
                total = sum(c for _, _, c in targets)
                console.print(f"[yellow][DRY RUN][/yellow] Would apply label '[bold]{label_name}[/bold]' to {total} emails from {len(targets)} sender(s).")
            else:
                with console.status(f"Applying label '{label_name}'..."):
                    label_id = get_or_create_label(service, label_name)
                    for _, s, _ in targets:
                        batch_modify(service, sender_ids[s], add_labels=[label_id])
                console.print(f"[green]Applied label '[bold]{label_name}[/bold]' to emails from {len(targets)} sender(s).[/green]")
        elif action == "query":
            emails = [_sender_to_email(s) for _, s, _ in targets]
            if len(emails) == 1:
                query_str = f"from:{emails[0]}"
            else:
                query_str = " OR ".join(f"from:{e}" for e in emails)
            console.print(f"\n[bold]Gmail search query:[/bold]")
            console.print(f"[bold cyan]{query_str}[/bold cyan]\n")
            console.print("[dim]Paste this into the Gmail search bar.[/dim]")


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
# Feature: Bulk unsubscribe
# ---------------------------------------------------------------------------

def _parse_list_unsubscribe(header_value: str) -> tuple[str | None, str | None]:
    """Return (https_url, mailto_url) from a List-Unsubscribe header value."""
    https_url = None
    mailto_url = None
    for match in re.finditer(r"<([^>]+)>", header_value):
        url = match.group(1).strip()
        if (url.startswith("https://") or url.startswith("http://")) and not https_url:
            https_url = url
        elif url.startswith("mailto:") and not mailto_url:
            mailto_url = url
    return https_url, mailto_url


def _fetch_unsubscribe_info(creds, msg_id: str) -> tuple[str | None, str | None, bool]:
    """Fetch List-Unsubscribe headers from a message.
    Returns (https_url, mailto_url, has_one_click)."""
    session = _get_session(creds)
    url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}"
    for attempt in range(6):
        wait = (2 ** attempt) + random.uniform(0, 1)
        try:
            resp = session.get(url, params={
                "format": "metadata",
                "metadataHeaders": ["List-Unsubscribe", "List-Unsubscribe-Post"],
            })
            if resp.status_code == 200:
                data = resp.json()
                hdrs = {h["name"].lower(): h["value"] for h in data.get("payload", {}).get("headers", [])}
                unsub = hdrs.get("list-unsubscribe", "")
                post = hdrs.get("list-unsubscribe-post", "")
                https_url, mailto_url = _parse_list_unsubscribe(unsub)
                has_one_click = "one-click" in post.lower() and https_url is not None
                return https_url, mailto_url, has_one_click
            elif resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(wait)
            else:
                break
        except Exception:
            time.sleep(wait)
    return None, None, False


def bulk_unsubscribe(service, creds):
    """List newsletter senders and unsubscribe via List-Unsubscribe headers."""
    if not CACHE_FILE.exists():
        console.print("[yellow]No sender cache found. Run a sender analysis first (option 1 or 6).[/yellow]")
        return

    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        console.print(f"[red]Failed to read cache:[/red] {e}")
        return

    sender_tags = {k: set(v) for k, v in data.get("sender_tags", {}).items()}
    sender_ids = data.get("sender_ids", {})

    if data.get("partial"):
        sender_counts = data.get("sender_counts", {})
        all_senders = sorted(sender_counts.items(), key=lambda x: x[1], reverse=True)
    else:
        raw_senders = data.get("sorted_senders", [])
        all_senders = sorted((tuple(x) for x in raw_senders), key=lambda x: x[1], reverse=True)

    newsletter_senders = sorted(
        ((s, c) for s, c in all_senders if "newsletter" in sender_tags.get(s, set())),
        key=lambda x: x[1],
        reverse=True,
    )[:30]

    if not newsletter_senders:
        console.print("[yellow]No newsletter senders in cache. Run a sender analysis first.[/yellow]")
        return

    console.print(f"\n[bold cyan]Newsletter Senders[/bold cyan] — top {len(newsletter_senders)} by email count\n")

    table = Table(show_header=True, header_style="bold")
    table.add_column("#", style="dim", width=4)
    table.add_column("Sender")
    table.add_column("Emails", justify="right")
    for i, (sender, count) in enumerate(newsletter_senders, 1):
        table.add_row(str(i), sender, str(count))
    console.print(table)

    console.print("\n[bold cyan]\\[#][/bold cyan] select sender(s)  [dim](e.g. 1  or  1,3,5  or  2-6)[/dim]   [bold cyan]\\[0][/bold cyan] go back")
    raw = Prompt.ask("Choice")

    if raw.strip() == "0":
        return

    selected_indices = _parse_selection(raw, len(newsletter_senders))
    if selected_indices is None:
        console.print("[red]Enter number(s), e.g. 1  or  1,3,5  or  2-6.[/red]")
        return

    targets = [(newsletter_senders[i - 1][0], newsletter_senders[i - 1][1]) for i in selected_indices]
    console.print(f"\nAttempting to unsubscribe from [bold]{len(targets)}[/bold] sender(s).\n")

    ok_senders: list[str] = []

    for sender, count in targets:
        ids = sender_ids.get(sender, [])
        console.print(f"[cyan]{sender}[/cyan]")
        if not ids:
            console.print("  [yellow]No emails found in cache for this sender.[/yellow]")
            continue

        with console.status("  Fetching unsubscribe info..."):
            https_url, mailto_url, has_one_click = _fetch_unsubscribe_info(creds, ids[0])

        if has_one_click and https_url:
            console.print(f"  [dim]One-click POST → {https_url[:70]}[/dim]")
            try:
                req = urllib.request.Request(
                    https_url,
                    data=b"List-Unsubscribe=One-Click",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    status_code = resp.status
                if status_code < 400:
                    console.print("  [green]Unsubscribed (one-click).[/green]")
                    ok_senders.append(sender)
                else:
                    console.print(f"  [yellow]POST returned {status_code}. Opening browser as fallback...[/yellow]")
                    webbrowser.open(https_url)
                    ok_senders.append(sender)
            except Exception as e:
                console.print(f"  [yellow]POST failed ({e}). Opening browser as fallback...[/yellow]")
                webbrowser.open(https_url)
                ok_senders.append(sender)
        elif https_url:
            console.print(f"  [dim]Opening unsubscribe page in browser...[/dim]")
            webbrowser.open(https_url)
            ok_senders.append(sender)
        elif mailto_url:
            console.print(f"  [dim]Opening mailto unsubscribe link...[/dim]")
            webbrowser.open(mailto_url)
            ok_senders.append(sender)
        else:
            console.print("  [yellow]No unsubscribe link found in this sender's emails.[/yellow]")

    console.print(f"\n[bold]Done.[/bold] Triggered unsubscribe for {len(ok_senders)}/{len(targets)} sender(s).")

    if not ok_senders:
        return

    if Confirm.ask("\nAlso delete all existing emails from these senders?"):
        action = Prompt.ask("Action", choices=["delete", "trash"], default="trash")
        for sender in ok_senders:
            ids = sender_ids.get(sender, [])
            if not ids:
                continue
            if action == "delete":
                with console.status(f"Deleting {sender}..."):
                    batch_delete(service, ids)
            else:
                with console.status(f"Trashing {sender}..."):
                    batch_modify(service, ids, add_labels=["TRASH"], remove_labels=["INBOX"])
            _remove_sender_from_cache(sender)
        console.print(f"[green]Done.[/green]")


# ---------------------------------------------------------------------------
# Main menu
# ---------------------------------------------------------------------------

GITHUB_URL = "https://github.com/littlecrakage/gmail-cleaning-helper"
KOFI_URL = "https://ko-fi.com/crakage"

MENU_OPTIONS = {
    "1": ("Analyze senders (count emails per sender)", analyze_senders),
    "2": ("Search & bulk action (delete / trash / mark read)", search_and_act),
    "3": ("Unsubscribe from newsletters", bulk_unsubscribe),
    "4": ("List all labels", list_labels),
    "5": ("Inbox stats", inbox_stats),
    "6": ("View sender cache", view_cache),
    "7": ("Clear sender cache", lambda _: clear_cache()),
    "8": ("Contact / Feedback (GitHub)", lambda _: _open_url(GITHUB_URL)),
    "9": ("Support me (Ko-fi)", lambda _: _open_url(KOFI_URL)),
    "0": ("Exit", None),
}


def _open_url(url: str):
    console.print(f"[dim]Opening {url} ...[/dim]")
    webbrowser.open(url)


def main():
    console.print("[bold magenta]Gmail Helper[/bold magenta]", justify="center")
    console.print()
    console.print("[bold red on white] ⚠  BIG FAT WARNING  ⚠ [/bold red on white]", justify="center")
    console.print("[bold red]THIS TOOL CAN PERMANENTLY DELETE EMAILS.[/bold red]", justify="center")
    console.print("[bold red]DELETED EMAILS ARE GONE FOREVER AND CANNOT BE RECOVERED.[/bold red]", justify="center")
    console.print("[bold red]USE AT YOUR OWN RISK.[/bold red]", justify="center")
    console.print()
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
    def _unsubscribe(svc): bulk_unsubscribe(svc, creds)
    def _view_cache(svc): view_cache(svc, creds)

    menu = {
        **MENU_OPTIONS,
        "1": ("Analyze senders (count emails per sender)", _analyze),
        "3": ("Unsubscribe from newsletters", _unsubscribe),
        "6": ("View sender cache", _view_cache),
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
