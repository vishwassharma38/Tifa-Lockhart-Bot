"""Safely verify private R2 upload, download, and cleanup."""

from __future__ import annotations

import asyncio
import sys

from app.config import ConfigurationError, Settings
from app.logging_config import configure_logging
from app.storage.r2 import R2Storage


async def _verify() -> int:
    settings = Settings.from_env()
    configure_logging(settings.log_level, settings.secret_values)
    settings.validate_service_configuration()

    storage = R2Storage.from_settings(settings)
    if not storage.configured:
        print(
            "R2 is not configured. Set R2_ENDPOINT_URL, R2_ACCESS_KEY_ID, "
            "R2_SECRET_ACCESS_KEY, and R2_BUCKET_NAME before verifying.",
            file=sys.stderr,
        )
        return 2

    try:
        result = await storage.verify_upload_download(cleanup=True)
    finally:
        await storage.close()

    print(
        "R2 verification succeeded: "
        f"key={result.object_key} size_bytes={result.size_bytes} "
        f"cleaned_up={result.cleaned_up}"
    )
    return 0


def main() -> None:
    try:
        exit_code = asyncio.run(_verify())
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    except Exception as exc:
        print(f"R2 verification failed: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
