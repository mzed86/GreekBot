"""Render deployment entry point.

Runs a minimal Flask server for health checks (keeps the free tier alive)
and a background thread that polls Telegram + sends proactive messages
every 20 minutes.
"""

from __future__ import annotations

import os
import threading
import time
import traceback

from flask import Flask, jsonify

app = Flask(__name__)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


def _self_ping():
    """Ping our own health endpoint every 10 minutes to prevent Render free tier spin-down."""
    import httpx

    url = os.environ.get("RENDER_EXTERNAL_URL", "")
    if not url:
        return

    while True:
        time.sleep(10 * 60)  # every 10 minutes
        try:
            httpx.get(f"{url}/health", timeout=10)
        except Exception:
            pass


def _cron_loop():
    """Background loop — runs the cron logic every 20 minutes."""
    # Wait a bit for the server to fully start
    time.sleep(10)

    while True:
        try:
            from greekapp.cron import run
            run()
        except Exception:
            traceback.print_exc()

        # Sleep 20 minutes
        time.sleep(20 * 60)


def main():
    # Start the cron loop in a background thread
    cron_thread = threading.Thread(target=_cron_loop, daemon=True)
    cron_thread.start()

    # Start self-ping to keep Render free tier alive
    ping_thread = threading.Thread(target=_self_ping, daemon=True)
    ping_thread.start()

    # Run Flask on the port Render provides
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
