"""CLI interface for GreekApp."""

from __future__ import annotations

from pathlib import Path

import click
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

load_dotenv()

from greekapp.db import get_connection, init_db, fetchall_dicts, fetchone_dict
from greekapp.importer import import_csv
from greekapp.srs import CardState, load_due_cards, record_review

console = Console()


@click.group()
def cli() -> None:
    """Greek vocabulary trainer with spaced repetition."""
    init_db()


@cli.command()
@click.argument("csv_path", type=click.Path(exists=True, path_type=Path))
def load(csv_path: Path) -> None:
    """Import vocabulary from a CSV file."""
    conn = get_connection()
    result = import_csv(conn, csv_path)
    conn.close()
    console.print(f"[green]Added {result['added']} words[/], skipped {result['skipped']}")


@cli.command()
@click.option("-n", "--count", default=20, help="Max cards to review")
def review(count: int) -> None:
    """Start a review session for due cards."""
    conn = get_connection()
    cards = load_due_cards(conn, limit=count)

    if not cards:
        console.print("[green]Nothing due! Come back later.[/]")
        conn.close()
        return

    console.print(f"\n[bold]{len(cards)} cards due[/]\n")

    for i, card in enumerate(cards, 1):
        console.print(f"[dim]({i}/{len(cards)})[/]")
        console.print(f"  [bold cyan]{card.greek}[/]")
        click.pause("  Press any key to reveal...")
        console.print(f"  [bold green]{card.english}[/]\n")

        while True:
            raw = click.prompt(
                "  Rate (0=blank, 1-2=wrong, 3=hard, 4=good, 5=easy)",
                type=int,
            )
            if 0 <= raw <= 5:
                break
            console.print("  [red]Enter 0-5[/]")

        card = record_review(conn, card, raw)
        console.print(f"  Next review in [yellow]{card.interval:.0f} days[/]\n")

    conn.close()
    console.print("[green]Session complete![/]")


@cli.command()
@click.option("--tag", default=None, help="Filter by tag")
def words(tag: str | None) -> None:
    """List all loaded vocabulary."""
    conn = get_connection()
    if tag:
        rows = fetchall_dicts(
            conn,
            "SELECT greek, english, tags FROM words WHERE tags LIKE ? ORDER BY greek",
            (f"%{tag}%",),
        )
    else:
        rows = fetchall_dicts(
            conn,
            "SELECT greek, english, tags FROM words ORDER BY greek",
        )

    table = Table(title=f"Vocabulary ({len(rows)} words)")
    table.add_column("Greek", style="cyan")
    table.add_column("English", style="green")
    table.add_column("Tags", style="dim")
    for row in rows:
        table.add_row(row["greek"], row["english"], row["tags"])

    console.print(table)
    conn.close()


@cli.command()
def stats() -> None:
    """Show learning progress."""
    conn = get_connection()

    total = fetchone_dict(conn, "SELECT COUNT(*) AS cnt FROM words")["cnt"]
    reviewed = fetchone_dict(conn, "SELECT COUNT(DISTINCT word_id) AS cnt FROM reviews")["cnt"]
    total_reviews = fetchone_dict(conn, "SELECT COUNT(*) AS cnt FROM reviews")["cnt"]

    mastered = fetchone_dict(conn, """
        SELECT COUNT(*) AS cnt FROM (
            SELECT word_id, interval
            FROM reviews r1
            WHERE reviewed_at = (
                SELECT MAX(reviewed_at) FROM reviews r2 WHERE r2.word_id = r1.word_id
            )
            AND interval >= 21
        ) sub
    """)["cnt"]

    console.print(f"\n[bold]Progress[/]")
    console.print(f"  Total words:    {total}")
    console.print(f"  Seen at least once: {reviewed}")
    console.print(f"  Mastered (21d+):    {mastered}")
    console.print(f"  Total reviews:  {total_reviews}\n")

    conn.close()


# --- Messaging commands ---


@cli.command()
def report() -> None:
    """Show a learning progress report."""
    from greekapp.report import generate_report

    conn = get_connection()
    text = generate_report(conn)
    conn.close()
    console.print(text)


@cli.command()
def send() -> None:
    """Send a message now (manual trigger for testing)."""
    from greekapp.config import Config
    from greekapp.messenger import compose_and_send

    config = Config.from_env()
    if not config.telegram_bot_token or not config.telegram_chat_id:
        console.print("[red]Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env[/]")
        return
    if not config.anthropic_api_key:
        console.print("[red]Set ANTHROPIC_API_KEY in .env[/]")
        return

    conn = get_connection()
    console.print("[dim]Composing message...[/]")
    result = compose_and_send(conn, config)
    conn.close()

    if "error" in result:
        console.print(f"[red]{result['error']}[/]")
        return

    console.print(f"\n[green]Sent:[/] {result['message']}")
    console.print(f"[dim]Words: {', '.join(w['greek'] for w in result['words'])}[/]")


@cli.command()
def cron() -> None:
    """Run one cron cycle (poll messages + maybe send). Same as what Render runs."""
    from greekapp.cron import run
    run()


