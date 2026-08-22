"""Gameplay knowledge retrieval (RAG) for player questions.

When a player asks a gameplay question (professions, classes,
mounts, talents, gold, dungeons, zones), retrieve the most
relevant WotLK 3.3.5a knowledge from wow_knowledge.json and let
the caller inject it into the bot's prompt so the answer is
accurate instead of generic.

Deliberately better than naive keyword matching:
  - question-intent gate: only fires for actual questions, so we
    never stuff knowledge into casual chat
  - stopword filtering: common question words ("how", "what",
    "как", "что") are ignored so they don't match everything
  - token-overlap scoring across title + keywords + content, not
    exact keyword equality
  - bilingual: English and Russian keywords both match
  - bounded and self-contained: a JSON lookup + scoring, no
    extra LLM call, no schema
"""

import json
import logging
import os
import re

logger = logging.getLogger(__name__)

_QUESTION_WORDS = {
    "how", "what", "where", "who", "why", "which", "when",
    "can", "could", "should", "tell", "explain", "help",
    "как", "что", "где", "кто", "почему", "когда", "какой",
    "какая", "какое", "какие", "куда", "зачем", "можно",
    "подскажи", "расскажи", "сколько",
}

_STOPWORDS = _QUESTION_WORDS | {
    "the", "a", "an", "to", "in", "of", "for", "and", "or",
    "my", "i", "me", "you", "your", "we", "on", "at", "with",
    "get", "got", "become", "make", "learn", "find", "do",
    "does", "is", "are", "it", "this", "that", "best", "good",
    "нужно", "надо", "как", "для", "меня", "мне", "есть",
    "стать", "научиться", "научится", "могу", "смогу",
    "сможет", "получить", "стоит", "стоят", "заработать",
    "нужна", "нужен", "хочу", "хочешь",
}

_knowledge = None


def _load_knowledge():
    global _knowledge
    if _knowledge is not None:
        return _knowledge
    try:
        path = os.path.join(
            os.path.dirname(__file__), "wow_knowledge.json"
        )
        with open(path, "r", encoding="utf-8") as f:
            _knowledge = json.load(f)
    except Exception:
        logger.warning(
            "Could not load wow_knowledge.json; "
            "gameplay Q&A disabled", exc_info=True,
        )
        _knowledge = []
    return _knowledge


def _normalize(text):
    return set(re.sub(r"[^\w\s]", " ", str(text).lower()).split())


def is_gameplay_question(text):
    """Cheap bilingual question-intent gate.

    Returns True when the message looks like a question:
    ends with '?', or contains a question word. Deliberately
    conservative so we only pay the (tiny) retrieval cost on
    real questions.
    """
    t = (text or "").strip()
    if not t or len(t) < 3:
        return False
    if "?" in t:
        return True
    return bool(_normalize(t) & _QUESTION_WORDS)


def _common_prefix_len(a, b):
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return n


def _token_matches(q, hay):
    """Return True if query token q matches any hay token.

    Exact match, or a shared stem/prefix of at least 4 characters
    so inflected Russian forms ("горному делу" vs "горное дело")
    and English plurals ("alchemists" vs "alchemist") still match.
    4 is a low-false-positive floor for both languages.
    """
    for h in hay:
        if q == h:
            return True
        if _common_prefix_len(q, h) >= 4:
            return True
    return False


def retrieve_wow_knowledge(text, max_items=3, threshold=1):
    """Return up to max_items relevant knowledge strings.

    Each returned string is "Title: content". Entries are scored
    by how many of the question's non-stopword tokens match
    their title/keywords (content is deliberately NOT matched --
    it is reference prose for the LLM and matching against it
    causes false positives on common words). Only entries scoring
    at least `threshold` are returned, best first.
    """
    entries = _load_knowledge()
    if not entries:
        return []
    q_tokens = list(_normalize(text) - _STOPWORDS)
    if not q_tokens:
        return []
    scored = []
    for entry in entries:
        hay = _normalize(entry.get("title", ""))
        for kw in entry.get("keywords", []) + entry.get(
            "keywords_ru", []
        ):
            hay |= _normalize(kw)
        overlap = sum(
            1 for q in q_tokens if _token_matches(q, hay)
        )
        if overlap >= threshold:
            scored.append((overlap, entry))
    scored.sort(key=lambda pair: (-pair[0], pair[1].get("title", "")))
    return [
        "{0}: {1}".format(e.get("title", ""), e.get("content", ""))
        for _, e in scored[:max_items]
    ]
