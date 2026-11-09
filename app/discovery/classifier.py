"""Deterministic text and metadata relevance classification.

The classifier never inspects pixels and never treats a search query or URL as
proof. Strong page evidence is required for ``verified``; ambiguous evidence is
staged in ``dumpster``. This is intentionally conservative because automatic
verified media can be archived without another human gate.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

VERIFIED = "verified"
DUMPSTER = "dumpster"
REJECTED = "rejected"

_TIFA_TOKEN = re.compile(r"(?<![a-z0-9])tifa(?![a-z0-9])", re.IGNORECASE)
_LOCKHART_TOKEN = re.compile(r"(?<![a-z0-9])lockhart(?![a-z0-9])", re.IGNORECASE)
_TIFA_LOCKHART = re.compile(r"(?<![a-z0-9])tifa\s+lockhart(?![a-z0-9])", re.IGNORECASE)
_FF7_TERMS = (
    re.compile(r"(?<![a-z0-9])final\s+fantasy\s+(?:vii|7)(?![a-z0-9])", re.IGNORECASE),
    re.compile(r"(?<![a-z0-9])ff\s?(?:vii|7)(?![a-z0-9])", re.IGNORECASE),
)
_STRONG_CONTEXT_TERMS = (
    re.compile(r"(?<![a-z0-9])nibelheim(?![a-z0-9])", re.IGNORECASE),
    re.compile(r"(?<![a-z0-9])seventh\s+heaven(?![a-z0-9])", re.IGNORECASE),
)
_TAG_SPLIT = re.compile(r"[\s,;|#]+")


def _clean(value: str | None, limit: int = 20_000) -> str:
    if not value:
        return ""
    return value.strip()[:limit]


def _join(values: Iterable[str | None]) -> str:
    return " ".join(_clean(value, 4_000) for value in values if value)


def _tag_text(tags: Sequence[str] | str | None) -> str:
    if tags is None:
        return ""
    if isinstance(tags, str):
        return _clean(tags, 4_000)
    return " ".join(_clean(tag, 200) for tag in tags if tag)


@dataclass(frozen=True, slots=True)
class Classification:
    status: str
    score: int
    reasons: tuple[str, ...]
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def is_verified(self) -> bool:
        return self.status == VERIFIED

    @property
    def allows_child_crawl(self) -> bool:
        return self.status in {VERIFIED, DUMPSTER}

    def to_metadata(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "score": self.score,
            "reasons": list(self.reasons),
            "evidence": self.evidence,
        }


def _matches_any(patterns: Iterable[re.Pattern[str]], text: str) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def classify_page(
    *,
    title: str | None = None,
    description: str | None = None,
    text: str | None = None,
    tags: Sequence[str] | str | None = None,
    author: str | None = None,
    search_title: str | None = None,
    search_snippet: str | None = None,
    search_phrase: str | None = None,
    source_type: str | None = None,
) -> Classification:
    """Classify one page from its own content plus separate search metadata.

    A search phrase (or any other single search-only signal) contributes at most
    one point and can never reach the verified threshold on its own.
    """
    title_text = _clean(title, 2_000)
    description_text = _clean(description, 4_000)
    body_text = _clean(text)
    tag_text = _tag_text(tags)
    author_text = _clean(author, 500)
    page_metadata = _join((title_text, description_text, tag_text, author_text))

    direct_score = 0
    reasons: list[str] = []

    if _TIFA_LOCKHART.search(page_metadata):
        direct_score += 4
        reasons.append("page_metadata_tifa_lockhart")
    elif _TIFA_TOKEN.search(page_metadata):
        direct_score += 3
        reasons.append("page_metadata_tifa")
    elif _LOCKHART_TOKEN.search(page_metadata):
        direct_score += 1
        reasons.append("page_metadata_lockhart_only")

    if body_text:
        if _TIFA_LOCKHART.search(body_text):
            direct_score += 2
            reasons.append("visible_text_tifa_lockhart")
        elif _TIFA_TOKEN.search(body_text):
            direct_score += 1
            reasons.append("visible_text_tifa")
        elif _LOCKHART_TOKEN.search(body_text):
            direct_score += 1
            reasons.append("visible_text_lockhart_only")

    context_score = 0
    context_text = _join((page_metadata, body_text))
    if _matches_any(_FF7_TERMS, context_text):
        context_score += 1
        reasons.append("final_fantasy_vii_context")
    if _matches_any(_STRONG_CONTEXT_TERMS, context_text):
        context_score += 1
        reasons.append("tifa_source_context")

    search_text = _join((search_title, search_snippet, search_phrase))
    search_score = 0
    if _TIFA_TOKEN.search(search_text) or _LOCKHART_TOKEN.search(search_text):
        search_score = 1
        reasons.append("search_metadata_only")

    content_available = bool(page_metadata or body_text)
    score = direct_score + context_score + search_score
    evidence = {
        "direct_score": direct_score,
        "context_score": context_score,
        "search_score": search_score,
        "content_available": content_available,
        "source_type": source_type,
        "tifa_in_page_metadata": bool(_TIFA_TOKEN.search(page_metadata)),
        "tifa_in_visible_text": bool(_TIFA_TOKEN.search(body_text)),
    }

    if direct_score > 0 and score >= 4:
        return Classification(VERIFIED, score, tuple(reasons), evidence)

    if not content_available:
        return Classification(
            DUMPSTER,
            score,
            tuple(reasons) + ("missing_page_metadata",),
            evidence,
        )

    if direct_score > 0 or search_score > 0:
        return Classification(
            DUMPSTER,
            score,
            tuple(reasons) + ("insufficient_attribution",),
            evidence,
        )

    return Classification(REJECTED, score, tuple(reasons) + ("no_tifa_evidence",), evidence)
