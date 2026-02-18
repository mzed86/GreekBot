"""Implicit SRS from conversation — assesses word understanding from replies.

When the user replies, Claude analyzes whether they demonstrated understanding
of recently sent Greek words, and extracts new profile learnings.
"""

from __future__ import annotations

import json

import re

from greekapp.config import Config


def _parse_json_lenient(raw: str) -> dict | None:
    """Try hard to parse JSON from Claude's response."""
    # Direct parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Extract JSON block from surrounding text
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start >= 0 and end > start:
        fragment = raw[start:end]
        try:
            return json.loads(fragment)
        except json.JSONDecodeError:
            pass

        # Common fix: remove trailing commas before } or ]
        cleaned = re.sub(r",\s*([}\]])", r"\1", fragment)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        # Try fixing unescaped newlines in strings
        cleaned2 = re.sub(r'(?<=": ")([^"]*?)(?=")', lambda m: m.group(1).replace("\n", " "), cleaned)
        try:
            return json.loads(cleaned2)
        except json.JSONDecodeError:
            pass

    return None
from greekapp.db import execute, fetchall_dicts, fetchone_dict
from greekapp.profile import get_full_profile, profile_to_prompt_text, save_learned_note
from greekapp.srs import CardState, DEFAULT_EASE, record_review


# Question markers in Greek and English
_QUESTION_PATTERNS = re.compile(
    r"[;?]|"  # Greek/English question marks
    r"πότε|πού|ποιος|τι |πώς|πόσο|"  # Greek question words
    r"when|where|who|what|how|which|"
    r"fixture|schedule|match|score|result|next game|"
    r"ματς|αγώνα|πρόγραμμα|βαθμολογ",
    re.IGNORECASE,
)


def _maybe_search(user_reply: str, profile: dict) -> str:
    """If the user's reply looks like a factual question, search the web for context."""
    if not _QUESTION_PATTERNS.search(user_reply):
        return ""

    from greekapp.messenger import web_search

    # Build a search query from the user's message
    # Strip Greek question marks and clean up
    query = user_reply.replace(";", "").replace("?", "").strip()

    # Add context from profile if the query is short
    if len(query.split()) < 5:
        sports = profile.get("interests", {}).get("sports", [])
        for s in sports:
            if s and "fan" not in s.lower():
                query = f"{s} {query}"
                break

    results = web_search(query, max_results=5)
    return results


def _get_recent_outgoing_words(conn, limit: int = 3) -> list[dict]:
    """Get words from recently sent messages for assessment."""
    recent = fetchall_dicts(
        conn,
        """SELECT target_word_ids FROM messages
           WHERE direction = 'out' AND target_word_ids IS NOT NULL
           ORDER BY created_at DESC
           LIMIT ?""",
        (limit,),
    )

    word_ids = set()
    for msg in recent:
        try:
            ids = json.loads(msg["target_word_ids"])
            word_ids.update(ids)
        except (json.JSONDecodeError, TypeError):
            continue

    if not word_ids:
        return []

    # Fetch the actual word details
    words = []
    for wid in word_ids:
        row = fetchone_dict(
            conn,
            "SELECT id, greek, english FROM words WHERE id = ?",
            (wid,),
        )
        if row:
            words.append(row)

    return words


def _get_word_card_state(conn, word_id: int) -> CardState:
    """Load the current SRS state for a word."""
    row = fetchone_dict(
        conn,
        """SELECT w.id, w.greek, w.english,
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
           WHERE w.id = ?""",
        (DEFAULT_EASE, word_id),
    )
    if not row:
        raise ValueError(f"Word {word_id} not found")

    from datetime import datetime
    lr = None
    if row["last_review"]:
        lr = datetime.fromisoformat(str(row["last_review"]))

    return CardState(
        word_id=row["id"],
        greek=row["greek"],
        english=row["english"],
        ease_factor=row["ease_factor"],
        interval=row["interval"],
        repetition=row["repetition"],
        last_review=lr,
    )


def _get_recent_conversation(conn, limit: int = 8) -> list[dict]:
    """Get recent conversation for assessment context."""
    return fetchall_dicts(
        conn,
        """SELECT direction, body, created_at
           FROM messages
           ORDER BY created_at DESC
           LIMIT ?""",
        (limit,),
    )


