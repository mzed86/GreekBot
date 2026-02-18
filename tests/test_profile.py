"""Tests for profile loader and prompt formatting."""

import tempfile
from pathlib import Path

import greekapp.db as db_module
import greekapp.profile as profile_module
from greekapp.db import get_connection, init_db
from greekapp.profile import (
    get_full_profile,
    load_static_profile,
    profile_to_prompt_text,
    save_learned_note,
)

_ORIG_DB_PATH = db_module.DB_PATH
_ORIG_PROFILE_PATH = profile_module.PROFILE_PATH


def setup_function():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_module.DB_PATH = Path(tmp.name)
    init_db()


def teardown_function():
    tmp_path = db_module.DB_PATH
    db_module.DB_PATH = _ORIG_DB_PATH
    profile_module.PROFILE_PATH = _ORIG_PROFILE_PATH
    if tmp_path.exists():
        tmp_path.unlink()


def test_profile_to_prompt_text_full():
    profile = {
        "identity": {"name": "Mike", "location": "London"},
        "interests": {"sports": ["football", "running"]},
        "conversation_style": {"formality": "casual", "humor": True, "emoji_level": "low"},
    }
    text = profile_to_prompt_text(profile)
    assert "Mike" in text
    assert "London" in text
    assert "football" in text
    assert "tone=casual" in text
    assert "humor=yes" in text


def test_profile_to_prompt_text_empty():
    text = profile_to_prompt_text({})
    assert text == ""


def test_profile_to_prompt_text_partial():
    profile = {"identity": {"name": "Mike"}}
    text = profile_to_prompt_text(profile)
    assert "Mike" in text
    assert "Location" not in text


def test_profile_to_prompt_text_with_learned():
    profile = {"learned": ["[hobby] likes cooking", "[work] software engineer"]}
    text = profile_to_prompt_text(profile)
    assert "likes cooking" in text
    assert "software engineer" in text
    assert "learned from conversation" in text.lower()


def test_load_static_profile_with_file():
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    tmp.write("identity:\n  name: TestUser\n")
    tmp.close()
    profile_module.PROFILE_PATH = Path(tmp.name)
    profile = load_static_profile()
    assert profile["identity"]["name"] == "TestUser"
    Path(tmp.name).unlink()


def test_load_static_profile_missing_file():
    profile_module.PROFILE_PATH = Path("/nonexistent/profile.yaml")
    profile = load_static_profile()
    assert profile == {}


def test_save_learned_note_persists():
    conn = get_connection()
    save_learned_note(conn, "hobby", "enjoys hiking")
    from greekapp.db import fetchone_dict
    row = fetchone_dict(conn, "SELECT category, content FROM profile_notes WHERE category = 'hobby'")
    assert row is not None
    assert row["content"] == "enjoys hiking"
    conn.close()


def test_get_full_profile_merges_learned():
    # Point at a non-existent file so static profile is empty
    profile_module.PROFILE_PATH = Path("/nonexistent/profile.yaml")
    conn = get_connection()
    save_learned_note(conn, "work", "software dev")
    profile = get_full_profile(conn)
    assert "learned" in profile
    assert any("software dev" in note for note in profile["learned"])
    conn.close()
