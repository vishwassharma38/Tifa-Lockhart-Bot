"""Safely verify private Backblaze B2 upload, download, and cleanup."""

from __future__ import annotations

import asyncio
import sys

from app.config import ConfigurationError, Settings
from app.logging_config import configure_logging
from app.storage import StorageError, create_storage


async def _verify() -> int:
    settings = Settings.from_env()
    configure_logging(settings.log_level, settings.secret_values)
    settings.validate_service_configuration()

    storage = create_storage(settings)
    if not storage.configured:
        print(
            "Backblaze B2 is not configured. Set B2_ENDPOINT_URL, B2_REGION, "
            "B2_BUCKET_NAME, B2_KEY_ID, and B2_APPLICATION_KEY before verifying.",
            file=sys.stderr,
        )
        return 2

    try:
        result = await storage.verify_upload_download(cleanup=True)
    finally:
        await storage.close()

    print(
        "Backblaze B2 verification succeeded: "
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
    except StorageError as exc:
        print(f"Backblaze B2 verification failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as exc:
        print(f"Backblaze B2 verification failed: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
