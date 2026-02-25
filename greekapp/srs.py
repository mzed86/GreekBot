"""SM-2 spaced repetition algorithm.

Quality scale (0-5):
  0 - no recall at all
  1 - wrong, but recognised after seeing answer
  2 - wrong, but answer felt familiar
  3 - correct, but with difficulty
  4 - correct, with some hesitation
  5 - perfect recall
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from greekapp.db import execute, fetchall_dicts, _is_postgres


DEFAULT_EASE = 2.5
MIN_EASE = 1.3


@dataclass
class CardState:
    word_id: int
    greek: str
    english: str
    ease_factor: float = DEFAULT_EASE
    interval: float = 0.0  # days
    repetition: int = 0
    last_review: datetime | None = None

    @property
    def due_at(self) -> datetime:
        if self.last_review is None:
            return datetime.min
        return self.last_review + timedelta(days=self.interval)

    @property
    def is_due(self) -> bool:
        return datetime.now(UTC) >= self.due_at


def next_state(state: CardState, quality: int) -> CardState:
    """Compute the next SM-2 state given a quality rating."""
    if quality < 0 or quality > 5:
        raise ValueError("quality must be 0-5")

    ease = state.ease_factor + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    ease = max(ease, MIN_EASE)

    if quality < 3:
        # Reset on failure
        return CardState(
            word_id=state.word_id,
            greek=state.greek,
            english=state.english,
            ease_factor=ease,
            interval=0.0,
            repetition=0,
            last_review=datetime.now(UTC),
        )

    if state.repetition == 0:
        interval = 1.0
    elif state.repetition == 1:
        interval = 6.0
    else:
        interval = state.interval * ease

    return CardState(
        word_id=state.word_id,
        greek=state.greek,
        english=state.english,
        ease_factor=ease,
        interval=interval,
        repetition=state.repetition + 1,
        last_review=datetime.now(UTC),
    )


def record_review(conn, state: CardState, quality: int) -> CardState:
    """Apply a review, persist it, and return the new state."""
    new = next_state(state, quality)
    execute(
        conn,
        """INSERT INTO reviews (word_id, quality, ease_factor, interval, repetition)
           VALUES (?, ?, ?, ?, ?)""",
        (new.word_id, quality, new.ease_factor, new.interval, new.repetition),
    )
    conn.commit()
    return new


def load_due_cards(conn, limit: int = 20) -> list[CardState]:
    """Return cards that are due for review.

    Review cards (seen before) are ordered oldest-review-first so the most
    overdue words come back first.  New cards (never reviewed) are randomised
    so the bot cycles through the whole vocabulary instead of always picking
    the same batch from the top of the table.
    """
    if _is_postgres():
        rows = fetchall_dicts(conn, """
            SELECT
                w.id, w.greek, w.english,
                COALESCE(r.ease_factor, ?) AS ease_factor,
                COALESCE(r.interval, 0.0) AS interval,
                COALESCE(r.repetition, 0) AS repetition,
                r.reviewed_at AS last_review
            FROM words w
            LEFT JOIN (
                SELECT word_id, ease_factor, interval, repetition, reviewed_at,
                       ROW_NUMBER() OVER (PARTITION BY word_id ORDER BY reviewed_at DESC) AS rn
                FROM reviews
            ) r ON r.word_id = w.id AND r.rn = 1
            WHERE r.reviewed_at IS NULL
               OR (r.reviewed_at + CAST(COALESCE(r.interval, 0) || ' days' AS INTERVAL))
                  <= NOW()
            ORDER BY
                CASE WHEN r.reviewed_at IS NULL THEN 1 ELSE 0 END,
                r.reviewed_at ASC,
                RANDOM()
            LIMIT ?
        """, (DEFAULT_EASE, limit))
    else:
        rows = fetchall_dicts(conn, """
            SELECT
                w.id, w.greek, w.english,
                COALESCE(r.ease_factor, ?) AS ease_factor,
                COALESCE(r.interval, 0.0) AS interval,
                COALESCE(r.repetition, 0) AS repetition,
                r.reviewed_at AS last_review
            FROM words w
            LEFT JOIN (
                SELECT word_id, ease_factor, interval, repetition, reviewed_at,
                       ROW_NUMBER() OVER (PARTITION BY word_id ORDER BY reviewed_at DESC) AS rn
                FROM reviews
            ) r ON r.word_id = w.id AND r.rn = 1
            WHERE r.reviewed_at IS NULL
               OR datetime(r.reviewed_at, '+' || CAST(COALESCE(r.interval, 0) AS TEXT) || ' days')
                  <= datetime('now')
            ORDER BY
                CASE WHEN r.reviewed_at IS NULL THEN 1 ELSE 0 END,
                r.reviewed_at ASC,
                RANDOM()
            LIMIT ?
        """, (DEFAULT_EASE, limit))

    cards = []
    for row in rows:
        lr = None
        if row["last_review"]:
            val = row["last_review"]
            if isinstance(val, datetime):
                lr = val
            else:
                lr = datetime.fromisoformat(str(val))
        cards.append(CardState(
            word_id=row["id"],
            greek=row["greek"],
            english=row["english"],
            ease_factor=row["ease_factor"],
            interval=row["interval"],
            repetition=row["repetition"],
            last_review=lr,
        ))
    return cards
