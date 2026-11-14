"""Run one controlled Phase 2 Reddit-to-Discord acquisition.

This is a development entry point, not a permanent slash command. It requires
authorized Reddit credentials, a configured discoveries channel, Supabase, and
B2. It processes exactly one submission and exits.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.bot.publisher import DiscordPublisher
from app.config import ConfigurationError, Settings
from app.db.client import SupabaseDatabase
from app.db.repository import MediaRepository
from app.logging_config import configure_logging
from app.main import Application
from app.media.fetcher import MediaFetcher
from app.pipeline.phase2 import Phase2Pipeline, Phase2Status
from app.reddit.connector import RedditConnector
from app.storage import create_storage


def _validate_prerequisites(settings: Settings) -> str | None:
    if not settings.reddit_access_approved:
        return "REDDIT_ACCESS_APPROVED is false; authorized Reddit access is required."
    missing = [
        name
        for name, value in (
            ("REDDIT_CLIENT_ID", settings.reddit_client_id),
            ("REDDIT_CLIENT_SECRET", settings.reddit_client_secret),
            ("REDDIT_USER_AGENT", settings.reddit_user_agent),
        )
        if not value
    ]
    if missing:
        return "Reddit access is approved but credentials are missing: " + ", ".join(missing)
    if not settings.discoveries_channel_id:
        return "DISCORD_DISCOVERIES_CHANNEL_ID must be configured."
    if not settings.supabase_configured:
        return "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be configured."
    if not settings.b2_configured:
        return "Backblaze B2 settings must be configured."
    return None


async def _run(submission: str) -> int:
    settings = Settings.from_env()
    configure_logging(settings.log_level, settings.secret_values)
    settings.validate_runtime()
    settings.validate_service_configuration()

    prerequisite_error = _validate_prerequisites(settings)
    if prerequisite_error is not None:
        print(prerequisite_error, file=sys.stderr)
        return 2

    database = SupabaseDatabase.from_settings(settings)
    storage = create_storage(settings)
    app = Application(
        settings,
        database=database,
        storage=storage,
        install_signal_handlers=False,
    )

    try:
        await app.start()
        bot = app.bot
        if bot is None:
            print("Discord client did not start.", file=sys.stderr)
            return 1
        await asyncio.wait_for(bot.wait_until_ready(), timeout=60)

        async with MediaFetcher() as fetcher:
            pipeline = Phase2Pipeline(
                settings=settings,
                connector=RedditConnector(settings),
                fetcher=fetcher,
                repository=MediaRepository(
                    database,
                    discovery_batch_size=settings.discovery_batch_size,
                    discovery_edge_batch_size=settings.discovery_edge_batch_size,
                    runtime_state_cache_ttl_seconds=settings.runtime_state_cache_ttl_seconds,
                ),
                storage=storage,
                publisher=DiscordPublisher(bot, settings.discoveries_channel_id),
            )
            result = await pipeline.run(submission)

        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return 0 if result.status in {Phase2Status.PUBLISHED, Phase2Status.DUPLICATE} else 1
    finally:
        await app.stop()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Process one controlled Reddit submission through Phase 2."
    )
    parser.add_argument(
        "--submission",
        required=True,
        help="Reddit submission ID or full permalink.",
    )
    args = parser.parse_args()

    try:
        exit_code = asyncio.run(_run(args.submission))
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as exc:
        print(f"Phase 2 run failed: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
