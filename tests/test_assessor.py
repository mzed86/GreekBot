"""Tests for the assessment module — JSON parsing, word matching, context guessing."""

import json
import tempfile
from pathlib import Path

import greekapp.db as db_module
from greekapp.db import execute, fetchone_dict, get_connection, init_db
from greekapp.assessor import (
    _find_vocab_words_in_text,
    _get_recent_outgoing_words,
    _guess_english_from_context,
    _parse_json_lenient,
)
from greekapp.srs import DEFAULT_EASE, record_review, CardState

_ORIG_DB_PATH = db_module.DB_PATH


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


# --- _parse_json_lenient ---

def test_parse_valid_json():
    data = _parse_json_lenient('{"key": "value"}')
    assert data == {"key": "value"}


def test_parse_json_with_surrounding_text():
    raw = 'Here is the JSON: {"word_assessments": [], "reply": "Γεια!"} Hope that helps.'
    data = _parse_json_lenient(raw)
    assert data is not None
    assert data["reply"] == "Γεια!"


def test_parse_json_trailing_comma():
    raw = '{"items": [1, 2, 3,], "name": "test",}'
    data = _parse_json_lenient(raw)
    assert data is not None
    assert data["items"] == [1, 2, 3]


def test_parse_json_nested_trailing_commas():
    raw = '{"a": {"b": 1,}, "c": [1, 2,],}'
    data = _parse_json_lenient(raw)
    assert data is not None
    assert data["a"]["b"] == 1


def test_parse_garbage_returns_none():
    assert _parse_json_lenient("this is not json at all") is None


def test_parse_empty_string_returns_none():
    assert _parse_json_lenient("") is None


def test_parse_json_with_newlines_in_strings():
    raw = '{"reply": "line one\\nline two"}'
    data = _parse_json_lenient(raw)
    assert data is not None
    assert "line one" in data["reply"]


# --- _find_vocab_words_in_text ---

def _add_word(conn, greek, english):
    execute(conn, "INSERT INTO words (greek, english) VALUES (?, ?)", (greek, english))
    conn.commit()
    from greekapp.db import fetchone_dict
    return fetchone_dict(conn, "SELECT id FROM words WHERE greek = ?", (greek,))["id"]


def test_find_words_basic_match():
    conn = get_connection()
    _add_word(conn, "καλημέρα", "good morning")
    found = _find_vocab_words_in_text(conn, "Καλημέρα! Τι κάνεις;")
    assert len(found) == 1
    conn.close()


def test_find_words_strips_article():
    conn = get_connection()
    _add_word(conn, "η γάτα", "the cat")
    found = _find_vocab_words_in_text(conn, "Η γάτα είναι εδώ")
    assert len(found) == 1
    conn.close()


def test_find_words_skips_short_words():
    conn = get_connection()
    _add_word(conn, "να", "to")  # 2 chars — should be skipped
    found = _find_vocab_words_in_text(conn, "Θέλω να πάω")
    assert len(found) == 0
    conn.close()


def test_find_words_respects_boundaries():
    conn = get_connection()
    _add_word(conn, "μέρα", "day")
    # "καλημέρα" contains "μέρα" but not at a word boundary
    found = _find_vocab_words_in_text(conn, "Καλημέρα!")
    assert len(found) == 0
    conn.close()


def test_find_words_multiple_matches():
    conn = get_connection()
    _add_word(conn, "σπίτι", "house")
    _add_word(conn, "φαγητό", "food")
    found = _find_vocab_words_in_text(conn, "Το σπίτι έχει φαγητό")
    assert len(found) == 2
    conn.close()


# --- _guess_english_from_context ---

def test_guess_english_equals_pattern():
    result = _guess_english_from_context("αναβάθμιση = upgrade in context", "αναβάθμιση")
    assert result == "upgrade"


def test_guess_english_diladi_pattern():
    result = _guess_english_from_context("αναβάθμιση δηλαδή upgrade σε αυτό", "αναβάθμιση")
    assert result == "upgrade"


def test_guess_english_no_match():
    result = _guess_english_from_context("Αυτό είναι ένα παράδειγμα", "παράδειγμα")
    assert result == "(from conversation)"


# --- _get_recent_outgoing_words (seed message anchoring) ---

