"""Tests for the probabilistic send scheduler."""

import tempfile
from datetime import datetime
from pathlib import Path

import greekapp.db as db_module
from greekapp.db import execute, get_connection, init_db
from greekapp.config import Config
from greekapp.scheduler import _sends_today, _time_weight, should_send_now

_ORIG_DB_PATH = db_module.DB_PATH


def _config(**overrides) -> Config:
    defaults = dict(
        timezone="Europe/London",
        daily_target=2,
        active_hours_start=9,
        active_hours_end=21,
    )
    defaults.update(overrides)
    return Config(**defaults)


def setup_function():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_module.DB_PATH = Path(tmp.name)
    init_db()


def teardown_function():
    tmp_path = db_module.DB_PATH
    db_module.DB_PATH = _ORIG_DB_PATH
    if tmp_path.exists():
        tmp_path.unlink()


# --- _time_weight ---

def test_time_weight_before_active_hours():
    assert _time_weight(5, _config()) == 0.0


def test_time_weight_after_active_hours():
    assert _time_weight(22, _config()) == 0.0


def test_time_weight_morning_peak():
    assert _time_weight(9, _config()) == 1.5


def test_time_weight_evening_peak():
    assert _time_weight(19, _config()) == 1.5


def test_time_weight_midday():
    assert _time_weight(12, _config()) == 0.8


def test_time_weight_other_active():
    assert _time_weight(16, _config()) == 1.0


# --- should_send_now ---

def test_should_send_outside_hours(monkeypatch):
    """Outside active hours → always False."""
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(
        "greekapp.scheduler.datetime",
        type("MockDT", (), {
            "now": staticmethod(lambda tz=None: datetime(2024, 6, 15, 3, 0, tzinfo=ZoneInfo("Europe/London"))),
        }),
    )
    conn = get_connection()
    assert should_send_now(conn, _config()) is False
    conn.close()


def test_should_send_target_reached(monkeypatch):
    """Already hit daily target → False."""
    from zoneinfo import ZoneInfo
    now = datetime(2024, 6, 15, 12, 0, tzinfo=ZoneInfo("Europe/London"))
    monkeypatch.setattr(
        "greekapp.scheduler.datetime",
        type("MockDT", (), {
            "now": staticmethod(lambda tz=None: now),
        }),
    )
    conn = get_connection()
    today_str = now.strftime("%Y-%m-%d")
    # Insert 2 send_log entries (daily_target=2)
    execute(conn, "INSERT INTO send_log (sent_date) VALUES (?)", (today_str,))
    execute(conn, "INSERT INTO send_log (sent_date) VALUES (?)", (today_str,))
    conn.commit()
    assert should_send_now(conn, _config()) is False
    conn.close()


def test_should_send_urgency_boost(monkeypatch):
    """When running low on slots, probability should be at least 0.5."""
    from zoneinfo import ZoneInfo
    # 20:00 with 0 sends = 1 remaining slot for 2 messages → urgent
    now = datetime(2024, 6, 15, 20, 0, tzinfo=ZoneInfo("Europe/London"))
    monkeypatch.setattr(
        "greekapp.scheduler.datetime",
        type("MockDT", (), {
            "now": staticmethod(lambda tz=None: now),
        }),
    )
    # Force random to return 0.49 — should still send due to urgency boost (prob >= 0.5)
    monkeypatch.setattr("greekapp.scheduler.random.random", lambda: 0.49)
    conn = get_connection()
    assert should_send_now(conn, _config()) is True
    conn.close()


# --- _sends_today ---

def test_sends_today_counts(monkeypatch):
    from zoneinfo import ZoneInfo
    now = datetime(2024, 6, 15, 12, 0, tzinfo=ZoneInfo("Europe/London"))
    monkeypatch.setattr(
        "greekapp.scheduler.datetime",
        type("MockDT", (), {
            "now": staticmethod(lambda tz=None: now),
        }),
    )
    conn = get_connection()
    today_str = now.strftime("%Y-%m-%d")
    execute(conn, "INSERT INTO send_log (sent_date) VALUES (?)", (today_str,))
    conn.commit()
    assert _sends_today(conn, _config()) == 1
    conn.close()


def test_sends_today_ignores_other_days(monkeypatch):
    from zoneinfo import ZoneInfo
    now = datetime(2024, 6, 15, 12, 0, tzinfo=ZoneInfo("Europe/London"))
    monkeypatch.setattr(
        "greekapp.scheduler.datetime",
        type("MockDT", (), {
            "now": staticmethod(lambda tz=None: now),
        }),
    )
    conn = get_connection()
    execute(conn, "INSERT INTO send_log (sent_date) VALUES (?)", ("2024-06-14",))
    conn.commit()
    assert _sends_today(conn, _config()) == 0
    conn.close()
