"""Flask webhook server for Telegram integration and cron triggers."""

from __future__ import annotations

import hmac
import json
import logging

from flask import Flask, request, jsonify

logger = logging.getLogger(__name__)

from greekapp.config import Config
from greekapp.db import get_connection, init_db, fetchone_dict, fetchall_dicts
from greekapp.assessor import assess_and_reply
from greekapp.messenger import compose_and_send
from greekapp.scheduler import should_send_now

app = Flask(__name__)


def _get_config() -> Config:
    return Config.from_env()


@app.before_request
def _ensure_db():
    init_db()


@app.route("/health", methods=["GET"])
def health():
    """Render health check endpoint."""
    return jsonify({"status": "ok"})


@app.route("/webhook", methods=["POST"])
def webhook():
    """Handle incoming Telegram messages."""
    config = _get_config()

    # Verify Telegram secret token if configured
    if config.webhook_secret:
        token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(token, config.webhook_secret):
            return jsonify({"error": "unauthorized"}), 403

    data = request.get_json(silent=True)
    if not data or "message" not in data:
        return jsonify({"ok": True})

    message = data["message"]
    text = message.get("text", "")
    chat_id = str(message.get("chat", {}).get("id", ""))

    # Only respond to messages from the configured chat
    if chat_id != config.telegram_chat_id:
        return jsonify({"ok": True})

    if not text:
        return jsonify({"ok": True})

    # Handle bot commands
    if text.startswith("/"):
        return _handle_command(text, config)

    # Regular message — assess and reply
    conn = get_connection()
    try:
        result = assess_and_reply(conn, config, text)
    finally:
        conn.close()

    return jsonify({"ok": True, "reply": result.get("reply", "")})


def _handle_command(text: str, config: Config):
    """Handle /slash commands."""
    # Strip @botname suffix that Telegram appends (e.g. /report@MyBot -> /report)
    cmd = text.strip().split()[0].lower().split("@")[0]
    conn = get_connection()

    try:
        if cmd == "/stats":
            return _cmd_stats(conn, config)
        elif cmd == "/due":
            return _cmd_due(conn, config)
        elif cmd == "/report":
            return _cmd_report(conn, config)
        elif cmd == "/start":
            return _cmd_start(config)
        else:
            from greekapp.telegram import send_message
            send_message(
                config.telegram_bot_token,
                config.telegram_chat_id,
                "Commands: /report, /stats, /due, /start",
                parse_mode="",
            )
            return jsonify({"ok": True})
    except Exception:
        logger.exception("Command %s failed", cmd)
        try:
            from greekapp.telegram import send_message
            send_message(
                config.telegram_bot_token,
                config.telegram_chat_id,
                f"Sorry, the {cmd} command hit an error. Check the logs.",
                parse_mode="",
            )
        except Exception:
            logger.exception("Failed to send error message for command %s", cmd)
        return jsonify({"ok": True})
    finally:
        conn.close()


def _cmd_stats(conn, config: Config):
    """Show learning stats."""
    from greekapp.telegram import send_message

    total = fetchone_dict(conn, "SELECT COUNT(*) AS cnt FROM words")
    reviewed = fetchone_dict(conn, "SELECT COUNT(DISTINCT word_id) AS cnt FROM reviews")
    total_reviews = fetchone_dict(conn, "SELECT COUNT(*) AS cnt FROM reviews")
    messages_out = fetchone_dict(
        conn, "SELECT COUNT(*) AS cnt FROM messages WHERE direction = 'out'"
    )
    messages_in = fetchone_dict(
        conn, "SELECT COUNT(*) AS cnt FROM messages WHERE direction = 'in'"
    )

    text = (
        f"Total words: {total['cnt']}\n"
        f"Words seen: {reviewed['cnt']}\n"
        f"Total reviews: {total_reviews['cnt']}\n"
        f"Messages sent: {messages_out['cnt']}\n"
        f"Messages received: {messages_in['cnt']}"
    )

    send_message(config.telegram_bot_token, config.telegram_chat_id, text, parse_mode="")
    return jsonify({"ok": True})


def _cmd_due(conn, config: Config):
    """Show due word count."""
    from greekapp.srs import load_due_cards
    from greekapp.telegram import send_message

    due = load_due_cards(conn, limit=100)
    new = sum(1 for c in due if c.last_review is None)
    review = len(due) - new

    text = f"Due now: {len(due)} words ({new} new, {review} review)"
    send_message(config.telegram_bot_token, config.telegram_chat_id, text, parse_mode="")
    return jsonify({"ok": True})


def _cmd_report(conn, config: Config):
    """Send a full learning report."""
    from greekapp.report import generate_report
    from greekapp.telegram import send_message

    text = generate_report(conn)
    send_message(config.telegram_bot_token, config.telegram_chat_id, text, parse_mode="")
    return jsonify({"ok": True})


def _cmd_start(config: Config):
    """Welcome message for /start."""
    from greekapp.telegram import send_message

    send_message(
        config.telegram_bot_token,
        config.telegram_chat_id,
        "Γεια σου! I'm your Greek practice companion. I'll text you throughout the day mixing Greek into casual conversation. Just reply naturally!",
        parse_mode="",
    )
    return jsonify({"ok": True})


@app.route("/cron/send", methods=["POST"])
def cron_send():
    """Endpoint hit by the Render cron job every 30 minutes."""
    config = _get_config()

    # Auth check — cron sends a secret header
    if config.webhook_secret:
        auth = request.headers.get("Authorization", "")
        if not hmac.compare_digest(auth, f"Bearer {config.webhook_secret}"):
            return jsonify({"error": "unauthorized"}), 403

    conn = get_connection()
    try:
        if not should_send_now(conn, config):
            return jsonify({"sent": False, "reason": "scheduler said not now"})

        result = compose_and_send(conn, config)
        if "error" in result:
            return jsonify({"sent": False, "error": result["error"]}), 500

        return jsonify({"sent": True, "message": result["message"]})
    finally:
        conn.close()
