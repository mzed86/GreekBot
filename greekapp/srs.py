"""SM-2 spaced repetition algorithm with learning steps, leech detection, and overdue decay.

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

from greekapp.db import execute, fetchall_dicts, fetchone_dict, ph, _is_postgres


DEFAULT_EASE = 2.5
MIN_EASE = 1.3
LEARNING_STEP = 0.014  # ~20 minutes — word returns next cron cycle
LEECH_THRESHOLD = 4  # consecutive failures before flagging as leech


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

    @property
    def overdue_factor(self) -> float:
        """How overdue this card is. 1.0 = on time, >1 = overdue."""
        if self.last_review is None or self.interval <= 0:
            return 1.0
        days_since = (datetime.now(UTC) - self.last_review).total_seconds() / 86400
        return max(1.0, days_since / self.interval)

    @property
    def is_learning(self) -> bool:
        """True if the card is still in the learning phase (not yet graduated)."""
        return self.repetition < 2


def next_state(state: CardState, quality: int) -> CardState:
    """Compute the next SM-2 state given a quality rating.

    Includes learning steps for new cards and overdue decay for long-absent cards.
    """
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

    # Learning steps for new cards: see the word twice before graduating
    if state.repetition == 0:
        interval = LEARNING_STEP  # ~20 min — reappears next cron cycle
    elif state.repetition == 1:
        interval = 1.0  # Graduate to 1 day
    elif state.repetition == 2:
        interval = 6.0
    else:
        interval = state.interval * ease

    # Overdue decay: if card was severely overdue (3x+ its interval),
    # don't trust a single success — cap interval growth to be conservative
    if state.interval > 1.0 and state.last_review:
        days_since = (datetime.now(UTC) - state.last_review).total_seconds() / 86400
        overdue_ratio = days_since / state.interval
        if overdue_ratio > 3.0:
            interval = min(interval, state.interval * 1.2)

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


def get_consecutive_failures(conn, word_id: int) -> int:
    """Count consecutive quality<3 reviews from the most recent backwards."""
    rows = fetchall_dicts(conn, """
        SELECT quality FROM reviews
        WHERE word_id = ?
        ORDER BY reviewed_at DESC
        LIMIT 10
    """, (word_id,))
    count = 0
    for row in rows:
        if row["quality"] < 3:
            count += 1
        else:
            break
    return count


def is_leech(conn, word_id: int) -> bool:
    """True if a word has failed LEECH_THRESHOLD+ times consecutively."""
    return get_consecutive_failures(conn, word_id) >= LEECH_THRESHOLD


def get_leeches(conn, limit: int = 20) -> list[CardState]:
    """Return all leech words (words with 4+ consecutive failures)."""
    # Get words with recent low-quality reviews
    candidates = fetchall_dicts(conn, """
        SELECT r.word_id
        FROM reviews r
        WHERE r.quality < 3
        GROUP BY r.word_id
        ORDER BY MAX(r.reviewed_at) DESC
        LIMIT ?
    """, (limit * 3,))

    leeches = []
    for row in candidates:
        if is_leech(conn, row["word_id"]):
            word = fetchone_dict(conn,
                "SELECT id, greek, english FROM words WHERE id = ?",
                (row["word_id"],))
            if word:
                leeches.append(CardState(
                    word_id=word["id"],
                    greek=word["greek"],
                    english=word["english"],
                ))
        if len(leeches) >= limit:
            break
    return leeches


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
            WHERE (r.reviewed_at IS NULL
               OR (r.reviewed_at + CAST(COALESCE(r.interval, 0) || ' days' AS INTERVAL))
                  <= NOW())
              AND (w.tags IS NULL OR w.tags NOT LIKE ?)
            ORDER BY
                CASE WHEN r.reviewed_at IS NULL THEN 1 ELSE 0 END,
                r.reviewed_at ASC,
                RANDOM()
            LIMIT ?
        """, (DEFAULT_EASE, '%skip:manual%', limit))
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
            WHERE (r.reviewed_at IS NULL
               OR datetime(r.reviewed_at, '+' || CAST(COALESCE(r.interval, 0) AS TEXT) || ' days')
                  <= datetime('now'))
              AND (w.tags IS NULL OR w.tags NOT LIKE ?)
            ORDER BY
                CASE WHEN r.reviewed_at IS NULL THEN 1 ELSE 0 END,
                r.reviewed_at ASC,
                RANDOM()
            LIMIT ?
        """, (DEFAULT_EASE, '%skip:manual%', limit))

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


def get_retention_stats(conn) -> dict:
    """Calculate retention metrics for self-monitoring."""
    # Overall retention rate (% of reviews with quality >= 3)
    total = fetchone_dict(conn, "SELECT COUNT(*) AS cnt FROM reviews")
    success = fetchone_dict(conn, "SELECT COUNT(*) AS cnt FROM reviews WHERE quality >= 3")

    total_count = total["cnt"] if total else 0
    success_count = success["cnt"] if success else 0
    retention_rate = (success_count / total_count * 100) if total_count > 0 else 0

    # Recent retention (last 7 days)
    if _is_postgres():
        recent_total = fetchone_dict(conn,
            "SELECT COUNT(*) AS cnt FROM reviews WHERE reviewed_at >= NOW() - INTERVAL '7 days'")
        recent_success = fetchone_dict(conn,
            "SELECT COUNT(*) AS cnt FROM reviews WHERE quality >= 3 AND reviewed_at >= NOW() - INTERVAL '7 days'")
    else:
        recent_total = fetchone_dict(conn,
            "SELECT COUNT(*) AS cnt FROM reviews WHERE reviewed_at >= datetime('now', '-7 days')")
        recent_success = fetchone_dict(conn,
            "SELECT COUNT(*) AS cnt FROM reviews WHERE quality >= 3 AND reviewed_at >= datetime('now', '-7 days')")

    recent_total_count = recent_total["cnt"] if recent_total else 0
    recent_success_count = recent_success["cnt"] if recent_success else 0
    recent_retention = (recent_success_count / recent_total_count * 100) if recent_total_count > 0 else 0

    # Average quality score trend
    if _is_postgres():
        avg_recent = fetchone_dict(conn,
            "SELECT AVG(quality) AS avg_q FROM reviews WHERE reviewed_at >= NOW() - INTERVAL '7 days'")
        avg_older = fetchone_dict(conn,
            "SELECT AVG(quality) AS avg_q FROM reviews WHERE reviewed_at < NOW() - INTERVAL '7 days'")
    else:
        avg_recent = fetchone_dict(conn,
            "SELECT AVG(quality) AS avg_q FROM reviews WHERE reviewed_at >= datetime('now', '-7 days')")
        avg_older = fetchone_dict(conn,
            "SELECT AVG(quality) AS avg_q FROM reviews WHERE reviewed_at < datetime('now', '-7 days')")

    avg_recent_q = avg_recent["avg_q"] if avg_recent and avg_recent["avg_q"] else 0
    avg_older_q = avg_older["avg_q"] if avg_older and avg_older["avg_q"] else 0

    return {
        "retention_rate": retention_rate,
        "recent_retention": recent_retention,
        "total_reviews": total_count,
        "recent_reviews": recent_total_count,
        "avg_quality_recent": float(avg_recent_q),
        "avg_quality_older": float(avg_older_q),
        "quality_trend": "improving" if avg_recent_q > avg_older_q + 0.3
                         else "declining" if avg_recent_q < avg_older_q - 0.3
                         else "stable",
    }
