"""Message composition — picks words, generates natural messages via Claude, sends via Telegram."""

from __future__ import annotations

import json
import logging
import random
import re
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

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
        if not headlines:
            logger.warning("Google News RSS returned no results for query: %s", query)
        else:
            logger.info("Fetched %d headlines for query: %s", len(headlines), query)
        return headlines
    except Exception:
        logger.exception("Google News RSS search failed for query: %s", query)
        return []


_POLITICAL_FEEDS = [
    {"name": "Guardian UK Politics", "url": "https://www.theguardian.com/politics/rss", "scope": "uk", "tag": "Guardian"},
    {"name": "Novara Media", "url": "https://novaramedia.com/feed", "scope": "uk", "tag": "Novara"},
    {"name": "eKathimerini", "url": "https://www.ekathimerini.com/news/rss", "scope": "greece", "tag": "eKathimerini"},
    {"name": "POLITICO Europe", "url": "https://www.politico.eu/feed/", "scope": "eu", "tag": "POLITICO"},
    {"name": "Tribune Magazine", "url": "https://tribunemag.co.uk/feed", "scope": "uk", "tag": "Tribune"},
    {"name": "Democracy Now", "url": "https://www.democracynow.org/democracynow.rss", "scope": "eu", "tag": "DemocracyNow"},
]


def _fetch_rss_items_rich(url: str, max_results: int = 3) -> list[dict]:
    """Fetch articles from a direct RSS feed URL with title, date, description, and source."""
    import xml.etree.ElementTree as ET

    try:
        resp = httpx.get(url, timeout=8, follow_redirects=True)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        items = []

        # Detect Atom feeds (namespace-prefixed <entry> elements)
        ns = ""
        if root.tag.startswith("{"):
            ns = root.tag.split("}")[0] + "}"
        entries = list(root.iter(f"{ns}entry")) if ns else list(root.iter("entry"))
        is_atom = len(entries) > 0

        if is_atom:
            elements = entries
        else:
            elements = list(root.iter("item"))

        for elem in elements:
            if is_atom:
                title = elem.findtext(f"{ns}title", "").strip()
                pub_date = elem.findtext(f"{ns}published", "") or elem.findtext(f"{ns}updated", "")
                desc_raw = elem.findtext(f"{ns}content", "") or elem.findtext(f"{ns}summary", "")
                source = ""
            else:
                title = elem.findtext("title", "").strip()
                pub_date = elem.findtext("pubDate", "")
                desc_raw = elem.findtext("description", "")
                source = elem.findtext("source", "")

            # Strip HTML tags from description
            desc_clean = re.sub(r"<[^>]+>", "", desc_raw).strip()
            # Truncate to 150 chars
            if len(desc_clean) > 150:
                desc_clean = desc_clean[:147] + "..."
            if title:
                items.append({
                    "title": title,
                    "pub_date": pub_date,
                    "description": desc_clean,
                    "source": source,
                })
            if len(items) >= max_results:
                break
        logger.info("Fetched %d items from %s", len(items), url)
        return items
    except Exception:
        logger.exception("RSS fetch failed for URL: %s", url)
        return []


def _fetch_curated_political_items(max_feeds: int = 2) -> list[dict]:
    """Sample curated political feeds and fetch rich items from each."""
    selected = random.sample(_POLITICAL_FEEDS, min(max_feeds, len(_POLITICAL_FEEDS)))
    results = []
    for feed in selected:
        items = _fetch_rss_items_rich(feed["url"], max_results=2)
        for item in items:
            item["tag"] = feed["tag"]
        results.extend(items)
    return results


def fetch_news_context(profile: dict) -> str:
    """Fetch curated political items + Google News headlines for the user's interests."""
    snippets: list[str] = []

    # 1. Curated political items (2 feeds × 2 articles with descriptions)
    political_items = _fetch_curated_political_items(max_feeds=2)
    for item in political_items:
        date_part = f" ({item['pub_date']})" if item["pub_date"] else ""
        desc_part = f" — {item['description']}" if item["description"] else ""
        snippets.append(f"[{item['tag']}] {item['title']}{date_part}{desc_part}")

    # 2. Google News search (existing logic — 2 random profile topics × 3 headlines)
    topics = _build_search_topics(profile)
    if topics:
        selected = random.sample(topics, min(2, len(topics)))
        for topic in selected:
            headlines = _fetch_rss_headlines(topic, max_results=3)
            for h in headlines:
                snippets.append(f"[{topic}] {h}")

    return "\n".join(snippets[:10]) if snippets else ""


