"""Thin wrapper over the Telegram Bot API using httpx."""

from __future__ import annotations

import httpx

BASE_URL = "https://api.telegram.org/bot{token}"


def _url(token: str, method: str) -> str:
    return f"{BASE_URL.format(token=token)}/{method}"


def send_message(token: str, chat_id: str, text: str, parse_mode: str = "HTML") -> dict:
    """Send a message via Telegram. Returns the API response."""
    resp = httpx.post(
        _url(token, "sendMessage"),
        json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def set_webhook(token: str, url: str, secret_token: str = "") -> dict:
    """Register a webhook URL with Telegram."""
    payload: dict = {"url": url}
    if secret_token:
        payload["secret_token"] = secret_token
    resp = httpx.post(
        _url(token, "setWebhook"),
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def delete_webhook(token: str) -> dict:
    """Remove the current webhook."""
    resp = httpx.post(
        _url(token, "deleteWebhook"),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_me(token: str) -> dict:
    """Get bot info — useful for verifying the token works."""
    resp = httpx.get(_url(token, "getMe"), timeout=15)
    resp.raise_for_status()
    return resp.json()
