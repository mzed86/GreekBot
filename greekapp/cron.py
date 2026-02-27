"""Cron entry point — runs every 20 minutes on Render free tier.

Handles everything:
1. Poll Telegram for any new incoming messages since last check
2. Process each one (assess understanding, reply)
3. Decide if we should send a proactive message
"""

from __future__ import annotations

import json
import sys

import httpx

from greekapp.config import Config
from greekapp.db import get_connection, init_db, execute, fetchone_dict


def _get_last_update_id(conn) -> int:
    """Get the last processed Telegram update_id from the DB."""
    # Store in a simple key-value style using profile_notes with a special category
    row = fetchone_dict(
        conn,
        "SELECT content FROM profile_notes WHERE category = 'system:last_update_id' ORDER BY created_at DESC LIMIT 1",
    )
    return int(row["content"]) if row else 0


def _set_last_update_id(conn, update_id: int) -> None:
    """Store the last processed Telegram update_id."""
    execute(
        conn,
        "INSERT INTO profile_notes (category, content) VALUES (?, ?)",
        ("system:last_update_id", str(update_id)),
    )
    conn.commit()


def _poll_telegram(config: Config, last_update_id: int) -> list[dict]:
    """Fetch new messages from Telegram using getUpdates (long-poll disabled for cron)."""
    resp = httpx.get(
        f"https://api.telegram.org/bot{config.telegram_bot_token}/getUpdates",
        params={"offset": last_update_id + 1, "timeout": 0},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("result", [])


def run() -> None:
    """Main cron entry point."""
    config = Config.from_env()
    if not config.telegram_bot_token or not config.anthropic_api_key:
        print("Missing TELEGRAM_BOT_TOKEN or ANTHROPIC_API_KEY")
        sys.exit(1)

    init_db()

    # Auto-import vocabulary if the words table is empty (first deploy)
    conn_check = get_connection()
    word_count = fetchone_dict(conn_check, "SELECT COUNT(*) AS cnt FROM words")["cnt"]
    if word_count == 0:
        from pathlib import Path
        from greekapp.importer import import_csv
        csv_path = Path(__file__).resolve().parent.parent / "data" / "quizlet_vocabulary.csv"
        if csv_path.exists():
            result = import_csv(conn_check, csv_path)
            print(f"Auto-imported vocabulary: {result['added']} added, {result['skipped']} skipped")
        else:
            print("Warning: No vocabulary CSV found at data/quizlet_vocabulary.csv")
    conn_check.close()

    # Ensure no webhook is set — we use polling
    try:
        httpx.post(
            f"https://api.telegram.org/bot{config.telegram_bot_token}/deleteWebhook",
            timeout=10,
        )
    except Exception:
        pass

    conn = get_connection()

    try:
        # --- Step 1: Poll Telegram for new messages ---
        last_update_id = _get_last_update_id(conn)
        try:
            updates = _poll_telegram(config, last_update_id)
        except Exception as exc:
            print(f"Telegram polling failed, skipping to proactive: {exc}")
            updates = []

        # --- Step 2: Process each message individually ---
        for update in updates:
            last_update_id = update["update_id"]
            try:
                msg = update.get("message", {})
                text = msg.get("text", "")
                chat_id = str(msg.get("chat", {}).get("id", ""))

                if chat_id != config.telegram_chat_id or not text:
                    continue

                print(f"Processing: {text[:60]}")

                if text.startswith("/"):
                    _handle_command(text, conn, config)
                else:
                    from greekapp.assessor import assess_and_reply
                    result = assess_and_reply(conn, config, text)
                    print(f"  Replied: {result.get('reply', '')[:60]}")
                    if result.get("assessments"):
                        for a in result["assessments"]:
                            print(f"  {a['greek']}: quality={a['quality']}")
            except Exception as exc:
                print(f"Error processing update {last_update_id}: {exc}")

        # Save our position so we don't reprocess messages (including failed ones)
        if updates:
            _set_last_update_id(conn, last_update_id)
            print(f"Processed {len(updates)} updates")

        # --- Step 3: Maybe send a proactive message ---
        try:
            from greekapp.scheduler import should_send_now
            from greekapp.messenger import (
                compose_and_send,
                compose_recall_and_send,
                should_use_recall,
            )

            if should_send_now(conn, config):
                # Decide between teaching mode and recall mode
                if should_use_recall(conn):
                    print("Scheduler says send (recall mode)...")
                    result = compose_recall_and_send(conn, config)
                else:
                    print("Scheduler says send (teaching mode)...")
                    result = compose_and_send(conn, config)

                if "error" in result:
                    print(f"  Error: {result['error']}")
                else:
                    mode = result.get("mode", "teaching")
                    print(f"  Sent [{mode}]: {result['message'][:60]}")
            else:
                print("Scheduler says skip this slot")
        except Exception as exc:
            print(f"Proactive send failed: {exc}")

        # --- Step 4: Maybe send weekly digest ---
        _maybe_send_weekly_digest(conn, config)

    finally:
        conn.close()


def _maybe_send_weekly_digest(conn, config: Config) -> None:
    """Send a weekly progress digest on Sunday evening (18:00-18:29 London time)."""
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(config.timezone)
        now = datetime.now(tz)

        # Only on Sundays, 18:00-18:19 (one cron window)
        if now.weekday() != 6 or now.hour != 18 or now.minute >= 20:
            return

        # Dedup key: weekly_digest:YYYY-WNN
        week_key = f"weekly_digest:{now.strftime('%G-W%V')}"
        existing = fetchone_dict(
            conn,
            "SELECT id FROM profile_notes WHERE category = ?",
            (week_key,),
        )
        if existing:
            return

        from greekapp.report import generate_report
        from greekapp.telegram import send_message

        report_text = f"--- Weekly Digest ---\n\n{generate_report(conn)}"
        send_message(config.telegram_bot_token, config.telegram_chat_id, report_text, parse_mode="")

        # Mark as sent
        execute(conn, "INSERT INTO profile_notes (category, content) VALUES (?, ?)", (week_key, "sent"))
        conn.commit()
        print(f"Sent weekly digest ({week_key})")
    except Exception as exc:
        print(f"Weekly digest failed: {exc}")


def _handle_command(text: str, conn, config: Config) -> None:
    """Handle /slash commands."""
    from greekapp.telegram import send_message
    from greekapp.srs import load_due_cards

    cmd = text.strip().split()[0].lower()

    if cmd == "/report":
        from greekapp.report import generate_report
        report_text = generate_report(conn)
        send_message(config.telegram_bot_token, config.telegram_chat_id, report_text, parse_mode="")

    elif cmd == "/stats":
        total = fetchone_dict(conn, "SELECT COUNT(*) AS cnt FROM words")["cnt"]
        reviewed = fetchone_dict(conn, "SELECT COUNT(DISTINCT word_id) AS cnt FROM reviews")["cnt"]
        total_reviews = fetchone_dict(conn, "SELECT COUNT(*) AS cnt FROM reviews")["cnt"]
        msg = f"Total words: {total}\nSeen: {reviewed}\nReviews: {total_reviews}"
        send_message(config.telegram_bot_token, config.telegram_chat_id, msg, parse_mode="")

    elif cmd == "/due":
        due = load_due_cards(conn, limit=100)
        new = sum(1 for c in due if c.last_review is None)
        review = len(due) - new
        msg = f"Due now: {len(due)} words ({new} new, {review} review)"
        send_message(config.telegram_bot_token, config.telegram_chat_id, msg, parse_mode="")

    elif cmd == "/know":
        _cmd_know_cron(text, conn, config, send_message)

    elif cmd == "/skip":
        _cmd_skip_cron(text, conn, config, send_message)

    elif cmd == "/start":
        send_message(
            config.telegram_bot_token, config.telegram_chat_id,
            "Γεια σου! I'm your Greek practice companion. Commands: /report, /stats, /due, /know, /skip",
            parse_mode="",
        )

    else:
        send_message(
            config.telegram_bot_token, config.telegram_chat_id,
            "Commands: /report, /stats, /due, /know, /skip",
            parse_mode="",
        )


def _find_word_cron(conn, greek_word: str):
    """Look up a word by exact match, then fuzzy match with/without article."""
    import re
    row = fetchone_dict(conn, "SELECT id, greek, english, tags FROM words WHERE greek = ?", (greek_word,))
    if row:
        return row
    for article in ["ο ", "η ", "το ", "οι ", "τα "]:
        row = fetchone_dict(conn, "SELECT id, greek, english, tags FROM words WHERE greek = ?", (f"{article}{greek_word}",))
        if row:
            return row
    bare = re.sub(r"^(ο|η|το|οι|τα|τον|την|του|της|των)\s+", "", greek_word)
    if bare != greek_word:
        row = fetchone_dict(conn, "SELECT id, greek, english, tags FROM words WHERE greek = ?", (bare,))
        if row:
            return row
    return None


def _cmd_know_cron(text, conn, config, send_message):
    """Mark a word as known via cron polling."""
    from greekapp.assessor import _get_word_card_state
    from greekapp.srs import record_review

    parts = text.strip().split(maxsplit=1)
    if len(parts) < 2:
        send_message(config.telegram_bot_token, config.telegram_chat_id,
                     "Usage: /know <greek word>", parse_mode="")
        return

    word = _find_word_cron(conn, parts[1].strip())
    if not word:
        send_message(config.telegram_bot_token, config.telegram_chat_id,
                     f"'{parts[1].strip()}' not found in vocabulary.", parse_mode="")
        return

    card = _get_word_card_state(conn, word["id"])
    record_review(conn, card, 5)
    send_message(config.telegram_bot_token, config.telegram_chat_id,
                 f"Marked '{word['greek']}' ({word['english']}) as known ✓", parse_mode="")


def _cmd_skip_cron(text, conn, config, send_message):
    """Remove a word from the review cycle via cron polling."""
    parts = text.strip().split(maxsplit=1)
    if len(parts) < 2:
        send_message(config.telegram_bot_token, config.telegram_chat_id,
                     "Usage: /skip <greek word>", parse_mode="")
        return

    word = _find_word_cron(conn, parts[1].strip())
    if not word:
        send_message(config.telegram_bot_token, config.telegram_chat_id,
                     f"'{parts[1].strip()}' not found in vocabulary.", parse_mode="")
        return

    current_tags = word.get("tags", "") or ""
    if "skip:manual" in current_tags:
        send_message(config.telegram_bot_token, config.telegram_chat_id,
                     f"'{word['greek']}' is already skipped.", parse_mode="")
        return

    new_tags = f"{current_tags},skip:manual" if current_tags else "skip:manual"
    execute(conn, "UPDATE words SET tags = ? WHERE id = ?", (new_tags, word["id"]))
    conn.commit()
    send_message(config.telegram_bot_token, config.telegram_chat_id,
                 f"Skipped '{word['greek']}' ({word['english']}) — won't appear again.", parse_mode="")


if __name__ == "__main__":
    run()
