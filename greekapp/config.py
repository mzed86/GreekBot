"""Configuration for GreekApp.

Settings are loaded from environment variables or a .env file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Config:
    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # AI (for generating natural messages)
    anthropic_api_key: str = ""

    # Database (PostgreSQL URL for production, empty = SQLite for local dev)
    database_url: str = ""

    # Scheduling
    timezone: str = "Europe/London"
    daily_target: int = 2  # messages per day
    active_hours_start: int = 9   # earliest hour to send (24h)
    active_hours_end: int = 21    # latest hour to send (24h)

    # Webhook security
    webhook_secret: str = ""

    # SRS tuning
    new_cards_per_day: int = 10
    review_cap: int = 50

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            database_url=os.getenv("DATABASE_URL", ""),
            timezone=os.getenv("TIMEZONE", "Europe/London"),
            daily_target=int(os.getenv("DAILY_TARGET", "2")),
            active_hours_start=int(os.getenv("ACTIVE_HOURS_START", "9")),
            active_hours_end=int(os.getenv("ACTIVE_HOURS_END", "21")),
            webhook_secret=os.getenv("WEBHOOK_SECRET", ""),
            new_cards_per_day=int(os.getenv("NEW_CARDS_PER_DAY", "10")),
            review_cap=int(os.getenv("REVIEW_CAP", "50")),
        )
