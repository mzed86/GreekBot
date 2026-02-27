"""Tests for the SM-2 spaced repetition engine with learning steps, leech detection, and overdue decay."""

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import greekapp.db as db_module
from greekapp.db import execute, get_connection, init_db
from greekapp.srs import (
    CardState,
    DEFAULT_EASE,
    LEARNING_STEP,
    LEECH_THRESHOLD,
    MIN_EASE,
    get_consecutive_failures,
    get_leeches,
    get_retention_stats,
    is_leech,
    load_due_cards,
    next_state,
    record_review,
)

_ORIG_DB_PATH = db_module.DB_PATH


def _card(**kw):
    defaults = dict(word_id=1, greek="γεια", english="hello")
    defaults.update(kw)
    return CardState(**defaults)


# --- Learning steps ---

def test_first_correct_review_gives_learning_step():
    """First success on a new card gives a short learning interval, not 1 day."""
    state = next_state(_card(), quality=4)
    assert state.interval == LEARNING_STEP
    assert state.repetition == 1


def test_second_correct_review_graduates_to_one_day():
    """Second success graduates the card to 1-day interval."""
    card = _card(repetition=1, interval=LEARNING_STEP)
    state = next_state(card, quality=4)
    assert state.interval == 1.0
    assert state.repetition == 2


def test_third_correct_review_sets_interval_6():
    card = _card(repetition=2, interval=1.0)
    state = next_state(card, quality=4)
    assert state.interval == 6.0
    assert state.repetition == 3


def test_fourth_correct_review_uses_ease_factor():
    card = _card(repetition=3, interval=6.0, ease_factor=2.5)
    state = next_state(card, quality=4)
    assert state.interval == 6.0 * state.ease_factor
    assert state.repetition == 4


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


# --- is_learning property ---

def test_is_learning_new_card():
    card = _card(repetition=0)
    assert card.is_learning is True


def test_is_learning_after_one_review():
    card = _card(repetition=1)
    assert card.is_learning is True


def test_not_learning_after_graduation():
    card = _card(repetition=2)
    assert card.is_learning is False


# --- Overdue factor ---

def test_overdue_factor_no_review():
    card = _card(last_review=None)
    assert card.overdue_factor == 1.0


def test_overdue_factor_on_time():
    card = _card(
        interval=10.0,
        last_review=datetime.now(UTC) - timedelta(days=10),
    )
    assert 0.9 <= card.overdue_factor <= 1.1


def test_overdue_factor_severely_overdue():
    card = _card(
        interval=10.0,
        last_review=datetime.now(UTC) - timedelta(days=40),
    )
    assert card.overdue_factor >= 3.5


# --- Overdue decay ---

def test_overdue_decay_caps_interval():
    """Severely overdue cards (3x+ interval) should have capped interval growth."""
    card = _card(
        repetition=5,
        interval=10.0,
        ease_factor=2.5,
        last_review=datetime.now(UTC) - timedelta(days=40),  # 4x overdue
    )
    state = next_state(card, quality=4)
    # Without decay: 10 * ~2.4 = ~24 days
    # With decay: capped at 10 * 1.2 = 12 days
    assert state.interval <= 12.1


def test_no_decay_when_not_severely_overdue():
    """Cards that are only slightly overdue should not be capped."""
    card = _card(
        repetition=5,
        interval=10.0,
        ease_factor=2.5,
        last_review=datetime.now(UTC) - timedelta(days=15),  # 1.5x overdue
    )
    state = next_state(card, quality=4)
    # Normal growth: 10 * ~2.4 ≈ 24
    assert state.interval > 12.0


# --- Leech detection ---

