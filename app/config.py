"""Typed, environment-backed application configuration."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_B2_REGION_PATTERN = re.compile(r"^[a-z]{2}-[a-z]+-\d{3}$")
_DEFAULT_SEARCH_PHRASES = (
    "Tifa Lockhart",
    "Tifa Lockhart fanart",
    "Tifa cosplay",
    "Tifa Lockhart cosplay",
    "Tifa Final Fantasy VII",
)


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


def _parse_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = _read(env, name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc


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


def _b2_region_is_valid(value: str) -> bool:
    return bool(_B2_REGION_PATTERN.fullmatch(value))


def _b2_endpoint_error(endpoint: str, region: str) -> str | None:
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.netloc:
        return "B2_ENDPOINT_URL must be a valid https URL"
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        return "B2_ENDPOINT_URL must not include a path, query, or fragment"

    if region and _b2_region_is_valid(region):
        expected_host = f"s3.{region}.backblazeb2.com"
        if parsed.netloc != expected_host:
            return "B2_ENDPOINT_URL must match B2_REGION using https://s3.<region>.backblazeb2.com"
    return None


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
    b2_endpoint_url: str
    b2_region: str
    b2_bucket_name: str
    b2_key_id: str
    b2_application_key: str
    reddit_client_id: str
    reddit_client_secret: str
    reddit_user_agent: str
    reddit_access_approved: bool
    brave_html_enabled: bool
    perceptual_hash_enabled: bool
    reddit_enabled: bool = True
    reddit_max_items_per_run: int = 100
    reddit_search_phrases: tuple[str, ...] = _DEFAULT_SEARCH_PHRASES
    brave_html_access_approved: bool = False
    brave_search_base_url: str = "https://search.brave.com/search"
    brave_search_interval_hours: int = 8
    brave_search_phrases: tuple[str, ...] = _DEFAULT_SEARCH_PHRASES
    brave_search_max_pages_per_phrase: int = 2
    max_page_bytes: int = 5_000_000
    max_crawl_depth: int = 6
    max_urls_per_job: int = 300
    max_pages_per_domain: int = 20
    max_concurrent_requests: int = 5
    per_domain_delay_seconds: float = 2.0
    max_redirects: int = 5
    crawl_connect_timeout_seconds: float = 10.0
    crawl_read_timeout_seconds: float = 20.0
    crawl_max_attempts: int = 3
    crawl_batch_size: int = 25

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
            b2_endpoint_url=_read(env, "B2_ENDPOINT_URL"),
            b2_region=_read(env, "B2_REGION"),
            b2_bucket_name=_read(env, "B2_BUCKET_NAME"),
            b2_key_id=_read(env, "B2_KEY_ID"),
            b2_application_key=_read(env, "B2_APPLICATION_KEY"),
            reddit_client_id=_read(env, "REDDIT_CLIENT_ID"),
            reddit_client_secret=_read(env, "REDDIT_CLIENT_SECRET"),
            reddit_user_agent=_read(env, "REDDIT_USER_AGENT"),
            reddit_access_approved=_parse_bool(env, "REDDIT_ACCESS_APPROVED"),
            reddit_enabled=_parse_bool(env, "REDDIT_ENABLED", True),
            reddit_max_items_per_run=_parse_int(env, "REDDIT_MAX_ITEMS_PER_RUN", 100),
            reddit_search_phrases=tuple(
                x.strip()
                for x in _read(
                    env,
                    "REDDIT_SEARCH_PHRASES",
                    "Tifa Lockhart|Tifa Lockhart fanart|Tifa cosplay|Tifa Lockhart cosplay|Tifa Final Fantasy VII",
                ).split("|")
                if x.strip()
            ),
            brave_html_enabled=_parse_bool(env, "BRAVE_HTML_ENABLED"),
            brave_html_access_approved=_parse_bool(env, "BRAVE_HTML_ACCESS_APPROVED"),
            brave_search_base_url=_read(
                env, "BRAVE_SEARCH_BASE_URL", "https://search.brave.com/search"
            ),
            brave_search_interval_hours=_parse_int(env, "BRAVE_SEARCH_INTERVAL_HOURS", 8),
            brave_search_phrases=tuple(
                x.strip()
                for x in _read(
                    env,
                    "BRAVE_SEARCH_PHRASES",
                    "Tifa Lockhart|Tifa Lockhart fanart|Tifa cosplay|Tifa Lockhart cosplay|Tifa Final Fantasy VII",
                ).split("|")
                if x.strip()
            ),
            brave_search_max_pages_per_phrase=_parse_int(
                env, "BRAVE_SEARCH_MAX_PAGES_PER_PHRASE", 2
            ),
            max_page_bytes=_parse_int(env, "MAX_PAGE_BYTES", 5_000_000),
            max_crawl_depth=_parse_int(env, "MAX_CRAWL_DEPTH", 6),
            max_urls_per_job=_parse_int(env, "MAX_URLS_PER_JOB", 300),
            max_pages_per_domain=_parse_int(env, "MAX_PAGES_PER_DOMAIN", 20),
            max_concurrent_requests=_parse_int(env, "MAX_CONCURRENT_REQUESTS", 5),
            per_domain_delay_seconds=_parse_float(env, "PER_DOMAIN_DELAY_SECONDS", 2.0),
            max_redirects=_parse_int(env, "MAX_REDIRECTS", 5),
            crawl_connect_timeout_seconds=_parse_float(env, "CRAWL_CONNECT_TIMEOUT_SECONDS", 10.0),
            crawl_read_timeout_seconds=_parse_float(env, "CRAWL_READ_TIMEOUT_SECONDS", 20.0),
            crawl_max_attempts=_parse_int(env, "CRAWL_MAX_ATTEMPTS", 3),
            crawl_batch_size=_parse_int(env, "CRAWL_BATCH_SIZE", 25),
            perceptual_hash_enabled=_parse_bool(env, "PERCEPTUAL_HASH_ENABLED"),
        )

    @property
    def discord_configured(self) -> bool:
        return bool(self.discord_token)

    @property
    def supabase_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_service_role_key)

    @property
    def b2_configured(self) -> bool:
        return all(
            (
                self.b2_endpoint_url,
                self.b2_region,
                self.b2_bucket_name,
                self.b2_key_id,
                self.b2_application_key,
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
                self.b2_key_id,
                self.b2_application_key,
                self.reddit_client_secret,
            )
            if value
        )

    def safe_summary(self) -> str:
        """Describe configuration state without exposing identifiers or secrets."""
        return (
            f"env={self.app_env} version={self.app_version} "
            f"discord={'configured' if self.discord_configured else 'not_configured'} "
            f"supabase={'configured' if self.supabase_configured else 'not_configured'} "
            f"b2={'configured' if self.b2_configured else 'not_configured'}"
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
        if self.reddit_max_items_per_run < 1:
            errors.append("REDDIT_MAX_ITEMS_PER_RUN must be positive")
        if not self.reddit_search_phrases:
            errors.append("REDDIT_SEARCH_PHRASES must contain at least one phrase")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            errors.append("TIMEZONE must be a valid IANA timezone")

        if not self.brave_search_phrases:
            errors.append("BRAVE_SEARCH_PHRASES must contain at least one phrase")
        if any(len(phrase) > 200 for phrase in self.brave_search_phrases):
            errors.append("BRAVE_SEARCH_PHRASES phrases must be 200 characters or fewer")
        if not 1 <= self.brave_search_interval_hours <= 168:
            errors.append("BRAVE_SEARCH_INTERVAL_HOURS must be between 1 and 168")
        if not 1 <= self.brave_search_max_pages_per_phrase <= 10:
            errors.append("BRAVE_SEARCH_MAX_PAGES_PER_PHRASE must be between 1 and 10")
        parsed_brave = urlparse(self.brave_search_base_url)
        if (
            parsed_brave.scheme != "https"
            or not parsed_brave.netloc
            or parsed_brave.username is not None
            or parsed_brave.password is not None
            or parsed_brave.fragment
        ):
            errors.append(
                "BRAVE_SEARCH_BASE_URL must be a valid https URL without credentials or a fragment"
            )
        if not 1 <= self.max_page_bytes <= 5_000_000:
            errors.append("MAX_PAGE_BYTES must be between 1 and 5000000")
        if not 1 <= self.max_crawl_depth <= 6:
            errors.append("MAX_CRAWL_DEPTH must be between 1 and 6")
        if not 1 <= self.max_urls_per_job <= 300:
            errors.append("MAX_URLS_PER_JOB must be between 1 and 300")
        if not 1 <= self.max_pages_per_domain <= 20:
            errors.append("MAX_PAGES_PER_DOMAIN must be between 1 and 20")
        if not 1 <= self.max_concurrent_requests <= 5:
            errors.append("MAX_CONCURRENT_REQUESTS must be between 1 and 5")
        if not 2.0 <= self.per_domain_delay_seconds <= 60.0:
            errors.append("PER_DOMAIN_DELAY_SECONDS must be between 2 and 60")
        if not 1 <= self.max_redirects <= 5:
            errors.append("MAX_REDIRECTS must be between 1 and 5")
        if not 0.1 <= self.crawl_connect_timeout_seconds <= 10.0:
            errors.append("CRAWL_CONNECT_TIMEOUT_SECONDS must be between 0.1 and 10")
        if not 0.1 <= self.crawl_read_timeout_seconds <= 20.0:
            errors.append("CRAWL_READ_TIMEOUT_SECONDS must be between 0.1 and 20")
        if not 1 <= self.crawl_max_attempts <= 3:
            errors.append("CRAWL_MAX_ATTEMPTS must be between 1 and 3")
        if not 1 <= self.crawl_batch_size <= self.max_urls_per_job:
            errors.append("CRAWL_BATCH_SIZE must be positive and no greater than MAX_URLS_PER_JOB")

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

        b2_fields = (
            self.b2_endpoint_url,
            self.b2_region,
            self.b2_bucket_name,
            self.b2_key_id,
            self.b2_application_key,
        )
        if any(b2_fields) and not all(b2_fields):
            errors.append(
                "B2_ENDPOINT_URL, B2_REGION, B2_BUCKET_NAME, B2_KEY_ID, and "
                "B2_APPLICATION_KEY must be configured together"
            )
        if self.b2_region and not _b2_region_is_valid(self.b2_region):
            errors.append("B2_REGION must be a Backblaze region identifier such as us-west-004")
        if self.b2_endpoint_url:
            endpoint_error = _b2_endpoint_error(self.b2_endpoint_url, self.b2_region)
            if endpoint_error:
                errors.append(endpoint_error)

        if errors:
            raise ConfigurationError("Invalid service configuration: " + "; ".join(errors))