def _build_assessment_prompt(
    user_reply: str,
    words: list[dict],
    conversation: list[dict],
    profile: dict,
    search_context: str = "",
    due_words: list | None = None,
) -> str:
    """Build prompt for Claude to assess understanding + extract learnings."""
    profile_text = profile_to_prompt_text(profile)

    word_list = "\n".join(
        f"  - {w['greek']} = {w['english']} (word_id: {w['id']})"
        for w in words
    )

    due_section = ""
    if due_words:
        due_list = ", ".join(f"{w.greek} ({w.english})" for w in due_words[:8])
        due_section = f"\nWORDS FROM THEIR STUDY LIST (use one of these in your reply if naturally possible): {due_list}\n"

    conv_lines = []
    for msg in reversed(conversation):
        prefix = "Bot" if msg["direction"] == "out" else "User"
        conv_lines.append(f"{prefix}: {msg['body']}")
    conv_text = "\n".join(conv_lines)

    search_section = ""
    if search_context:
        search_section = f"""
CURRENT NEWS/FACTS (use these to answer factual questions accurately — do NOT make up scores, fixtures, or dates):
{search_context}
"""

    return f"""You are assessing a Greek language learner's understanding based on their reply in a casual conversation.

RECENT CONVERSATION:
{conv_text}

USER'S LATEST REPLY:
{user_reply}

GREEK WORDS RECENTLY SENT:
{word_list}

USER PROFILE:
{profile_text}
{search_section}{due_section}
Analyze the user's reply and respond with ONLY valid JSON (no other text):

{{
  "word_assessments": [
    {{
      "word_id": <int>,
      "greek": "<word>",
      "quality": <int 0-5>,
      "reasoning": "<brief explanation>"
    }}
  ],
  "corrections": [
    {{
      "wrong": "<what the user wrote>",
      "correct": "<correct Greek form>",
      "english": "<English meaning>",
      "type": "<vocab|grammar|spelling>",
      "explanation": "<brief explanation of the mistake in simple Greek>"
    }}
  ],
  "profile_learnings": [
    {{
      "category": "<work|hobby|preference|life_event|other>",
      "content": "<what you learned about them>"
    }}
  ],
  "reply": "<your reply — MUST be entirely in Greek, no English at all>"
}}

QUALITY SCALE for word_assessments:
  5 - Used the Greek word correctly in their reply
  4 - Responded showing clear understanding of the word's meaning
  3 - Seemed to understand but response is ambiguous
  2 - Asked what the word means or seemed confused
  1 - Ignored the Greek word entirely
  0 - Clearly misunderstood the word

CORRECTIONS — analyze the user's Greek for:
  - Vocabulary mistakes (wrong word used, e.g. "καιρό" when they meant "ώρα")
  - Grammar errors (wrong case, wrong verb conjugation, wrong article gender)
  - Spelling/accent errors (missing or wrong accent marks, wrong letters)
  Only include actual mistakes. Empty array if their Greek was correct.
  For each correction, "correct" should be the word in its correct form (dictionary/base form).

Only assess words where you have signal from their reply. Skip words the conversation didn't touch on.
For profile_learnings, note anything new you learned about the user (a project, a plan, a preference, etc). Empty array if nothing new.
For the reply: write ENTIRELY in Greek — no English whatsoever. You're a Greek friend texting naturally. If the user made mistakes, gently use the correct form in your reply (don't lecture, just model the right usage). If they ask what a word means, explain it in simple Greek. If they ask you to teach them a word, ONLY use words from the WORDS FROM THEIR STUDY LIST above — never invent your own. If the conversation touched on a political topic, continue that thread — push back, agree loudly, add a new angle, or ask them what they think. Keep it to 1-3 sentences."""


def _process_correction(conn, correction: dict) -> None:
    """Process a correction — ensure the correct word is in the vocab and schedule it for review.

    If the word already exists, record a low-quality review so it comes back sooner.
    If it's a new word, add it to the vocabulary and it'll naturally appear as a due card.
    """
    correct = correction.get("correct", "").strip()
    english = correction.get("english", "").strip()
    if not correct or not english:
        return

    # Check if the correct form already exists in our vocabulary
    existing = fetchone_dict(conn, "SELECT id FROM words WHERE greek = ?", (correct,))

    if existing:
        # Word exists — record a quality=1 review to bring it back for practice
        try:
            card = _get_word_card_state(conn, existing["id"])
            record_review(conn, card, 1)  # Wrong but recognized
        except ValueError:
            pass
    else:
        # New word — add it to vocabulary tagged as learned-from-mistake
        error_type = correction.get("type", "vocab")
        execute(
            conn,
            """INSERT INTO words (greek, english, tags)
               VALUES (?, ?, ?)""",
            (correct, english, f"correction:{error_type}"),
        )
        conn.commit()


