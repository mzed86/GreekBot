"""Tests for message composition — word selection, prompt building, RSS fetching."""

import tempfile
from pathlib import Path

import greekapp.db as db_module
from greekapp.db import execute, get_connection, init_db
from greekapp.messenger import (
    _build_search_topics,
    _fetch_rss_headlines,
    build_generation_prompt,
    select_words,
)
from greekapp.srs import CardState

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


def _add_word(conn, greek, english):
    execute(conn, "INSERT INTO words (greek, english) VALUES (?, ?)", (greek, english))
    conn.commit()


# --- select_words ---

def test_select_words_empty_db():
    conn = get_connection()
    words = select_words(conn)
    assert words == []
    conn.close()


def test_select_words_returns_cards():
    conn = get_connection()
    for i, (g, e) in enumerate([
        ("γεια", "hello"), ("ευχαριστώ", "thanks"), ("ναι", "yes"),
        ("όχι", "no"), ("καλά", "good"),
    ]):
        _add_word(conn, g, e)
    words = select_words(conn)
    assert len(words) >= 3
    assert all(isinstance(w, CardState) for w in words)
    conn.close()


def test_select_words_caps_at_five():
    conn = get_connection()
    for i in range(20):
        _add_word(conn, f"word{i}", f"eng{i}")
    words = select_words(conn)
    assert len(words) <= 6  # 3 review + 3 new max, but can be up to 5+1
    conn.close()


# --- build_generation_prompt ---

def test_prompt_contains_target_words():
    words = [
        CardState(word_id=1, greek="σπίτι", english="house"),
        CardState(word_id=2, greek="φαγητό", english="food"),
    ]
    prompt = build_generation_prompt({}, words, [])
    assert "σπίτι" in prompt
    assert "φαγητό" in prompt
    assert "house" in prompt


def test_prompt_includes_profile():
    profile = {"identity": {"name": "Mike", "location": "London"}}
    words = [CardState(word_id=1, greek="γεια", english="hello")]
    prompt = build_generation_prompt(profile, words, [])
    assert "Mike" in prompt
    assert "London" in prompt


def test_prompt_includes_history():
    words = [CardState(word_id=1, greek="γεια", english="hello")]
    history = [{"direction": "out", "body": "Γεια σου!", "created_at": "2024-01-01"}]
    prompt = build_generation_prompt({}, words, history)
    assert "Γεια σου!" in prompt


def test_prompt_includes_news_context():
    words = [CardState(word_id=1, greek="γεια", english="hello")]
    prompt = build_generation_prompt({}, words, [], news_context="Arsenal beat Chelsea 3-0")
    assert "Arsenal beat Chelsea" in prompt


# --- _build_search_topics ---

def test_search_topics_from_sports():
    profile = {"interests": {"sports": ["Arsenal FC", "Formula 1"]}}
    topics = _build_search_topics(profile)
    assert "Arsenal FC" in topics
    assert "Formula 1" in topics


def test_search_topics_filters_fan():
    profile = {"interests": {"sports": ["Gunners fan"]}}
    topics = _build_search_topics(profile)
    assert len(topics) == 0


def test_search_topics_empty_profile():
    topics = _build_search_topics({})
    assert topics == []


# --- _fetch_rss_headlines ---

def test_rss_network_error(monkeypatch):
    """Network errors should return an empty list, not raise."""
    import httpx

    def _raise(*args, **kwargs):
        raise httpx.ConnectError("no internet")

    monkeypatch.setattr(httpx, "get", _raise)
    result = _fetch_rss_headlines("test query")
    assert result == []
