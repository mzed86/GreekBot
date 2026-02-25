"""Tests for message composition — word selection, prompt building, RSS fetching."""

import tempfile
from pathlib import Path

import greekapp.db as db_module
from greekapp.db import execute, get_connection, init_db
from greekapp.messenger import (
    _build_search_topics,
    _bold_target_words,
    _fetch_rss_headlines,
    _fetch_rss_items_rich,
    _POLITICAL_FEEDS,
    _verify_words_in_message,
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


# --- _fetch_rss_headlines backward compat ---

def test_rss_headlines_returns_list_of_strings(monkeypatch):
    """_fetch_rss_headlines must still return list[str] for web_search/assessor compat."""
    xml_body = """<?xml version="1.0"?>
    <rss><channel>
      <item><title>Headline A</title><pubDate>Mon, 01 Jan 2024</pubDate></item>
      <item><title>Headline B</title></item>
    </channel></rss>"""
    import httpx

    class FakeResp:
        status_code = 200
        text = xml_body
        def raise_for_status(self): pass

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: FakeResp())
    result = _fetch_rss_headlines("test")
    assert isinstance(result, list)
    assert all(isinstance(h, str) for h in result)
    assert len(result) == 2


# --- _POLITICAL_FEEDS registry ---

def test_political_feeds_structure():
    """Each feed must have name, url, scope, and tag."""
    assert len(_POLITICAL_FEEDS) >= 3
    for feed in _POLITICAL_FEEDS:
        assert "name" in feed
        assert "url" in feed
        assert "scope" in feed
        assert "tag" in feed
        assert feed["scope"] in ("uk", "greece", "eu")


# --- _fetch_rss_items_rich ---

def test_rss_items_rich_returns_dicts(monkeypatch):
    """_fetch_rss_items_rich returns list of dicts with expected keys."""
    xml_body = """<?xml version="1.0"?>
    <rss><channel>
      <item>
        <title>PM announces new policy</title>
        <pubDate>Tue, 18 Feb 2026</pubDate>
        <description>The Prime Minister unveiled a new policy today.</description>
        <source>Guardian</source>
      </item>
    </channel></rss>"""
    import httpx

    class FakeResp:
        status_code = 200
        text = xml_body
        def raise_for_status(self): pass

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: FakeResp())
    items = _fetch_rss_items_rich("https://example.com/rss")
    assert len(items) == 1
    item = items[0]
    assert item["title"] == "PM announces new policy"
    assert item["pub_date"] == "Tue, 18 Feb 2026"
    assert item["description"] == "The Prime Minister unveiled a new policy today."
    assert item["source"] == "Guardian"


def test_rss_items_rich_strips_html(monkeypatch):
    """HTML tags in <description> must be stripped."""
    xml_body = """<?xml version="1.0"?>
    <rss><channel>
      <item>
        <title>Test</title>
        <description>&lt;p&gt;Bold &lt;b&gt;text&lt;/b&gt; here.&lt;/p&gt;</description>
      </item>
    </channel></rss>"""
    import httpx

    class FakeResp:
        status_code = 200
        text = xml_body
        def raise_for_status(self): pass

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: FakeResp())
    items = _fetch_rss_items_rich("https://example.com/rss")
    assert "<" not in items[0]["description"]
    assert "Bold" in items[0]["description"]


def test_rss_items_rich_truncates_description(monkeypatch):
    """Descriptions longer than 150 chars must be truncated."""
    long_desc = "A" * 300
    xml_body = f"""<?xml version="1.0"?>
    <rss><channel>
      <item>
        <title>Test</title>
        <description>{long_desc}</description>
      </item>
    </channel></rss>"""
    import httpx

    class FakeResp:
        status_code = 200
        text = xml_body
        def raise_for_status(self): pass

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: FakeResp())
    items = _fetch_rss_items_rich("https://example.com/rss")
    assert len(items[0]["description"]) == 150
    assert items[0]["description"].endswith("...")


