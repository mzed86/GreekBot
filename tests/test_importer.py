"""Tests for CSV import."""

import tempfile
from pathlib import Path

import greekapp.db as db_module
from greekapp.db import get_connection, init_db
from greekapp.importer import import_csv

_ORIG_DB_PATH = db_module.DB_PATH


def _tmp_csv(content: str) -> Path:
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False)
    f.write(content)
    f.close()
    return Path(f.name)


def setup_function():
    """Use a temp DB so we don't nuke real data."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_module.DB_PATH = Path(tmp.name)
    init_db()


def teardown_function():
    """Restore original DB path and clean up temp."""
    tmp_path = db_module.DB_PATH
    db_module.DB_PATH = _ORIG_DB_PATH
    if tmp_path.exists():
        tmp_path.unlink()


def test_import_basic():
    path = _tmp_csv("greek,english\nγεια,hello\nόχι,no\n")
    conn = get_connection()
    result = import_csv(conn, path)
    assert result["added"] == 2
    assert result["skipped"] == 0
    conn.close()


def test_import_skips_duplicates():
    path = _tmp_csv("greek,english\nγεια,hello\n")
    conn = get_connection()
    import_csv(conn, path)
    result = import_csv(conn, path)
    assert result["added"] == 0
    assert result["skipped"] == 1
    conn.close()


def test_import_skips_empty_rows():
    path = _tmp_csv("greek,english\n,\nγεια,hello\n")
    conn = get_connection()
    result = import_csv(conn, path)
    assert result["added"] == 1
    assert result["skipped"] == 1
    conn.close()


def test_import_duplicates_dont_destroy_earlier_inserts():
    """Duplicates mid-CSV must not wipe out words inserted before them.

    This was the core bug: on PostgreSQL, conn.rollback() after a unique
    constraint violation erased ALL previously inserted words in the
    transaction, not just the failed row.
    """
    path = _tmp_csv(
        "greek,english\n"
        "ένα,one\n"
        "δύο,two\n"
        "τρία,three\n"
        "ένα,one\n"    # duplicate of row 1
        "τέσσερα,four\n"
    )
    conn = get_connection()
    result = import_csv(conn, path)
    assert result["added"] == 4
    assert result["skipped"] == 1

    # Crucially, ALL four unique words must be present — not just the ones
    # after the duplicate.
    from greekapp.db import fetchall_dicts
    rows = fetchall_dicts(conn, "SELECT greek FROM words")
    words = sorted(r["greek"] for r in rows)
    assert words == sorted(["δύο", "ένα", "τέσσερα", "τρία"])
    conn.close()
