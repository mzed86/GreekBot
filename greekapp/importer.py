"""Import vocabulary from CSV files.

Supported formats:
  1. Standard:  greek, english [, part_of_speech, example_el, example_en, tags]
  2. Quizlet:   Set Name, Greek Term, English Definition
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from greekapp.db import execute, _is_postgres


# Map common alternate column names to our canonical names
COLUMN_ALIASES: dict[str, str] = {
    "greek term": "greek",
    "english definition": "english",
    "set name": "tags",
    "definition": "english",
    "term": "greek",
}

REQUIRED = {"greek", "english"}


def _normalise_row(raw: dict[str, str]) -> dict[str, str]:
    """Lower-case keys and apply column aliases."""
    row = {k.strip().lower(): v.strip() for k, v in raw.items()}
    return {COLUMN_ALIASES.get(k, k): v for k, v in row.items()}


def import_csv(conn, path: Path) -> dict[str, int]:
    """Import words from a CSV file. Returns {"added": n, "skipped": n}."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError("CSV file is empty")

        # Check that required columns exist after alias mapping
        headers = {h.strip().lower() for h in reader.fieldnames}
        mapped = {COLUMN_ALIASES.get(h, h) for h in headers}
        missing = REQUIRED - mapped
        if missing:
            raise ValueError(f"CSV is missing required columns: {missing}")

        added = skipped = 0
        use_pg = _is_postgres()

        for raw_row in reader:
            row = _normalise_row(raw_row)
            greek = row.get("greek", "")
            english = row.get("english", "")
            if not greek or not english:
                skipped += 1
                continue

            try:
                if use_pg:
                    # Use SAVEPOINT so a duplicate only rolls back the single row,
                    # not the entire transaction.
                    execute(conn, "SAVEPOINT import_row")
                execute(
                    conn,
                    """INSERT INTO words (greek, english, part_of_speech, example_el, example_en, tags)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        greek,
                        english,
                        row.get("part_of_speech", ""),
                        row.get("example_el", ""),
                        row.get("example_en", ""),
                        row.get("tags", ""),
                    ),
                )
                if use_pg:
                    execute(conn, "RELEASE SAVEPOINT import_row")
                added += 1
            except (sqlite3.IntegrityError, Exception) as e:
                err_str = str(e).lower()
                if "unique" in err_str or "duplicate" in err_str or "integrity" in err_str:
                    if use_pg:
                        execute(conn, "ROLLBACK TO SAVEPOINT import_row")
                    skipped += 1
                else:
                    raise

        conn.commit()
        return {"added": added, "skipped": skipped}
