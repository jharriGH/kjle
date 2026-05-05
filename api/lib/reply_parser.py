"""
KJLE — Inbound reply parser for unsubscribe detection
File: api/lib/reply_parser.py

Conservative keyword matcher. False-negatives over false-positives, per spec:
    "Conservative — false negatives are better than false positives."

Two-tier matching:
  Tier 1 (high-confidence phrases): multi-word imperatives that are
    unambiguous unsubscribe requests. Match anywhere in the message via
    word boundaries. e.g., "remove me", "do not call", "take me off".

  Tier 2 (single-word keywords): "stop", "unsubscribe", "no more". These
    only match if the message is ≤3 words total — avoids false positives
    where the keyword is buried in a legitimate sentence (the canonical
    trap: "I'm interested, please don't stop sending" → must be False).

Not auto-wired anywhere in Phase 3 — this helper exists so future code
(e.g., a future ReachInbox reply webhook) can use it.
"""
from __future__ import annotations

import re
from typing import Optional

# ── High-confidence phrases (Tier 1) ─────────────────────────────────────────
# Multi-word imperatives. Match anywhere in the message via word boundaries.
HIGH_CONFIDENCE_PHRASES: list[str] = [
    "remove me",
    "remove from list",
    "do not call",
    "do not contact",
    "take me off",
    "opt out",
    "unsubscribe me",
]

# ── Single-word keywords (Tier 2) ────────────────────────────────────────────
# Match only if message word-count ≤ SINGLE_WORD_MAX_WORDS.
SINGLE_WORD_KEYWORDS: list[str] = [
    "stop",
    "unsubscribe",
    "no more",  # technically two words but functions as a single short imperative
]

SINGLE_WORD_MAX_WORDS: int = 3


def _word_count(text: str) -> int:
    """Whitespace-split word count. Treats contractions as single words."""
    return len(text.split())


def is_unsubscribe_reply(text: str) -> tuple[bool, Optional[str]]:
    """
    Detect whether a reply text is an unsubscribe request.

    Returns:
        (True, matched_keyword)  — text appears to request unsubscribe
        (False, None)            — no match, or empty/None input

    Conservative by design. See module docstring for matching tiers and
    the canonical false-positive guard.
    """
    if not text:
        return False, None

    lower = text.lower()

    # Tier 1: high-confidence phrases — match anywhere with word boundaries
    for phrase in HIGH_CONFIDENCE_PHRASES:
        pattern = r"\b" + re.escape(phrase) + r"\b"
        if re.search(pattern, lower):
            return True, phrase

    # Tier 2: single-word keywords — only on short messages
    if _word_count(text) <= SINGLE_WORD_MAX_WORDS:
        for kw in SINGLE_WORD_KEYWORDS:
            pattern = r"\b" + re.escape(kw) + r"\b"
            if re.search(pattern, lower):
                return True, kw

    return False, None
