"""Tests for the assessment module — JSON parsing, word matching, context guessing."""

import tempfile
from pathlib import Path

import greekapp.db as db_module
from greekapp.db import execute, get_connection, init_db
from greekapp.assessor import (
    _find_vocab_words_in_text,
    _guess_english_from_context,
    _parse_json_lenient,
)

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