def assess_and_reply(conn, config: Config, user_reply: str) -> dict:
    """Process a user reply: assess understanding, learn profile, generate reply.

    Returns dict with 'reply', 'assessments', and 'learnings'.
    """
    # Record the incoming message
    execute(
        conn,
        "INSERT INTO messages (direction, body) VALUES (?, ?)",
        ("in", user_reply),
    )
    conn.commit()

    # Get context
    words = _get_recent_outgoing_words(conn)
    conversation = _get_recent_conversation(conn)
    profile = get_full_profile(conn)

    # If the reply looks like a factual question, do a web search for context
    search_context = _maybe_search(user_reply, profile)

    # Load due words from SRS so replies can use them
    from greekapp.srs import load_due_cards
    due_words = load_due_cards(conn, limit=10)

    # If no words to assess, just generate a reply
    if not words:
        return _simple_reply(conn, config, user_reply, conversation, profile, search_context, due_words)

    # Ask Claude to assess + reply
    import anthropic
    prompt = _build_assessment_prompt(user_reply, words, conversation, profile, search_context, due_words)
    client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    response = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()

    # Parse JSON response — Claude sometimes returns slightly malformed JSON
    data = _parse_json_lenient(raw)
    if data is None:
        return _simple_reply(conn, config, user_reply, conversation, profile, search_context)

    # Apply SRS updates
    assessments = data.get("word_assessments", [])
    for assessment in assessments:
        try:
            card = _get_word_card_state(conn, assessment["word_id"])
            record_review(conn, card, assessment["quality"])
        except (ValueError, KeyError):
            continue

    # Process corrections — add mistaken words to SRS
    corrections = data.get("corrections", [])
    for correction in corrections:
        _process_correction(conn, correction)

    # Store profile learnings
    learnings = data.get("profile_learnings", [])
    for learning in learnings:
        if learning.get("content"):
            save_learned_note(conn, learning.get("category", "other"), learning["content"])

    # Record + send the reply
    reply_text = data.get("reply", "")
    if reply_text:
        _record_and_send_reply(conn, config, reply_text)

    return {
        "reply": reply_text,
        "assessments": assessments,
        "corrections": corrections,
        "learnings": learnings,
    }


def _simple_reply(conn, config: Config, user_reply: str, conversation: list[dict], profile: dict, search_context: str = "", due_words: list | None = None) -> dict:
    """Generate a simple conversational reply without word assessment."""
    profile_text = profile_to_prompt_text(profile)

    conv_lines = []
    for msg in reversed(conversation[:6]):
        prefix = "Bot" if msg["direction"] == "out" else "User"
        conv_lines.append(f"{prefix}: {msg['body']}")
    conv_text = "\n".join(conv_lines)

    search_section = ""
    if search_context:
        search_section = f"""
CURRENT NEWS/FACTS (use these to answer factual questions accurately — do NOT make up scores, fixtures, or dates):
{search_context}
"""

    due_section = ""
    if due_words:
        due_list = ", ".join(f"{w.greek} ({w.english})" for w in due_words[:8])
        due_section = f"\nWORDS FROM THEIR STUDY LIST (use one of these in your reply if naturally possible, and ONLY use these if they ask to learn a word): {due_list}\n"

    prompt = f"""You are a Greek friend texting in Greek. Continue this conversation naturally. You follow UK, Greek, and EU politics closely and have real opinions. If the user brings up anything political, react with a genuine take — don't be neutral.

CONVERSATION:
{conv_text}

ABOUT THEM:
{profile_text}
{search_section}{due_section}
Reply in 1-3 sentences ENTIRELY in Greek. No English at all. If they ask what a word means, explain it in simple Greek. If they ask a factual question, use the NEWS/FACTS above to answer accurately. If they ask you to teach them a word, ONLY pick from the WORDS FROM THEIR STUDY LIST above — never invent your own. Just the message, nothing else."""

    import anthropic
    client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    response = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    reply_text = response.content[0].text.strip()
    _record_and_send_reply(conn, config, reply_text)

    return {"reply": reply_text, "assessments": [], "learnings": []}