def test_rss_items_rich_network_error(monkeypatch):
    """Network errors return empty list, not raise."""
    import httpx

    def _raise(*args, **kwargs):
        raise httpx.ConnectError("no internet")

    monkeypatch.setattr(httpx, "get", _raise)
    result = _fetch_rss_items_rich("https://example.com/rss")
    assert result == []


def test_rss_items_rich_atom_feed(monkeypatch):
    """_fetch_rss_items_rich handles Atom feeds (entry/published/content)."""
    atom_body = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <title>Tribune</title>
      <entry>
        <title>Labour's Smear Operation</title>
        <published>2026-02-18T10:00:00Z</published>
        <content type="html">&lt;p&gt;An investigative piece.&lt;/p&gt;</content>
      </entry>
      <entry>
        <title>Wrestling Against ICE</title>
        <summary>Short summary here</summary>
      </entry>
    </feed>"""
    import httpx

    class FakeResp:
        status_code = 200
        text = atom_body
        def raise_for_status(self): pass

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: FakeResp())
    items = _fetch_rss_items_rich("https://tribunemag.co.uk/feed")
    assert len(items) == 2
    assert items[0]["title"] == "Labour's Smear Operation"
    assert items[0]["pub_date"] == "2026-02-18T10:00:00Z"
    assert "<" not in items[0]["description"]
    assert items[1]["title"] == "Wrestling Against ICE"
    assert items[1]["description"] == "Short summary here"


# --- build_generation_prompt political persona ---

def test_prompt_contains_political_persona():
    """Prompt must include political opinion persona language."""
    words = [CardState(word_id=1, greek="γεια", english="hello")]
    prompt = build_generation_prompt({}, words, [])
    assert "you take sides" in prompt.lower() or "take sides" in prompt
    assert "not neutral" in prompt.lower() or "you're not neutral" in prompt


# --- _verify_words_in_message ---

def test_verify_exact_match():
    words = [CardState(word_id=1, greek="η βελτίωση", english="improvement")]
    verified, dropped = _verify_words_in_message(words, "Υπάρχει μεγάλη βελτίωση στην οικονομία.")
    assert len(verified) == 1
    assert len(dropped) == 0


def test_verify_inflected_form():
    """Stem matching should catch inflected forms like βελτιώσεις from βελτίωση."""
    words = [CardState(word_id=1, greek="η βελτίωση", english="improvement")]
    verified, dropped = _verify_words_in_message(words, "Βλέπω πολλές βελτιώσεις τώρα τελευταία.")
    assert len(verified) == 1


def test_verify_missing_word():
    words = [CardState(word_id=1, greek="η βελτίωση", english="improvement")]
    verified, dropped = _verify_words_in_message(words, "Πώς είσαι σήμερα;")
    assert len(verified) == 0
    assert len(dropped) == 1


def test_verify_multiple_mixed():
    words = [
        CardState(word_id=1, greek="η βελτίωση", english="improvement"),
        CardState(word_id=2, greek="το κράτος", english="state"),
    ]
    verified, dropped = _verify_words_in_message(words, "Το κράτος πρέπει να κάνει κάτι.")
    assert len(verified) == 1
    assert verified[0].greek == "το κράτος"
    assert len(dropped) == 1


# --- _bold_target_words ---

def test_bold_wraps_target_word():
    words = [CardState(word_id=1, greek="η βελτίωση", english="improvement")]
    result = _bold_target_words("Υπάρχει βελτίωση στην οικονομία.", words)
    assert "<b>" in result
    assert "βελτίωση" in result


def test_bold_escapes_html():
    words = [CardState(word_id=1, greek="η βελτίωση", english="improvement")]
    result = _bold_target_words("A < B & βελτίωση > C", words)
    assert "&lt;" in result
    assert "&amp;" in result
    assert "<b>" in result


def test_prompt_requires_all_target_words():
    """Generation prompt must instruct Claude to use ALL target words."""
    words = [CardState(word_id=1, greek="γεια", english="hello")]
    prompt = build_generation_prompt({}, words, [])
    assert "MUST use ALL" in prompt