def test_consecutive_failures_count():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_module.DB_PATH = Path(tmp.name)
    try:
        init_db()
        conn = get_connection()
        execute(conn, "INSERT INTO words (greek, english) VALUES (?, ?)", ("λάθος", "mistake"))
        conn.commit()

        card = CardState(word_id=1, greek="λάθος", english="mistake")
        # 3 failures then 1 success then 2 failures
        record_review(conn, card, 1)
        card = CardState(word_id=1, greek="λάθος", english="mistake", repetition=0)
        record_review(conn, card, 2)
        card = CardState(word_id=1, greek="λάθος", english="mistake", repetition=0)
        record_review(conn, card, 4)  # success breaks the streak
        card = CardState(word_id=1, greek="λάθος", english="mistake", repetition=1, interval=LEARNING_STEP)
        record_review(conn, card, 1)
        card = CardState(word_id=1, greek="λάθος", english="mistake", repetition=0)
        record_review(conn, card, 0)

        # Most recent: fail, fail, success — consecutive failures = 2
        assert get_consecutive_failures(conn, 1) == 2
        assert is_leech(conn, 1) is False

        conn.close()
    finally:
        db_module.DB_PATH = _ORIG_DB_PATH
        Path(tmp.name).unlink(missing_ok=True)


def test_is_leech_after_many_failures():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_module.DB_PATH = Path(tmp.name)
    try:
        init_db()
        conn = get_connection()
        execute(conn, "INSERT INTO words (greek, english) VALUES (?, ?)", ("δύσκολο", "difficult"))
        conn.commit()

        card = CardState(word_id=1, greek="δύσκολο", english="difficult")
        for _ in range(5):
            record_review(conn, card, 1)

        assert get_consecutive_failures(conn, 1) == 5
        assert is_leech(conn, 1) is True

        conn.close()
    finally:
        db_module.DB_PATH = _ORIG_DB_PATH
        Path(tmp.name).unlink(missing_ok=True)


def test_get_leeches():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_module.DB_PATH = Path(tmp.name)
    try:
        init_db()
        conn = get_connection()
        execute(conn, "INSERT INTO words (greek, english) VALUES (?, ?)", ("δύσκολο", "difficult"))
        execute(conn, "INSERT INTO words (greek, english) VALUES (?, ?)", ("εύκολο", "easy"))
        conn.commit()

        # Make word 1 a leech
        card = CardState(word_id=1, greek="δύσκολο", english="difficult")
        for _ in range(5):
            record_review(conn, card, 1)

        # Word 2 has only one failure
        card2 = CardState(word_id=2, greek="εύκολο", english="easy")
        record_review(conn, card2, 1)

        leeches = get_leeches(conn)
        assert len(leeches) == 1
        assert leeches[0].greek == "δύσκολο"

        conn.close()
    finally:
        db_module.DB_PATH = _ORIG_DB_PATH
        Path(tmp.name).unlink(missing_ok=True)


# --- Retention stats ---

def test_retention_stats_empty_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_module.DB_PATH = Path(tmp.name)
    try:
        init_db()
        conn = get_connection()
        stats = get_retention_stats(conn)
        assert stats["retention_rate"] == 0
        assert stats["total_reviews"] == 0
        assert stats["quality_trend"] == "stable"
        conn.close()
    finally:
        db_module.DB_PATH = _ORIG_DB_PATH
        Path(tmp.name).unlink(missing_ok=True)


def test_retention_stats_with_reviews():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_module.DB_PATH = Path(tmp.name)
    try:
        init_db()
        conn = get_connection()
        execute(conn, "INSERT INTO words (greek, english) VALUES (?, ?)", ("τεστ", "test"))
        conn.commit()

        card = CardState(word_id=1, greek="τεστ", english="test")
        record_review(conn, card, 4)
        card = CardState(word_id=1, greek="τεστ", english="test", repetition=1, interval=LEARNING_STEP)
        record_review(conn, card, 5)
        card = CardState(word_id=1, greek="τεστ", english="test", repetition=2, interval=1.0)
        record_review(conn, card, 1)  # one failure

        stats = get_retention_stats(conn)
        assert stats["total_reviews"] == 3
        # 2 out of 3 successful = ~66.7%
        assert 60 < stats["retention_rate"] < 70

        conn.close()
    finally:
        db_module.DB_PATH = _ORIG_DB_PATH
        Path(tmp.name).unlink(missing_ok=True)


# --- Existing tests ---

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
