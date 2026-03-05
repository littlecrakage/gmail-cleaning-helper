"""Gmail Helper - Interactive CLI to manage your Gmail inbox."""

import sys
from collections import defaultdict
from typing import Optional

from googleapiclient.discovery import build
from rich import print as rprint
from rich.console import Console
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

from auth import get_credentials

console = Console()


# ---------------------------------------------------------------------------
# Gmail API helpers
# ---------------------------------------------------------------------------

def build_service():
    creds = get_credentials()
    return build("gmail", "v1", credentials=creds)


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


def get_message_headers(service, msg_id: str) -> dict:
    """Return a dict of selected headers for a message."""
    msg = service.users().messages().get(
        userId="me", id=msg_id, format="metadata",
        metadataHeaders=["From", "Subject", "Date"]
    ).execute()
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    headers["id"] = msg_id
    headers["labelIds"] = msg.get("labelIds", [])
    return headers


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
# Feature: Sender analysis
# ---------------------------------------------------------------------------

def analyze_senders(service):
    console.print("\n[bold cyan]Analyzing senders...[/bold cyan]")
    limit = IntPrompt.ask("Max emails to scan", default=500)

    query = Prompt.ask("Optional Gmail search filter (leave blank for all)", default="")
    with console.status("Fetching messages..."):
        messages = fetch_messages(service, query=query, max_results=limit)

    if not messages:
        console.print("[yellow]No messages found.[/yellow]")
        return

    console.print(f"Fetched [bold]{len(messages)}[/bold] messages. Reading headers...")

    sender_counts: dict[str, int] = defaultdict(int)
    sender_ids: dict[str, list[str]] = defaultdict(list)

    with console.status("Reading headers...") as status:
        for i, msg in enumerate(messages):
            if i % 50 == 0:
                status.update(f"Reading headers... {i}/{len(messages)}")
            headers = get_message_headers(service, msg["id"])
            sender = headers.get("From", "Unknown")
            sender_counts[sender] += 1
            sender_ids[sender].append(msg["id"])

    # Sort by count descending
    sorted_senders = sorted(sender_counts.items(), key=lambda x: x[1], reverse=True)

    top_n = IntPrompt.ask("How many top senders to display", default=30)

    table = Table(title=f"Top {top_n} Senders", show_lines=False)
    table.add_column("#", style="dim", width=4)
    table.add_column("Sender", style="cyan", no_wrap=False)
    table.add_column("Emails", justify="right", style="green")

    for idx, (sender, count) in enumerate(sorted_senders[:top_n], 1):
        table.add_row(str(idx), sender, str(count))

    console.print(table)

    # Optionally take action
    _sender_action_menu(service, sorted_senders[:top_n], sender_ids)


def _sender_action_menu(service, sorted_senders: list, sender_ids: dict):
    """Ask the user if they want to act on emails from a specific sender."""
    while True:
        console.print("\n[bold]Actions:[/bold] enter a sender # to act on it, or [bold]0[/bold] to go back")
        choice = IntPrompt.ask("Choice", default=0)
        if choice == 0:
            return

        if choice < 1 or choice > len(sorted_senders):
            console.print("[red]Invalid choice.[/red]")
            continue

        sender, count = sorted_senders[choice - 1]
        ids = sender_ids[sender]
        console.print(f"\nSelected: [cyan]{sender}[/cyan] ({count} emails)")

        action = Prompt.ask(
            "Action",
            choices=["delete", "trash", "mark_read", "mark_unread", "back"],
            default="back",
        )

        if action == "back":
            continue
        elif action == "delete":
            if Confirm.ask(f"Permanently delete all {count} emails from this sender?"):
                with console.status("Deleting..."):
                    n = batch_delete(service, ids)
                console.print(f"[green]Deleted {n} emails.[/green]")
                return
        elif action == "trash":
            if Confirm.ask(f"Move {count} emails from this sender to Trash?"):
                with console.status("Moving to Trash..."):
                    n = batch_modify(service, ids, add_labels=["TRASH"], remove_labels=["INBOX"])
                console.print(f"[green]Moved {n} emails to Trash.[/green]")
                return
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


# ---------------------------------------------------------------------------
# Main menu
# ---------------------------------------------------------------------------

MENU_OPTIONS = {
    "1": ("Analyze senders (count emails per sender)", analyze_senders),
    "2": ("Search & bulk action (delete / trash / mark read)", search_and_act),
    "3": ("List all labels", list_labels),
    "4": ("Inbox stats", inbox_stats),
    "0": ("Exit", None),
}


def main():
    console.print("[bold magenta]Gmail Helper[/bold magenta]", justify="center")
    console.print("[dim]Connecting to Gmail...[/dim]")

    try:
        service = build_service()
    except FileNotFoundError as e:
        console.print(f"\n[red]Error:[/red] {e}")
        sys.exit(1)
    except Exception as e:
        console.print(f"\n[red]Authentication failed:[/red] {e}")
        sys.exit(1)

    console.print("[green]Connected.[/green]\n")

    while True:
        console.print("\n[bold]Main Menu[/bold]")
        for key, (label, _) in MENU_OPTIONS.items():
            console.print(f"  [bold cyan]{key}[/bold cyan]  {label}")

        choice = Prompt.ask("\nChoice", choices=list(MENU_OPTIONS.keys()))

        if choice == "0":
            console.print("Bye!")
            break

        label, fn = MENU_OPTIONS[choice]
        try:
            fn(service)
        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted, back to menu.[/yellow]")
        except Exception as e:
            console.print(f"\n[red]Error:[/red] {e}")


if __name__ == "__main__":
    main()
