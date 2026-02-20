"""Probabilistic send scheduler.

Cron fires every 20 minutes. The scheduler decides whether *this* invocation
should actually send a message, creating natural timing variation.
"""

from __future__ import annotations

import random
from datetime import datetime

from zoneinfo import ZoneInfo

from greekapp.config import Config
from greekapp.db import fetchone_dict


def _sends_today(conn, config: Config) -> int:
    """Count how many messages have been sent today."""
    tz = ZoneInfo(config.timezone)
    today = datetime.now(tz).strftime("%Y-%m-%d")
    row = fetchone_dict(
        conn,
        "SELECT COUNT(*) AS cnt FROM send_log WHERE sent_date = ?",
        (today,),
    )
    return row["cnt"] if row else 0


def _time_weight(hour: int, config: Config) -> float:
    """Return a weight for how likely we are to send at this hour.

    Morning (8-10) and evening (18-20) are weighted higher.
    """
    scheduling = {}  # Could pull from profile.yaml if loaded
    if hour < config.active_hours_start or hour >= config.active_hours_end:
        return 0.0

    # Peak windows: morning and evening
    if 8 <= hour <= 10:
        return 1.5
    if 18 <= hour <= 20:
        return 1.5
    # Midday is fine but less likely
    if 11 <= hour <= 14:
        return 0.8
    # Other active hours
    return 1.0


def should_send_now(conn, config: Config) -> bool:
    """Decide probabilistically whether to send a message right now.

    Called every 20 minutes by the cron job. Returns True if we should send.
    """
    tz = ZoneInfo(config.timezone)
    now = datetime.now(tz)
    hour = now.hour

    # Hard boundary: outside active hours → never send
    if hour < config.active_hours_start or hour >= config.active_hours_end:
        return False

    # Already hit daily target → done
    sent_today = _sends_today(conn, config)
    if sent_today >= config.daily_target:
        return False

    # Calculate base probability
    # Active window = (end - start) hours = N slots of 20 min each
    active_slots = (config.active_hours_end - config.active_hours_start) * 3
    remaining_target = config.daily_target - sent_today
    remaining_slots = (config.active_hours_end - hour) * 3

    if remaining_slots <= 0:
        return False

    # Base probability: spread remaining messages over remaining slots
    base_prob = remaining_target / remaining_slots

    # Apply time-of-day weighting
    weight = _time_weight(hour, config)
    prob = min(base_prob * weight, 0.9)  # Cap at 90% to keep some randomness

    # If we're running low on slots, increase urgency
    if remaining_slots <= remaining_target * 2:
        prob = max(prob, 0.5)

    return random.random() < prob
