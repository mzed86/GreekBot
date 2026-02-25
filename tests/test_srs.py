"""Tests for the SM-2 spaced repetition engine."""

import tempfile
from pathlib import Path

import greekapp.db as db_module
from greekapp.db import execute, get_connection, init_db
from greekapp.srs import CardState, DEFAULT_EASE, MIN_EASE, load_due_cards, next_state

_ORIG_DB_PATH = db_module.DB_PATH


def _card(**kw):
    defaults = dict(word_id=1, greek="γεια", english="hello")
    defaults.update(kw)
    return CardState(**defaults)


def test_first_correct_review_sets_interval_1():
    state = next_state(_card(), quality=4)
    assert state.interval == 1.0
    assert state.repetition == 1


def test_second_correct_review_sets_interval_6():
    card = _card(repetition=1, interval=1.0)
    state = next_state(card, quality=4)
    assert state.interval == 6.0
    assert state.repetition == 2


def test_failure_resets_repetition():
    card = _card(repetition=3, interval=15.0, ease_factor=2.5)
    state = next_state(card, quality=1)
    assert state.repetition == 0
    assert state.interval == 0.0


def test_ease_never_below_minimum():
    card = _card(ease_factor=MIN_EASE)
    state = next_state(card, quality=0)
    assert state.ease_factor >= MIN_EASE


def test_perfect_score_increases_ease():
    card = _card(ease_factor=2.5)
    state = next_state(card, quality=5)
    assert state.ease_factor > 2.5


def test_skip_tag_excludes_from_due():
    """Words tagged with skip:manual must not appear in load_due_cards."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_module.DB_PATH = Path(tmp.name)
    try:
        init_db()
        conn = get_connection()
        execute(conn, "INSERT INTO words (greek, english, tags) VALUES (?, ?, ?)",
                ("γεια", "hello", "skip:manual"))
        execute(conn, "INSERT INTO words (greek, english) VALUES (?, ?)",
                ("όχι", "no"))
        conn.commit()
        due = load_due_cards(conn, limit=100)
        greeks = [c.greek for c in due]
        assert "όχι" in greeks
        assert "γεια" not in greeks
        conn.close()
    finally:
        db_module.DB_PATH = _ORIG_DB_PATH
        Path(tmp.name).unlink(missing_ok=True)
