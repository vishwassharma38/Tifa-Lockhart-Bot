"""Typed, environment-backed application configuration."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


class ConfigurationError(ValueError):
    """Raised when configuration is missing or invalid.

    Messages identify environment variable names only. They must never contain
    configured secret values.
    """


def _read(env: Mapping[str, str], name: str, default: str = "") -> str:
    value = env.get(name)
    if value is None:
        return default
    return value.strip()


def _parse_bool(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = _read(env, name)
    if not raw:
        return default
    normalized = raw.lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ConfigurationError(f"{name} must be a boolean value such as true or false")


def _parse_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = _read(env, name)
    if not raw:
        return default
    try:
        return int(raw, 10)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc


def _parse_optional_int(env: Mapping[str, str], name: str) -> int | None:
    raw = _read(env, name)
    if not raw:
        return None
    try:
        return int(raw, 10)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc


def _url_is_valid(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated application settings read from environment variables."""

    app_env: str
    app_version: str
    log_level: str
    timezone: str
    port: int
    discord_token: str
    discord_guild_id: int | None
    owner_discord_id: int | None
    discoveries_channel_id: int | None
    saves_channel_id: int | None
    supabase_url: str
    supabase_service_role_key: str
    r2_endpoint_url: str
    r2_access_key_id: str
    r2_secret_access_key: str
    r2_bucket_name: str
    r2_region: str
    reddit_access_approved: bool
    brave_html_enabled: bool

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        load_env_file: bool = True,
    ) -> Settings:
        """Build settings without requiring credentials.

        Passing ``env`` avoids loading the local ``.env`` file, which keeps
        tests deterministic. Secret validation happens in ``validate_runtime``
        and ``validate_service_configuration``.
        """
        if env is None:
            if load_env_file:
                load_dotenv(override=False)
            env = os.environ

        return cls(
            app_env=_read(env, "APP_ENV", "development").lower(),
            app_version=_read(env, "APP_VERSION", "1.0.0"),
            log_level=_read(env, "LOG_LEVEL", "INFO").upper(),
            timezone=_read(env, "TIMEZONE", "Asia/Kolkata"),
            port=_parse_int(env, "PORT", 10000),
            discord_token=_read(env, "DISCORD_TOKEN"),
            discord_guild_id=_parse_optional_int(env, "DISCORD_GUILD_ID"),
            owner_discord_id=_parse_optional_int(env, "OWNER_DISCORD_ID"),
            discoveries_channel_id=_parse_optional_int(env, "DISCORD_DISCOVERIES_CHANNEL_ID"),
            saves_channel_id=_parse_optional_int(env, "DISCORD_SAVES_CHANNEL_ID"),
            supabase_url=_read(env, "SUPABASE_URL"),
            supabase_service_role_key=_read(env, "SUPABASE_SERVICE_ROLE_KEY"),
            r2_endpoint_url=_read(env, "R2_ENDPOINT_URL"),
            r2_access_key_id=_read(env, "R2_ACCESS_KEY_ID"),
            r2_secret_access_key=_read(env, "R2_SECRET_ACCESS_KEY"),
            r2_bucket_name=_read(env, "R2_BUCKET_NAME"),
            r2_region=_read(env, "R2_REGION", "auto") or "auto",
            reddit_access_approved=_parse_bool(env, "REDDIT_ACCESS_APPROVED"),
            brave_html_enabled=_parse_bool(env, "BRAVE_HTML_ENABLED"),
        )

    @property
    def discord_configured(self) -> bool:
        return bool(self.discord_token)

    @property
    def supabase_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_service_role_key)

    @property
    def r2_configured(self) -> bool:
        return all(
            (
                self.r2_endpoint_url,
                self.r2_access_key_id,
                self.r2_secret_access_key,
                self.r2_bucket_name,
            )
        )

    @property
    def secret_values(self) -> tuple[str, ...]:
        """Values that must never appear in log output."""
        return tuple(
            value
            for value in (
                self.discord_token,
                self.supabase_service_role_key,
                self.r2_access_key_id,
                self.r2_secret_access_key,
            )
            if value
        )

    def safe_summary(self) -> str:
        """Describe configuration state without exposing identifiers or secrets."""
        return (
            f"env={self.app_env} version={self.app_version} "
            f"discord={'configured' if self.discord_configured else 'not_configured'} "
            f"supabase={'configured' if self.supabase_configured else 'not_configured'} "
            f"r2={'configured' if self.r2_configured else 'not_configured'}"
        )

    def validate_runtime(self) -> None:
        """Validate settings required to start the application process."""
        errors: list[str] = []
        if not self.discord_token:
            errors.append("DISCORD_TOKEN is required to connect to Discord")
        if not self.app_env:
            errors.append("APP_ENV must not be empty")
        if not self.app_version:
            errors.append("APP_VERSION must not be empty")
        if self.log_level not in VALID_LOG_LEVELS:
            errors.append("LOG_LEVEL must be a standard logging level")
        if not 1 <= self.port <= 65535:
            errors.append("PORT must be between 1 and 65535")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            errors.append("TIMEZONE must be a valid IANA timezone")

        for name, value in (
            ("DISCORD_GUILD_ID", self.discord_guild_id),
            ("OWNER_DISCORD_ID", self.owner_discord_id),
            ("DISCORD_DISCOVERIES_CHANNEL_ID", self.discoveries_channel_id),
            ("DISCORD_SAVES_CHANNEL_ID", self.saves_channel_id),
        ):
            if value is not None and value <= 0:
                errors.append(f"{name} must be a positive integer")

        if errors:
            raise ConfigurationError("Invalid configuration: " + "; ".join(errors))

    def validate_service_configuration(self) -> None:
        """Validate optional service settings before initializing clients.

        A completely absent optional service is valid and results in a
        documented degraded mode. A partially configured service is treated as
        a configuration error rather than silently ignored.
        """
        errors: list[str] = []

        supabase_fields = (self.supabase_url, self.supabase_service_role_key)
        if any(supabase_fields) and not all(supabase_fields):
            errors.append("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be configured together")
        if self.supabase_url and not _url_is_valid(self.supabase_url):
            errors.append("SUPABASE_URL must be a valid http(s) URL")

        r2_fields = (
            self.r2_endpoint_url,
            self.r2_access_key_id,
            self.r2_secret_access_key,
            self.r2_bucket_name,
        )
        if any(r2_fields) and not all(r2_fields):
            errors.append(
                "R2_ENDPOINT_URL, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, and "
                "R2_BUCKET_NAME must be configured together"
            )
        if self.r2_endpoint_url and not _url_is_valid(self.r2_endpoint_url):
            errors.append("R2_ENDPOINT_URL must be a valid http(s) URL")
        if not self.r2_region:
            errors.append("R2_REGION must not be empty")

        if errors:
            raise ConfigurationError("Invalid service configuration: " + "; ".join(errors))
