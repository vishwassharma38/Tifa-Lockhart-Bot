"""Explainable, conservative quality signals for extracted page links."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

_RELEVANT = re.compile(
    r"\b(?:tifa|lockhart|final fantasy|ff7|fan[ -]?art|artwork|illustrat(?:ion|or)|"
    r"gallery|portfolio|artist|creator|cosplay|commission|character art|deviantart)\b",
    re.IGNORECASE,
)
_GENERIC = re.compile(r"\b(?:home|about|contact|privacy|terms|share|like|follow)\b", re.I)
_BLOCKED_ROUTE = re.compile(
    r"(?:^|/)(?:login|logout|signin|signout|signup|register|account|checkout|"
    r"search|privacy|terms|cookies?)(?:/|$)",
    re.IGNORECASE,
)
_TRAP_QUERY_KEYS = frozenset(
    {"filter", "filters", "facets", "sort", "order", "view", "session", "sessionid"}
)


def score_link(
    url: str,
    *,
    text: str = "",
    context: str = "",
    kind: str = "anchor",
) -> tuple[int, tuple[str, ...], bool]:
    """Return an explainable link score, its signals, and whether it is a trap.

    This score describes the destination link, independent of the current
    page's media classification. The crawler adds source relevance and
    cross-domain exploration signals before admitting a candidate.
    """
    parsed = urlsplit(url)
    path = parsed.path.lower()
    segments = [segment for segment in path.split("/") if segment]
    if _BLOCKED_ROUTE.search(path):
        return -100, ("restricted_or_action_route",), True

    query_keys = {key.lower() for key, _value in parse_qsl(parsed.query, keep_blank_values=True)}
    if len(query_keys) >= 5 and query_keys & _TRAP_QUERY_KEYS:
        return -100, ("faceted_navigation_trap",), True
    if len(segments) >= 8 and len(set(segments)) <= 3:
        return -100, ("repeating_path_trap",), True
    if re.search(r"/(?:19|20)\d{2}/(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01])(?:/|$)", path):
        return -100, ("calendar_archive_trap",), True

    score = 0
    reasons: list[str] = [f"link_kind:{kind}"]
    signal_text = " ".join((text, context, path.replace("/", " ")))
    if _RELEVANT.search(signal_text):
        score += 4
        reasons.append("creator_or_art_destination")
    if re.search(r"\b(?:tifa|lockhart|final fantasy|ff7)\b", signal_text, re.I):
        score += 3
        reasons.append("character_or_franchise_signal")
    if kind in {"author", "related", "next", "tag", "sitemap"}:
        score += 1
        reasons.append(f"useful_relation:{kind}")
    if _GENERIC.search(text) and not _RELEVANT.search(signal_text):
        score -= 2
        reasons.append("generic_navigation")
    return score, tuple(reasons), False