def _find_vocab_words_in_text(conn, text: str) -> list[int]:
    """Find word IDs from the vocabulary that appear in a text.

    Uses word-boundary matching to avoid false substring hits.
    Only matches words with 3+ characters to skip tiny articles/particles.
    """
    all_words = fetchall_dicts(conn, "SELECT id, greek FROM words")

    text_lower = text.lower()
    found = []
    for w in all_words:
        greek = w["greek"].lower()
        # Strip article prefixes
        bare = re.sub(r"^(ο|η|το|οι|τα|τον|την|του|της|των)\s+", "", greek)
        # Skip very short words (articles, particles) — too many false matches
        target = bare if bare else greek
        if len(target) < 3:
            continue
        # Word boundary match: look for the word surrounded by non-letter chars
        # Greek Unicode range for word boundary
        pattern = r'(?<![α-ωά-ώ])' + re.escape(target) + r'(?![α-ωά-ώ])'
        if re.search(pattern, text_lower):
            found.append(w["id"])
    return found


def _extract_taught_words_from_reply(conn, reply_text: str) -> list[int]:
    """Extract Greek words being taught in a reply and ensure they're in the vocab.

    Looks for patterns like quoted words, words being explained, etc.
    If a word isn't in the vocab, adds it.
    """
    # Find words in quotes (e.g., "αναβάθμιση") or after "λέξη" (word)
    quoted = re.findall(r'[""«]([α-ωά-ώΑ-ΩΆ-Ώ]+)[""»]', reply_text)
    # Also find "X σημαίνει" or "X = " patterns
    explained = re.findall(r'([Α-ΩΆ-Ώα-ωά-ώ]{3,})\s*(?:σημαίνει|=|είναι όταν)', reply_text)

    candidates = set(w.lower() for w in quoted + explained if len(w) >= 3)

    word_ids = []
    for word in candidates:
        # Check if already in vocab (with or without article)
        existing = fetchone_dict(conn, "SELECT id FROM words WHERE lower(greek) = ?", (word,))
        if not existing:
            # Try with common articles
            for article in ["", "ο ", "η ", "το "]:
                existing = fetchone_dict(
                    conn, "SELECT id FROM words WHERE lower(greek) = ?",
                    (f"{article}{word}",),
                )
                if existing:
                    break

        if existing:
            word_ids.append(existing["id"])
        else:
            # New word — add it to vocab so it gets tracked
            # We don't know the English yet, but we can extract it from context
            english = _guess_english_from_context(reply_text, word)
            execute(
                conn,
                "INSERT INTO words (greek, english, tags) VALUES (?, ?, ?)",
                (word, english, "conversation"),
            )
            conn.commit()
            new_row = fetchone_dict(conn, "SELECT id FROM words WHERE greek = ?", (word,))
            if new_row:
                word_ids.append(new_row["id"])

    return word_ids


def _guess_english_from_context(text: str, greek_word: str) -> str:
    """Try to extract the English translation from the surrounding text."""
    # Look for patterns like "word = english" or "word, δηλαδή english"
    patterns = [
        rf'{re.escape(greek_word)}\s*=\s*(\w+)',
        rf'{re.escape(greek_word)}.*?δηλαδή\s+(\w+)',
        rf'(\w+)\s+δηλαδή.*?{re.escape(greek_word)}',
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return m.group(1)
    return "(from conversation)"


def _record_and_send_reply(conn, config: Config, reply_text: str) -> None:
    """Record reply in DB and send via Telegram. Auto-tags vocab words used."""
    from greekapp.telegram import send_message

    tg_resp = send_message(
        config.telegram_bot_token,
        config.telegram_chat_id,
        reply_text,
        parse_mode="",
    )
    telegram_msg_id = tg_resp.get("result", {}).get("message_id")

    # Find taught words (quoted/explained) and add to vocab if new
    taught_ids = _extract_taught_words_from_reply(conn, reply_text)
    # Also find existing vocab words used in the reply
    existing_ids = _find_vocab_words_in_text(conn, reply_text)
    # Merge — taught words take priority
    all_ids = list(dict.fromkeys(taught_ids + existing_ids))
    word_ids_json = json.dumps(all_ids) if all_ids else None

    execute(
        conn,
        """INSERT INTO messages (direction, body, telegram_msg_id, target_word_ids)
           VALUES (?, ?, ?, ?)""",
        ("out", reply_text, telegram_msg_id, word_ids_json),
    )
    conn.commit()
