"""Tests for learning progress report generation."""

import tempfile
from pathlib import Path

import greekapp.db as db_module
from greekapp.db import execute, get_connection, init_db
from greekapp.report import generate_report

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


def _add_word(conn, greek, english, tags=""):
    execute(conn, "INSERT INTO words (greek, english, tags) VALUES (?, ?, ?)", (greek, english, tags))
    conn.commit()


def _add_review(conn, word_id, quality, ease=2.5, interval=1.0, repetition=1):
    execute(
        conn,
        "INSERT INTO reviews (word_id, quality, ease_factor, interval, repetition) VALUES (?, ?, ?, ?, ?)",
        (word_id, quality, ease, interval, repetition),
    )
    conn.commit()


def test_report_empty_db():
    conn = get_connection()
    report = generate_report(conn)
    assert "Total words: 0" in report
    assert "Seen: 0" in report
    conn.close()


def test_report_with_words_and_reviews():
    conn = get_connection()
    _add_word(conn, "γεια", "hello")
    _add_word(conn, "ευχαριστώ", "thank you")
    _add_review(conn, 1, quality=4, ease=2.6, interval=6.0, repetition=2)
    _add_review(conn, 2, quality=3, ease=2.3, interval=1.0, repetition=1)
    report = generate_report(conn)
    assert "Total words: 2" in report
    assert "Seen: 2" in report
    assert "Reviews: 2" in report
    conn.close()


def test_report_struggling_section():
    conn = get_connection()
    _add_word(conn, "δύσκολη", "difficult")
    # Low ease = struggling
    _add_review(conn, 1, quality=1, ease=1.5, interval=0.0, repetition=0)
    report = generate_report(conn)
    assert "Struggling" in report
    assert "δύσκολη" in report
    conn.close()


def test_report_strong_section():
    conn = get_connection()
    _add_word(conn, "γεια", "hello")
    _add_review(conn, 1, quality=5, ease=2.8, interval=30.0, repetition=5)
    report = generate_report(conn)
    assert "Strongest" in report
    assert "γεια" in report
    assert "30 days" in report
    conn.close()


def test_report_corrections_section():
    conn = get_connection()
    _add_word(conn, "ώρα", "time", tags="correction:vocab")
    report = generate_report(conn)
    assert "corrections" in report.lower()
    assert "ώρα" in report
    assert "vocab" in report
    conn.close()
