"""Environment-backed application settings with safe V1 defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


@dataclass(frozen=True, slots=True)
class Settings:
    discord_token: str
    discord_guild_id: int | None
    port: int
    log_level: str
    reddit_access_approved: bool
    brave_html_enabled: bool

    @classmethod
    def from_env(cls) -> "Settings":
        guild_raw = os.getenv("DISCORD_GUILD_ID", "").strip()
        return cls(
            discord_token=os.getenv("DISCORD_TOKEN", "").strip(),
            discord_guild_id=int(guild_raw) if guild_raw else None,
            port=_int("PORT", 8000),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            reddit_access_approved=_bool("REDDIT_ACCESS_APPROVED"),
            brave_html_enabled=_bool("BRAVE_HTML_ENABLED"),
        )

    def validate_runtime(self) -> None:
        if not self.discord_token:
            raise ValueError("DISCORD_TOKEN is required to start the bot")
        if not 1 <= self.port <= 65535:
            raise ValueError("PORT must be between 1 and 65535")
        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL must be a standard logging level")