@cli.command()
@click.option("--port", default=5000, help="Port to serve on")
def serve(port: int) -> None:
    """Run the webhook server locally."""
    from greekapp.webhook import app

    console.print(f"[bold]Starting webhook server on port {port}[/]")
    console.print("[dim]Use ngrok or similar to expose for Telegram webhooks[/]")
    app.run(host="0.0.0.0", port=port, debug=True)


def _handle_bot_command(text: str, conn, config) -> None:
    """Handle /slash commands in poll mode."""
    from greekapp.telegram import send_message
    from greekapp.srs import load_due_cards

    cmd = text.strip().split()[0].lower()

    if cmd == "/report":
        from greekapp.report import generate_report
        report_text = generate_report(conn)
        send_message(config.telegram_bot_token, config.telegram_chat_id, report_text, parse_mode="")
        console.print(f"[green]Sent report[/]")

    elif cmd == "/stats":
        total = fetchone_dict(conn, "SELECT COUNT(*) AS cnt FROM words")["cnt"]
        reviewed = fetchone_dict(conn, "SELECT COUNT(DISTINCT word_id) AS cnt FROM reviews")["cnt"]
        total_reviews = fetchone_dict(conn, "SELECT COUNT(*) AS cnt FROM reviews")["cnt"]
        text = f"Total words: {total}\nSeen: {reviewed}\nReviews: {total_reviews}"
        send_message(config.telegram_bot_token, config.telegram_chat_id, text, parse_mode="")
        console.print(f"[green]Sent stats[/]")

    elif cmd == "/due":
        due = load_due_cards(conn, limit=100)
        new = sum(1 for c in due if c.last_review is None)
        review = len(due) - new
        text = f"Due now: {len(due)} words ({new} new, {review} review)"
        send_message(config.telegram_bot_token, config.telegram_chat_id, text, parse_mode="")
        console.print(f"[green]Sent due count[/]")

    elif cmd == "/start":
        send_message(
            config.telegram_bot_token, config.telegram_chat_id,
            "Γεια σου! Commands: /report, /stats, /due", parse_mode="",
        )
        console.print(f"[green]Sent welcome[/]")

    else:
        send_message(
            config.telegram_bot_token, config.telegram_chat_id,
            "Commands: /report, /stats, /due", parse_mode="",
        )


@cli.command()
def poll() -> None:
    """Poll for Telegram replies and respond (local dev, no webhook needed)."""
    import time
    import httpx
    from greekapp.config import Config
    from greekapp.assessor import assess_and_reply

    config = Config.from_env()
    if not config.telegram_bot_token or not config.anthropic_api_key:
        console.print("[red]Set TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, and ANTHROPIC_API_KEY in .env[/]")
        return

    # Delete any existing webhook so polling works
    httpx.post(
        f"https://api.telegram.org/bot{config.telegram_bot_token}/deleteWebhook",
        timeout=10,
    )

    console.print("[bold]Polling for messages... (Ctrl+C to stop)[/]")
    offset = 0
    while True:
        try:
            resp = httpx.get(
                f"https://api.telegram.org/bot{config.telegram_bot_token}/getUpdates",
                params={"offset": offset, "timeout": 30},
                timeout=35,
            )
            data = resp.json()
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                text = msg.get("text", "")
                chat_id = str(msg.get("chat", {}).get("id", ""))

                if chat_id != config.telegram_chat_id or not text:
                    continue

                console.print(f"\n[cyan]Received:[/] {text}")
                conn = get_connection()
                try:
                    if text.startswith("/"):
                        _handle_bot_command(text, conn, config)
                    else:
                        result = assess_and_reply(conn, config, text)
                        console.print(f"[green]Replied:[/] {result.get('reply', '')}")
                        if result.get("assessments"):
                            for a in result["assessments"]:
                                console.print(f"  [dim]{a['greek']}: quality={a['quality']} — {a.get('reasoning', '')}[/]")
                        if result.get("corrections"):
                            for c in result["corrections"]:
                                console.print(f"  [yellow]Correction: {c.get('wrong', '')} → {c.get('correct', '')} ({c.get('type', '')})[/]")
                finally:
                    conn.close()
        except KeyboardInterrupt:
            console.print("\n[dim]Stopped.[/]")
            break
        except Exception as e:
            console.print(f"[red]Error: {e}[/]")
            time.sleep(5)


@cli.command("setup-webhook")
@click.argument("url")
def setup_webhook(url: str) -> None:
    """Register a webhook URL with Telegram.

    URL should be your public HTTPS endpoint, e.g.:
    greek setup-webhook https://your-app.onrender.com/webhook
    """
    from greekapp.config import Config
    from greekapp.telegram import set_webhook

    config = Config.from_env()
    if not config.telegram_bot_token:
        console.print("[red]Set TELEGRAM_BOT_TOKEN in .env[/]")
        return

    result = set_webhook(
        config.telegram_bot_token,
        url,
        secret_token=config.webhook_secret,
    )
    if result.get("ok"):
        console.print(f"[green]Webhook set to {url}[/]")
    else:
        console.print(f"[red]Failed: {result}[/]")


if __name__ == "__main__":
    cli()
