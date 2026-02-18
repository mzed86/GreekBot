"""Learning progress report generation."""

from __future__ import annotations

from greekapp.db import fetchall_dicts, fetchone_dict


def generate_report(conn) -> str:
    """Generate a full learning progress report as plain text."""
    sections = []

    # --- Overview ---
    total = fetchone_dict(conn, "SELECT COUNT(*) AS cnt FROM words")["cnt"]
    seen = fetchone_dict(conn, "SELECT COUNT(DISTINCT word_id) AS cnt FROM reviews")["cnt"]
    total_reviews = fetchone_dict(conn, "SELECT COUNT(*) AS cnt FROM reviews")["cnt"]

    mastered = fetchone_dict(conn, """
        SELECT COUNT(*) AS cnt FROM (
            SELECT word_id FROM reviews r1
            WHERE reviewed_at = (
                SELECT MAX(reviewed_at) FROM reviews r2 WHERE r2.word_id = r1.word_id
            )
            AND interval >= 21
        ) sub
    """)["cnt"]

    messages_out = fetchone_dict(conn, "SELECT COUNT(*) AS cnt FROM messages WHERE direction = 'out'")["cnt"]
    messages_in = fetchone_dict(conn, "SELECT COUNT(*) AS cnt FROM messages WHERE direction = 'in'")["cnt"]

    corrections_count = fetchone_dict(conn, "SELECT COUNT(*) AS cnt FROM words WHERE tags LIKE ?", ("correction:%",))["cnt"]

    sections.append(
        f"--- Progress ---\n"
        f"Total words: {total} ({corrections_count} from corrections)\n"
        f"Seen: {seen} | Mastered (21d+): {mastered}\n"
        f"Reviews: {total_reviews}\n"
        f"Messages: {messages_out} sent, {messages_in} received"
    )

    # --- Struggling words (lowest ease, most resets) ---
    struggling = fetchall_dicts(conn, """
        SELECT w.greek, w.english, r.ease_factor, r.interval, r.repetition
        FROM words w
        JOIN (
            SELECT word_id, ease_factor, interval, repetition,
                   ROW_NUMBER() OVER (PARTITION BY word_id ORDER BY reviewed_at DESC) AS rn
            FROM reviews
        ) r ON r.word_id = w.id AND r.rn = 1
        WHERE r.ease_factor < 2.0 OR r.repetition = 0
        ORDER BY r.ease_factor ASC, r.interval ASC
        LIMIT 10
    """)

    if struggling:
        lines = ["--- Struggling words ---"]
        for w in struggling:
            lines.append(f"  {w['greek']} ({w['english']}) — ease={w['ease_factor']:.1f}, interval={w['interval']:.0f}d")
        sections.append("\n".join(lines))

    # --- Strongest words ---
    strong = fetchall_dicts(conn, """
        SELECT w.greek, w.english, r.interval, r.ease_factor
        FROM words w
        JOIN (
            SELECT word_id, ease_factor, interval,
                   ROW_NUMBER() OVER (PARTITION BY word_id ORDER BY reviewed_at DESC) AS rn
            FROM reviews
        ) r ON r.word_id = w.id AND r.rn = 1
        ORDER BY r.interval DESC
        LIMIT 5
    """)

    if strong:
        lines = ["--- Strongest words ---"]
        for w in strong:
            lines.append(f"  {w['greek']} ({w['english']}) — {w['interval']:.0f} days")
        sections.append("\n".join(lines))

    # --- Recent corrections ---
    corrections = fetchall_dicts(conn, """
        SELECT greek, english, tags FROM words
        WHERE tags LIKE ?
        ORDER BY created_at DESC
        LIMIT 8
    """, ("correction:%",))

    if corrections:
        lines = ["--- Recent corrections ---"]
        for c in corrections:
            ctype = c["tags"].replace("correction:", "")
            lines.append(f"  {c['greek']} ({c['english']}) [{ctype}]")
        sections.append("\n".join(lines))

    # --- Due now ---
    from greekapp.srs import load_due_cards
    due = load_due_cards(conn, limit=100)
    new_due = sum(1 for c in due if c.last_review is None)
    review_due = len(due) - new_due
    sections.append(f"--- Due now ---\n{len(due)} words ({new_due} new, {review_due} review)")

    # --- Profile notes learned ---
    notes = fetchall_dicts(conn, """
        SELECT category, content FROM profile_notes
        ORDER BY created_at DESC
        LIMIT 8
    """)

    if notes:
        lines = ["--- Learned about you ---"]
        for n in notes:
            lines.append(f"  [{n['category']}] {n['content']}")
        sections.append("\n".join(lines))

    return "\n\n".join(sections)
