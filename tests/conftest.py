from __future__ import annotations

from collections.abc import Callable

import pytest

from app.config import Settings


@pytest.fixture
def make_settings() -> Callable[..., Settings]:
    """Build deterministic settings without reading the local .env file."""

    def factory(**overrides: object) -> Settings:
        env: dict[str, str] = {
            "APP_ENV": "test",
            "APP_VERSION": "1.2.3",
            "LOG_LEVEL": "DEBUG",
            "TIMEZONE": "UTC",
            "PORT": "10000",
            "DISCORD_TOKEN": "test-discord-token",
            "DISCORD_CATEGORY_CHANNELS_JSON": (
                '{"image": 999, "animation": 999, "video": 999, "dumpster": 999}'
            ),
        }
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = str(value)
        return Settings.from_env(env, load_env_file=False)

    return factory