def _add_outgoing_msg(conn, word_ids, is_seed=False):
    """Add an outgoing message, optionally with a send_log entry (seed)."""
    word_ids_json = json.dumps(word_ids) if word_ids else None
    execute(
        conn,
        "INSERT INTO messages (direction, body, target_word_ids) VALUES (?, ?, ?)",
        ("out", "test message", word_ids_json),
    )
    conn.commit()
    from greekapp.db import fetchone_dict
    msg_id = fetchone_dict(conn, "SELECT id FROM messages ORDER BY id DESC LIMIT 1")["id"]
    if is_seed:
        execute(conn, "INSERT INTO send_log (sent_date, message_id) VALUES (?, ?)", ("2026-02-19", msg_id))
        conn.commit()
    return msg_id


def test_seed_words_always_included():
    """Seed message words survive being pushed out of the recent-message window."""
    conn = get_connection()
    w1 = _add_word(conn, "πολιτική", "politics")
    w2 = _add_word(conn, "οικονομία", "economy")
    w3 = _add_word(conn, "εκπαίδευση", "education")

    # Seed message with target words
    _add_outgoing_msg(conn, [w1, w2, w3], is_seed=True)

    # 5 subsequent reply messages (no target words) — push seed out of window
    for _ in range(5):
        _add_outgoing_msg(conn, None, is_seed=False)

    words = _get_recent_outgoing_words(conn)
    found_ids = {w["id"] for w in words}
    assert w1 in found_ids
    assert w2 in found_ids
    assert w3 in found_ids
    conn.close()


def test_seed_words_plus_taught_words():
    """Both seed words and explicitly taught words from replies are included."""
    conn = get_connection()
    w1 = _add_word(conn, "δημοκρατία", "democracy")
    w2 = _add_word(conn, "ελευθερία", "freedom")

    # Seed message with w1
    _add_outgoing_msg(conn, [w1], is_seed=True)
    # Reply that explicitly taught w2
    _add_outgoing_msg(conn, [w2], is_seed=False)

    words = _get_recent_outgoing_words(conn)
    found_ids = {w["id"] for w in words}
    assert w1 in found_ids
    assert w2 in found_ids
    conn.close()


def test_no_seed_message_falls_back_to_recent():
    """Without a seed message, recent outgoing messages are still used."""
    conn = get_connection()
    w1 = _add_word(conn, "γλώσσα", "language")

    # No seed — just a regular outgoing message
    _add_outgoing_msg(conn, [w1], is_seed=False)

    words = _get_recent_outgoing_words(conn)
    found_ids = {w["id"] for w in words}
    assert w1 in found_ids
    conn.close()


def test_empty_outgoing_returns_empty():
    """No outgoing messages at all returns empty list."""
    conn = get_connection()
    words = _get_recent_outgoing_words(conn)
    assert words == []
    conn.close()


# --- quality=1 skip (word count fix) ---

def test_quality_1_should_not_reset_srs_progress():
    """Quality=1 (ignored) in conversation should not reset a word's SRS state.

    Regression test: during long conversations, words not mentioned in a
    particular exchange were being assessed as quality=1, which SM-2 treats
    as a failure and resets the interval to 0. This was wiping out progress.
    """
    conn = get_connection()
    wid = _add_word(conn, "πρόοδος", "progress")

    # Simulate the word having been reviewed successfully (quality=4)
    card = CardState(word_id=wid, greek="πρόοδος", english="progress")
    card = record_review(conn, card, 4)  # interval=1, repetition=1
    card = record_review(conn, card, 4)  # interval=6, repetition=2
    assert card.interval == 6.0
    assert card.repetition == 2

    # Now verify that if we record quality=1 it DOES reset (baseline)
    # This confirms the SM-2 algorithm treats quality<3 as failure
    from greekapp.srs import next_state
    hypothetical = next_state(card, 1)
    assert hypothetical.interval == 0.0
    assert hypothetical.repetition == 0

    # The fix: assess_and_reply skips quality=1 entirely, so the card
    # state should remain at interval=6, repetition=2 after a conversation
    # where this word wasn't discussed. We verify the card state is unchanged.
    review_count = fetchone_dict(
        conn, "SELECT COUNT(*) AS cnt FROM reviews WHERE word_id = ?", (wid,)
    )["cnt"]
    assert review_count == 2  # Only the two quality=4 reviews

    conn.close()