def web_search(query: str, max_results: int = 5) -> str:
    """Search for specific factual info via Google News RSS. Use for fixture schedules, results, etc."""
    headlines = _fetch_rss_headlines(query, max_results=max_results)
    return "\n".join(headlines) if headlines else ""


_ACCENT_MAP = str.maketrans("άέήίόύώϊϋΐΰ", "αεηιουωιυιυ")


def _strip_accents(text: str) -> str:
    return text.translate(_ACCENT_MAP)


def _verify_words_in_message(words: list[CardState], message_text: str) -> tuple[list[CardState], list[CardState]]:
    """Check which target words actually appear in the generated message.

    Uses accent-normalized stem matching (first 4+ chars) to handle Greek
    inflections where accents shift (e.g. βελτίωση → βελτιώσεις).
    Returns (verified, dropped) tuples.
    """
    text_norm = _strip_accents(message_text.lower())
    verified, dropped = [], []

    for w in words:
        bare = re.sub(r"^(ο|η|το|οι|τα|τον|την|του|της|των)\s+", "", w.greek.lower())
        target = _strip_accents(bare if bare else w.greek.lower())
        if len(target) < 3:
            verified.append(w)  # Too short to verify reliably
            continue
        # Use a stem (first 4 chars min, or full word if shorter) for inflection tolerance
        stem_len = min(len(target), max(4, len(target) - 2))
        stem = target[:stem_len]
        pattern = r'(?<![α-ωϊϋ])' + re.escape(stem)
        if re.search(pattern, text_norm):
            verified.append(w)
        else:
            dropped.append(w)

    return verified, dropped


def _bold_target_words(message_text: str, words: list[CardState]) -> str:
    """Wrap target vocabulary words in <b> tags for Telegram HTML.

    HTML-escapes the full message first, then applies bold tags.
    """
    import html as html_mod
    escaped = html_mod.escape(message_text)

    for w in words:
        bare = re.sub(r"^(ο|η|το|οι|τα|τον|την|του|της|των)\s+", "", w.greek.lower())
        target = bare if bare else w.greek.lower()
        if len(target) < 3:
            continue
        stem_len = min(len(target), max(4, len(target) - 2))
        stem = target[:stem_len]
        # Match the stem at a Greek word boundary, capture the full word
        pattern = r'(?<![α-ωά-ώϊϋΐΰ])(' + re.escape(stem) + r'[α-ωά-ώϊϋΐΰ]*)(?![α-ωά-ώϊϋΐΰ])'
        escaped = re.sub(pattern, r'<b>\1</b>', escaped, count=1, flags=re.IGNORECASE)

    return escaped


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

You are texting a friend who is learning Greek. They understand a lot already. Write to them the way you'd text any Greek friend — naturally, casually, all in Greek. You follow UK, Greek, and EU politics closely — you have real opinions, you take sides, you're not neutral.

ABOUT THEM:
{profile_text}

TIME: {time_context}

{word_section}
RULES:
- Write 1-3 short sentences in Greek, like a real text message
- Write ONLY in Greek. Do NOT include English translations, parenthetical or otherwise.
- You MUST use ALL of the target words above. Every single one. This is critical.
- If a word doesn't fit the current topic, shift the topic slightly to include it
- Use natural Greek grammar and sentence structure — inflect words as needed
- NEVER list vocabulary or make it feel like a flashcard or lesson
- Reference their actual interests and life when possible — use the NEWS CONTEXT below if available
- Match the time of day naturally
- Warm, casual tone — you're friends
- Keep it to plain text (no markdown, no HTML tags, no bold)
- If there's political news in the NEWS CONTEXT, share a sharp take — say what you actually think, pick a side, react like a real person

{f"RECENT CONVERSATION (for continuity):{chr(10)}{history_text}" if history_text else "This is the start of your conversation. Send a friendly opener."}

{f"NEWS CONTEXT (pick the most interesting item and react to it with an actual opinion — don't just report it, respond to it):{chr(10)}{news_context}" if news_context else ""}

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

    # Verify which target words Claude actually used
    verified, dropped = _verify_words_in_message(words, message_text)
    if dropped:
        logger.info("Words not used in message: %s",
                     ", ".join(w.greek for w in dropped))

    # Bold target words and send as HTML
    html_text = _bold_target_words(message_text, verified)
    tg_response = send_message(
        config.telegram_bot_token,
        config.telegram_chat_id,
        html_text,
        parse_mode="HTML",
    )

    # Only record verified word IDs — don't track words the user never saw
    word_ids = json.dumps([w.word_id for w in verified])
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
        "words": [{"greek": w.greek, "english": w.english} for w in verified],
        "words_dropped": [{"greek": w.greek, "english": w.english} for w in dropped],
        "telegram_msg_id": telegram_msg_id,
    }
