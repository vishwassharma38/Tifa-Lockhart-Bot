"""Secret-conscious logging setup."""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterable

from app.config import VALID_LOG_LEVELS

LOGGER_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b([a-z0-9_]*(?:"
    r"token|secret|password|api[_-]?key|service[_-]?role[_-]?key|"
    r"access[_-]?key(?:[_-]?id)?|signature|credential|authorization"
    r")[a-z0-9_]*)(\s*[:=]\s*)([^\s&,;\"']+)"
)
_BEARER_TOKEN = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+")
_SIGNED_QUERY_PARAMETER = re.compile(
    r"(?i)([?&](?:x-amz-signature|x-amz-credential|x-amz-security-token|"
    r"signature|token|key)=)[^&\s]+"
)


def redact_secrets(text: str, secrets: Iterable[str] = ()) -> str:
    """Remove known secret values and common credential patterns from text."""
    if not text:
        return text

    redacted = text
    for secret in sorted(
        {value for value in secrets if value and len(value) >= 4}, key=len, reverse=True
    ):
        redacted = redacted.replace(secret, "***")

    redacted = _BEARER_TOKEN.sub(r"\1 ***", redacted)
    redacted = _SENSITIVE_ASSIGNMENT.sub(r"\1\2***", redacted)
    redacted = _SIGNED_QUERY_PARAMETER.sub(r"\1***", redacted)
    return redacted


class SecretRedactingFilter(logging.Filter):
    """Redact secrets from record messages before any handler sees them."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = tuple(secrets)

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = redact_secrets(message, self._secrets)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


class SecretRedactingFormatter(logging.Formatter):
    """Redact secrets from the fully formatted record, including tracebacks."""

    def __init__(self, fmt: str = LOGGER_FORMAT, secrets: Iterable[str] = ()) -> None:
        super().__init__(fmt)
        self._secrets = tuple(secrets)

    def format(self, record: logging.LogRecord) -> str:
        return redact_secrets(super().format(record), self._secrets)


def configure_logging(level: str = "INFO", secrets: Iterable[str] = ()) -> None:
    """Configure stdout logging once while preserving other handlers."""
    resolved_level = level.upper() if level else "INFO"
    if resolved_level not in VALID_LOG_LEVELS:
        resolved_level = "INFO"

    root = logging.getLogger()
    root.setLevel(resolved_level)

    for handler in list(root.handlers):
        if getattr(handler, "_tifa_handler", False):
            root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(resolved_level)
    handler.setFormatter(SecretRedactingFormatter(secrets=secrets))
    handler.addFilter(SecretRedactingFilter(secrets=secrets))
    handler._tifa_handler = True  # type: ignore[attr-defined]
    root.addHandler(handler)

    # Keep noisy HTTP internals from logging request details at normal levels.
    for noisy_logger in (
        "aiohttp.access",
        "botocore",
        "discord.http",
        "httpcore",
        "httpx",
        "supabase",
        "urllib3",
    ):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)
