"""Profile loader — merges static YAML with learned notes from DB."""

from __future__ import annotations

from pathlib import Path

import yaml

from greekapp.db import fetchall_dicts

PROFILE_PATH = Path(__file__).resolve().parent.parent / "profile.yaml"


def load_static_profile() -> dict:
    """Load the user's profile from profile.yaml."""
    if not PROFILE_PATH.exists():
        return {}
    with open(PROFILE_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_learned_notes(conn, limit: int = 50) -> list[dict]:
    """Load recent profile notes learned from conversation."""
    return fetchall_dicts(
        conn,
        """SELECT category, content, created_at
           FROM profile_notes
           ORDER BY created_at DESC
           LIMIT ?""",
        (limit,),
    )


def save_learned_note(conn, category: str, content: str) -> None:
    """Store a new profile note learned from conversation."""
    from greekapp.db import execute
    execute(
        conn,
        "INSERT INTO profile_notes (category, content) VALUES (?, ?)",
        (category, content),
    )
    conn.commit()


def get_full_profile(conn) -> dict:
    """Return the complete profile: static YAML merged with learned notes."""
    profile = load_static_profile()
    notes = load_learned_notes(conn)
    if notes:
        profile["learned"] = [
            f"[{n['category']}] {n['content']}" for n in notes
        ]
    return profile


def profile_to_prompt_text(profile: dict) -> str:
    """Convert a profile dict into text suitable for a Claude prompt."""
    lines = []

    identity = profile.get("identity", {})
    if identity.get("name"):
        lines.append(f"Name: {identity['name']}")
    if identity.get("location"):
        lines.append(f"Location: {identity['location']}")

    interests = profile.get("interests", {})
    for category, items in interests.items():
        filtered = [i for i in items if i]
        if filtered:
            lines.append(f"{category.title()}: {', '.join(filtered)}")

    style = profile.get("conversation_style", {})
    if style:
        parts = []
        if style.get("formality"):
            parts.append(f"tone={style['formality']}")
        if style.get("humor") is not None:
            parts.append(f"humor={'yes' if style['humor'] else 'no'}")
        if style.get("emoji_level"):
            parts.append(f"emoji={style['emoji_level']}")
        if parts:
            lines.append(f"Style: {', '.join(parts)}")

    learned = profile.get("learned", [])
    if learned:
        lines.append("\nThings learned from conversation:")
        for note in learned[:20]:
            lines.append(f"  - {note}")

    return "\n".join(lines)
