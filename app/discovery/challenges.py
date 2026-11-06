"""Conservative detection of CAPTCHA and anti-automation barriers."""

from __future__ import annotations

import re

_STRONG_MARKERS = (
    "cf-challenge",
    "challenge-platform",
    "challenge-form",
    "captcha-delivery",
    "g-recaptcha",
    "h-captcha",
    "hcaptcha",
    "px-captcha",
    "proof-of-work",
    "proof of work",
    "turnstile",
    "ddos-guard",
)
_WEAK_MARKERS = (
    "captcha",
    "verify you are human",
    "verify you are a human",
    "checking your browser",
    "checking if the site connection is secure",
    "just a moment",
    "attention required",
    "access denied",
    "request blocked",
    "unusual traffic",
    "automated queries",
    "bot detection",
    "security check",
    "enable javascript and cookies",
)
_TAG_PATTERN = re.compile(r"<[^>]+>")


def _visible_prefix(html: str, limit: int = 20_000) -> str:
    return _TAG_PATTERN.sub(" ", html[:limit]).lower()


def detect_challenge(html: str | None) -> str | None:
    """Return a short challenge category when the response is a barrier page."""
    if not html:
        return None
    raw = html[:20_000].lower()
    prefix = _visible_prefix(html)
    for marker in _STRONG_MARKERS:
        # Strong markers also appear in script URLs, iframe sources and widget
        # classes, which tag stripping intentionally removes from visible text.
        if marker in raw or marker in prefix:
            return "anti_automation_challenge"
    matches = sum(1 for marker in _WEAK_MARKERS if marker in prefix)
    if matches >= 2:
        return "anti_automation_challenge"
    return None
