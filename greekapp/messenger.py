"""Message composition — picks words, generates natural messages via Claude, sends via Telegram.

Supports two message modes:
  1. Teaching mode (default): Embeds target vocabulary into natural Greek messages.
  2. Recall mode (~30%): Tests active recall with translation/meaning/cloze prompts.
"""

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
from greekapp.srs import CardState, DEFAULT_EASE, load_due_cards, get_retention_stats
from greekapp.telegram import send_message


RECALL_PROBABILITY = 0.3  # 30% of proactive messages are recall prompts


def select_words(conn, new_limit: int = 3, review_limit: int = 3) -> list[CardState]:
    """Pick a mix of new + due review words for a message.

    Prioritizes:
    1. Recently failed words (quality < 3 in last 24h) — re-introduce for within-session practice
    2. Learning-phase words (rep < 2) that are due — they need a second look
    3. Regular review words (seen before, due)
    4. New words to fill remaining slots

    Returns 3-5 words.
    """
    due = load_due_cards(conn, limit=15)

    # Split by category
    learning_words = [c for c in due if c.last_review is not None and c.is_learning]
    review_words = [c for c in due if c.last_review is not None and not c.is_learning]
    new_words = [c for c in due if c.last_review is None]

    # Build selection: learning phase first (they need reinforcement), then review, then new
    selected: list[CardState] = []
    selected.extend(learning_words[:2])  # Up to 2 learning-phase words
    remaining = max(0, review_limit - len(selected))
    selected.extend(review_words[:remaining])
    remaining_slots = max(0, 5 - len(selected))
    selected.extend(new_words[:min(new_limit, remaining_slots)])

    # If we still have fewer than 3, grab whatever is available
    if len(selected) < 3:
        extras = [c for c in due if c not in selected]
        selected.extend(extras[:3 - len(selected)])

    random.shuffle(selected)
    return selected


def select_recall_words(conn) -> list[CardState]:
    """Pick 1-2 review words for active recall testing.

    Only picks words that have been seen before (not brand new) so the user
    actually has something to recall. Prefers words that are due and have
    moderate intervals (not too new, not too well-known).
    """
    due = load_due_cards(conn, limit=20)
    # Only review words (seen at least once, past learning phase)
    candidates = [c for c in due if c.last_review is not None and c.repetition >= 2]

    if not candidates:
        # Fall back to any seen words
        candidates = [c for c in due if c.last_review is not None]

    if not candidates:
        return []

    # Prefer words with moderate intervals (1-30 days) — sweet spot for recall testing
    moderate = [c for c in candidates if 1.0 <= c.interval <= 30.0]
    if moderate:
        candidates = moderate

    return random.sample(candidates, min(2, len(candidates)))


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

    # 1. Curated political items (2 feeds x 2 articles with descriptions)
    political_items = _fetch_curated_political_items(max_feeds=2)
    for item in political_items:
        date_part = f" ({item['pub_date']})" if item["pub_date"] else ""
        desc_part = f" — {item['description']}" if item["description"] else ""
        snippets.append(f"[{item['tag']}] {item['title']}{date_part}{desc_part}")

    # 2. Google News search (existing logic — 2 random profile topics x 3 headlines)
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
    inflections where accents shift (e.g. βελτίωση -> βελτιώσεις).
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


def build_recall_prompt(
    profile: dict,
    words: list[CardState],
    history: list[dict],
) -> str:
    """Build a prompt for active recall testing.

    Generates a natural-sounding message that tests whether the user
    can recall specific vocabulary. Uses varied formats:
    - Translation recall (EN -> GR): "Πώς λέμε X στα ελληνικά;"
    - Meaning recall (GR -> ?): "Θυμάσαι τι σημαίνει X;"
    - Cloze/context: A sentence with a blank or hint
    """
    profile_text = profile_to_prompt_text(profile)

    word_items = []
    for w in words:
        word_items.append(f"  - {w.greek} = {w.english} (word_id: {w.word_id})")
    word_list = "\n".join(word_items)

    # Recent conversation for continuity
    history_text = ""
    if history:
        history_lines = []
        for msg in reversed(history[:4]):
            prefix = "You" if msg["direction"] == "out" else "Them"
            history_lines.append(f"{prefix}: {msg['body']}")
        history_text = "\n".join(history_lines)

    recall_type = random.choice(["translate", "meaning", "cloze"])

    type_instructions = {
        "translate": "Ask them how to say the English meaning in Greek. Be casual, like you forgot the word yourself. Example: 'Πώς λέμε improvement στα ελληνικά; Το ξέχασα...'",
        "meaning": "Use the Greek word and ask if they remember what it means. Example: 'Θυμάσαι τι σημαίνει βελτίωση; Μου το είπες πρόσφατα...'",
        "cloze": "Write a natural Greek sentence with a blank (___) where the target word should go, and give a hint. Example: 'Χρειαζόμαστε μεγάλη ___ στην οικονομία (starts with β)'. The hint can be the first letter or a brief description.",
    }

    return f"""You are a Greek friend casually testing your friend's vocabulary. Write in Greek.

ABOUT THEM:
{profile_text}

WORDS TO TEST (pick ONE word to quiz them on):
{word_list}

RECALL TYPE: {recall_type}
{type_instructions[recall_type]}

{f"RECENT CONVERSATION:{chr(10)}{history_text}" if history_text else ""}

RULES:
- Pick ONE word from the list above to test
- Make it feel natural — like you're having a conversation, not giving a quiz
- Write entirely in Greek (except the English word if using translate mode)
- Keep it casual and friendly, 1-2 sentences max
- Don't give away the answer!

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
        "mode": "teaching",
    }


def compose_recall_and_send(conn, config: Config) -> dict:
    """Active recall pipeline: pick review words -> generate recall prompt -> send.

    Returns a dict with 'message', 'words', 'mode': 'recall'.
    """
    words = select_recall_words(conn)
    if not words:
        # Fall back to teaching mode if no recall candidates
        return compose_and_send(conn, config)

    profile = get_full_profile(conn)
    history = _get_recent_messages(conn)
    prompt = build_recall_prompt(profile, words, history)

    # Generate recall message via Claude
    import anthropic
    client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    response = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    message_text = response.content[0].text.strip()

    # Send the recall prompt
    tg_response = send_message(
        config.telegram_bot_token,
        config.telegram_chat_id,
        message_text,
        parse_mode="",
    )

    # Record the recall words as targets so the assessor knows what to evaluate
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
    last_msg = fetchone_dict(conn, "SELECT id FROM messages ORDER BY id DESC LIMIT 1")
    msg_id = last_msg["id"] if last_msg else None
    execute(conn, "INSERT INTO send_log (sent_date, message_id) VALUES (?, ?)", (today, msg_id))
    conn.commit()

    return {
        "message": message_text,
        "words": [{"greek": w.greek, "english": w.english} for w in words],
        "telegram_msg_id": telegram_msg_id,
        "mode": "recall",
    }


def should_use_recall(conn) -> bool:
    """Decide whether to use recall mode for the next proactive message.

    Uses RECALL_PROBABILITY (~30%) but adapts based on retention:
    - If retention is dropping, increase recall frequency
    - If retention is high, keep the mix interesting with more teaching
    """
    stats = get_retention_stats(conn)

    prob = RECALL_PROBABILITY

    # If retention is declining, do more recall to reinforce
    if stats["quality_trend"] == "declining":
        prob = min(0.5, prob + 0.15)
    # If retention is very high, slightly reduce recall
    elif stats["recent_retention"] > 85 and stats["recent_reviews"] > 10:
        prob = max(0.15, prob - 0.1)

    return random.random() < prob
