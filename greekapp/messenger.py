"""Message composition — picks words, generates natural messages via Claude, sends via Telegram."""

from __future__ import annotations

import json
import random
from datetime import datetime, timezone

import httpx

from greekapp.config import Config
from greekapp.db import execute, fetchall_dicts, fetchone_dict, ph
from greekapp.profile import get_full_profile, profile_to_prompt_text
from greekapp.srs import CardState, DEFAULT_EASE, load_due_cards
from greekapp.telegram import send_message


def select_words(conn, new_limit: int = 3, review_limit: int = 3) -> list[CardState]:
    """Pick a mix of new + due review words for a message.

    Returns 3-5 words: review words first, then new words to fill.
    """
    due = load_due_cards(conn, limit=10)

    # Split into review words (seen before) and new words (never reviewed)
    review_words = [c for c in due if c.last_review is not None]
    new_words = [c for c in due if c.last_review is None]

    # Take review words first, then fill remaining slots with new words
    selected = review_words[:review_limit]
    remaining_slots = max(0, 5 - len(selected))
    selected.extend(new_words[:min(new_limit, remaining_slots)])

    # If we still have fewer than 3, grab whatever is available
    if len(selected) < 3:
        extras = [c for c in due if c not in selected]
        selected.extend(extras[:3 - len(selected)])

    random.shuffle(selected)
    return selected


def _get_recent_messages(conn, limit: int = 10) -> list[dict]:
    """Load recent conversation history for context."""
    return fetchall_dicts(
        conn,
        """SELECT direction, body, created_at
           FROM messages
           ORDER BY created_at DESC
           LIMIT ?""",
        (limit,),
    )


def _time_of_day() -> str:
    hour = datetime.now().hour
    if hour < 12:
        return "morning"
    if hour < 17:
        return "afternoon"
    return "evening"


def _build_search_topics(profile: dict) -> list[str]:
    """Extract search queries from profile interests."""
    topics = []
    sports = profile.get("interests", {}).get("sports", [])
    current = profile.get("interests", {}).get("current_events", [])
    hobbies = profile.get("interests", {}).get("hobbies", [])
    location = profile.get("identity", {}).get("location", "")

    for item in (current or []) + (sports or []):
        if item and "fan" not in item.lower():
            topics.append(item)

    music_hobbies = [h for h in (hobbies or []) if h and any(k in h.lower() for k in ("music", "concert", "gig", "live"))]
    if music_hobbies and location:
        topics.append(f"concerts gigs {location}")

    return [t for t in topics if t]


def _fetch_rss_headlines(query: str, max_results: int = 3) -> list[str]:
    """Fetch real headlines from Google News RSS for a query."""
    import xml.etree.ElementTree as ET

    try:
        resp = httpx.get(
            "https://news.google.com/rss/search",
            params={"q": query, "hl": "en-GB", "gl": "GB", "ceid": "GB:en"},
            timeout=8,
            follow_redirects=True,
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        headlines = []
        for item in root.iter("item"):
            title = item.findtext("title", "")
            pub_date = item.findtext("pubDate", "")
            if title:
                headlines.append(f"{title} ({pub_date})" if pub_date else title)
            if len(headlines) >= max_results:
                break
        return headlines
    except Exception:
        return []


def fetch_news_context(profile: dict) -> str:
    """Fetch real current headlines relevant to the user's interests via Google News RSS."""
    topics = _build_search_topics(profile)
    if not topics:
        return ""

    selected = random.sample(topics, min(2, len(topics)))
    snippets = []
    for topic in selected:
        headlines = _fetch_rss_headlines(topic, max_results=3)
        for h in headlines:
            snippets.append(f"[{topic}] {h}")

    return "\n".join(snippets[:8]) if snippets else ""


def web_search(query: str, max_results: int = 5) -> str:
    """Search for specific factual info via Google News RSS. Use for fixture schedules, results, etc."""
    headlines = _fetch_rss_headlines(query, max_results=max_results)
    return "\n".join(headlines) if headlines else ""


def build_generation_prompt(
    profile: dict,
    words: list[CardState],
    history: list[dict],
    news_context: str = "",
) -> str:
    """Build the Claude prompt for message generation."""
    profile_text = profile_to_prompt_text(profile)
    time_context = _time_of_day()

    word_list = ", ".join(f"{w.greek} ({w.english})" for w in words)
    word_section = f"Target words to weave in: {word_list}\n"

    # Recent conversation for continuity
    history_text = ""
    if history:
        history_lines = []
        for msg in reversed(history[:6]):
            prefix = "You" if msg["direction"] == "out" else "Them"
            history_lines.append(f"{prefix}: {msg['body']}")
        history_text = "\n".join(history_lines)

    return f"""You are a Greek friend texting in Greek. Write ENTIRELY in Greek. No English at all.

You are texting a friend who is learning Greek. They understand a lot already. Write to them the way you'd text any Greek friend — naturally, casually, all in Greek.

ABOUT THEM:
{profile_text}

TIME: {time_context}

{word_section}
RULES:
- Write 1-3 short sentences in Greek, like a real text message
- Write ONLY in Greek. Do NOT include English translations, parenthetical or otherwise.
- If they don't understand a word, they will ask — that's part of learning
- Weave the target words naturally into what you're saying
- Use natural Greek grammar and sentence structure
- NEVER list vocabulary or make it feel like a flashcard or lesson
- Reference their actual interests and life when possible — use the NEWS CONTEXT below if available
- Match the time of day naturally
- Warm, casual tone — you're friends
- Keep it to plain text (no markdown, no HTML tags)

{f"RECENT CONVERSATION (for continuity):{chr(10)}{history_text}" if history_text else "This is the start of your conversation. Send a friendly opener."}

{f"NEWS CONTEXT (use this to make your message topical — mention a result, a headline, an upcoming event):{chr(10)}{news_context}" if news_context else ""}

Write your message now. Just the message text, nothing else."""


def compose_and_send(conn, config: Config) -> dict:
    """Full pipeline: select words -> generate message -> send -> record.

    Returns a dict with 'message', 'words', and 'telegram_response'.
    """
    words = select_words(conn)
    if not words:
        return {"error": "No words available. Import vocabulary first."}

    profile = get_full_profile(conn)
    history = _get_recent_messages(conn)
    news_context = fetch_news_context(profile)
    prompt = build_generation_prompt(profile, words, history, news_context=news_context)

    # Generate message via Claude
    import anthropic
    client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    response = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    message_text = response.content[0].text.strip()

    # Send via Telegram
    tg_response = send_message(
        config.telegram_bot_token,
        config.telegram_chat_id,
        message_text,
        parse_mode="",  # plain text
    )

    # Record in DB
    word_ids = json.dumps([w.word_id for w in words])
    telegram_msg_id = tg_response.get("result", {}).get("message_id")

    execute(
        conn,
        """INSERT INTO messages (direction, body, telegram_msg_id, target_word_ids)
           VALUES (?, ?, ?, ?)""",
        ("out", message_text, telegram_msg_id, word_ids),
    )

    # Record in send_log
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Get the message ID we just inserted
    last_msg = fetchone_dict(
        conn,
        "SELECT id FROM messages ORDER BY id DESC LIMIT 1",
    )
    msg_id = last_msg["id"] if last_msg else None

    execute(
        conn,
        "INSERT INTO send_log (sent_date, message_id) VALUES (?, ?)",
        (today, msg_id),
    )
    conn.commit()

    return {
        "message": message_text,
        "words": [{"greek": w.greek, "english": w.english} for w in words],
        "telegram_msg_id": telegram_msg_id,
    }
