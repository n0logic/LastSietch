"""Deterministic message triage — category, severity, and title shaping.

Pure functions, unit-tested without a live Discord. This is the keyword
pre-filter that decides whether a message is worth the (cheap, local) embedding
+ dedup pass at all, and how loudly to page the mods. No LLM here.

Supersedes the old tracker's classify_kind / summarize_title / looks_like_report.
"""

from __future__ import annotations

import re

TITLE_LIMIT = 90
_MIN_REPORT_LEN = 12

# Categories a watched message can fall into. CHATTER is the discard bucket.
CAT_BUG = "bug"
CAT_FEATURE = "feature"
CAT_QUESTION = "question"
CAT_REPORT = "report"  # player-report (someone misbehaving) — routes to mods
CAT_CHATTER = "chatter"

# Severities, low -> high. URGENT pages the mods immediately.
SEV_LOW = "low"
SEV_NORMAL = "normal"
SEV_HIGH = "high"
SEV_URGENT = "urgent"

CATEGORY_META = {
    CAT_BUG: {"emoji": "\U0001F41E", "label": "Bug"},
    CAT_FEATURE: {"emoji": "\U0001F4A1", "label": "Feature Request"},
    CAT_QUESTION: {"emoji": "\U00002753", "label": "Question"},
    CAT_REPORT: {"emoji": "\U0001F6A8", "label": "Player Report"},
}

_FEATURE_WORDS = (
    "feature", "request", "suggestion", "idea", "would be nice", "please add",
    "can you add", "could you add", "wish", "add a ", "support for", "it'd be",
    "it would be cool", "qol", "quality of life",
)
_BUG_WORDS = (
    "bug", "broken", "error", "crash", "fail", "doesn't", "does not",
    "not working", "isn't working", "glitch", "wrong", "404", "500",
    "stuck", "can't", "cannot", "missing", "typo", "unable to", "won't load",
    # Past-tense reports. The list above only had present-tense negations
    # ("doesn't"), so a player writing "I didn't receive the welcome gift" fell
    # through to CHATTER and was silently discarded (ticket #32, 2026-07-24).
    # These are the most natural phrasings for a grant/delivery that failed, and
    # polite reports are hit hardest: no "broken", no "bug", often no "?".
    # Deliberately paired with a verb rather than a bare "didn't"/"couldn't",
    # which fire constantly in ordinary chatter ("didn't know that").
    "didn't get", "did not get", "didn't receive", "did not receive",
    "didn't work", "did not work", "didn't show", "did not show",
    "didn't appear", "did not appear", "didn't spawn", "did not spawn",
    "never got", "never received", "never showed", "never appeared",
    "wasn't working", "weren't working", "hasn't worked", "haven't received",
    "haven't gotten", "no longer works", "stopped working",
)
_QUESTION_WORDS = (
    "how do i", "how to", "where is", "where do i", "what is", "what's the",
    "which", "anyone know", "is there a way", "can i ", "do i need",
    "?",
)
_REPORT_WORDS = (
    "cheating", "cheater", "hacker", "hacking", "exploiting", "griefing",
    "harassing", "harassment", "scammed", "scammer", "ban this", "report this",
    "toxic", "slur",
)

# Urgent: anything that means the server or a player is actively on fire.
_URGENT_WORDS = (
    "server down", "server is down", "is the server down", "can't log in",
    "cannot log in", "can't login", "cant login", "wont connect", "won't connect",
    "everyone disconnected", "exploit", "duping", "dupe", "lost my base",
    "lost everything", "got wiped", "hacker", "harassment", "harassing",
    "slur", "ddos", "all my stuff is gone",
)
_HIGH_WORDS = (
    "crash", "crashed", "stuck", "can't play", "cannot play", "lost", "broken",
    "not working", "down", "disconnected", "blocked", "softlock", "soft lock",
)


def _hits(low: str, words: tuple[str, ...]) -> bool:
    return any(w in low for w in words)


def classify_category(text: str) -> str:
    """Best-effort category from message text.

    Precedence: player-report > bug > feature > question. A message that trips
    none of the cue lists is CHATTER (ignored by the watcher).
    """
    low = text.lower()
    if _hits(low, _REPORT_WORDS):
        return CAT_REPORT
    is_bug = _hits(low, _BUG_WORDS)
    is_feature = _hits(low, _FEATURE_WORDS)
    if is_feature and not is_bug:
        return CAT_FEATURE
    if is_bug:
        return CAT_BUG
    if _hits(low, _QUESTION_WORDS):
        return CAT_QUESTION
    return CAT_CHATTER


def classify_severity(text: str, category: str = CAT_BUG) -> str:
    """Triage urgency from cue words. Player-reports floor at HIGH."""
    low = text.lower()
    if _hits(low, _URGENT_WORDS):
        return SEV_URGENT
    if category == CAT_REPORT:
        return SEV_HIGH
    if _hits(low, _HIGH_WORDS):
        return SEV_HIGH
    if category == CAT_FEATURE:
        return SEV_LOW
    return SEV_NORMAL


def looks_like_report(content: str) -> bool:
    """A human message with enough substance to bother triaging."""
    return len(content.strip()) >= _MIN_REPORT_LEN


def is_actionable(category: str) -> bool:
    """True for categories the watcher should turn into a ticket/FR."""
    return category in (CAT_BUG, CAT_FEATURE, CAT_QUESTION, CAT_REPORT)


def summarize_title(text: str, limit: int = TITLE_LIMIT) -> str:
    """First non-empty line, whitespace-collapsed, truncated for embeds/boards."""
    for line in text.splitlines():
        cleaned = re.sub(r"\s+", " ", line.strip())
        if cleaned:
            if len(cleaned) > limit:
                return cleaned[: limit - 1].rstrip() + "…"
            return cleaned
    return "(no description)"


def normalize_question(text: str) -> str:
    """Collapse a question to a cache/dedup key: lowercased, punctuation-stripped,
    whitespace-collapsed. Used by the qa_cache normalized-question hash."""
    low = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    return re.sub(r"\s+", " ", low).strip()
