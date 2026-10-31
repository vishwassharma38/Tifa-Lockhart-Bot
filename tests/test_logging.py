from __future__ import annotations

import io
import logging
import sys

from app.logging_config import (
    SecretRedactingFilter,
    SecretRedactingFormatter,
    redact_secrets,
)


def test_redacts_known_secret_value() -> None:
    output = redact_secrets("failed for super-secret-token-value", ["super-secret-token-value"])
    assert "super-secret-token-value" not in output
    assert "***" in output


def test_redacts_sensitive_assignments_and_bearer_tokens() -> None:
    text = (
        "DISCORD_TOKEN=abc123 SUPABASE_SERVICE_ROLE_KEY: xyz789 "
        "Authorization: Bearer header.payload.signature"
    )
    output = redact_secrets(text)
    assert "abc123" not in output
    assert "xyz789" not in output
    assert "header.payload.signature" not in output


def test_redacts_signed_url_query_parameters() -> None:
    text = (
        "https://example.r2.cloudflarestorage.com/object?"
        "X-Amz-Credential=credential-value&X-Amz-Signature=signature-value"
    )
    output = redact_secrets(text)
    assert "credential-value" not in output
    assert "signature-value" not in output


def test_formatter_redacts_message_arguments() -> None:
    formatter = SecretRedactingFormatter(secrets=("super-secret-token-value",))
    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="request failed with %s",
        args=("super-secret-token-value",),
        exc_info=None,
    )

    output = formatter.format(record)
    assert "super-secret-token-value" not in output


def test_formatter_redacts_exception_text() -> None:
    formatter = SecretRedactingFormatter(secrets=("super-secret-token-value",))
    try:
        raise RuntimeError("token=super-secret-token-value")
    except RuntimeError:
        exc_info = sys.exc_info()

    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="unhandled failure",
        args=(),
        exc_info=exc_info,
    )

    output = formatter.format(record)
    assert "super-secret-token-value" not in output
    assert "RuntimeError" in output


def test_filter_redacts_before_other_handlers_see_the_record() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(SecretRedactingFilter(secrets=("super-secret-token-value",)))

    logger = logging.getLogger("tests.secret-redaction")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    logger.info("token used: %s", "super-secret-token-value")

    output = stream.getvalue()
    assert "super-secret-token-value" not in output
    assert "***" in output
