from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_KEYS = {
    "APP_ENV",
    "APP_VERSION",
    "LOG_LEVEL",
    "TIMEZONE",
    "PORT",
    "DISCORD_TOKEN",
    "DISCORD_GUILD_ID",
    "OWNER_DISCORD_ID",
    "DISCORD_DISCOVERIES_CHANNEL_ID",
    "DISCORD_SAVES_CHANNEL_ID",
    "DISCORD_CATEGORY_CHANNELS_JSON",
    "SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
    "B2_ENDPOINT_URL",
    "B2_REGION",
    "B2_BUCKET_NAME",
    "B2_KEY_ID",
    "B2_APPLICATION_KEY",
    "REDDIT_ACCESS_APPROVED",
    "REDDIT_SEED_DISCOVERY_ENABLED",
    "BRAVE_HTML_ENABLED",
    "BRAVE_HTML_ACCESS_APPROVED",
    "BRAVE_SEARCH_BASE_URL",
    "BRAVE_SEARCH_INTERVAL_HOURS",
    "BRAVE_SEARCH_PHRASES",
    "BRAVE_SEARCH_MAX_PAGES_PER_PHRASE",
    "MAX_PAGE_BYTES",
    "MAX_CRAWL_DEPTH",
    "MAX_URLS_PER_JOB",
    "MAX_PAGES_PER_DOMAIN",
    "MAX_CONCURRENT_REQUESTS",
    "PER_DOMAIN_DELAY_SECONDS",
    "MAX_REDIRECTS",
    "CRAWL_CONNECT_TIMEOUT_SECONDS",
    "CRAWL_READ_TIMEOUT_SECONDS",
    "CRAWL_MAX_ATTEMPTS",
    "CRAWL_BATCH_SIZE",
    "MAX_MEDIA_BYTES",
}


def parse_env_file() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition("=")
        assert separator == "=", stripped
        values[key.strip()] = value.strip()
    return values


def test_template_contains_all_required_settings() -> None:
    values = parse_env_file()
    assert REQUIRED_KEYS <= values.keys()


def test_template_contains_only_placeholders_and_safe_defaults() -> None:
    values = parse_env_file()

    assert values["DISCORD_TOKEN"] == ""
    assert values["DISCORD_GUILD_ID"] == ""
    assert values["OWNER_DISCORD_ID"] == ""
    assert values["DISCORD_CATEGORY_CHANNELS_JSON"] == "{}"
    assert values["SUPABASE_URL"] == ""
    assert values["SUPABASE_SERVICE_ROLE_KEY"] == ""
    assert values["B2_ENDPOINT_URL"] == ""
    assert values["B2_REGION"] == ""
    assert values["B2_BUCKET_NAME"] == ""
    assert values["B2_KEY_ID"] == ""
    assert values["B2_APPLICATION_KEY"] == ""
    assert values["REDDIT_ACCESS_APPROVED"] == "false"
    assert values["REDDIT_SEED_DISCOVERY_ENABLED"] == "true"
    assert values["BRAVE_HTML_ENABLED"] == "false"
    assert values["BRAVE_HTML_ACCESS_APPROVED"] == "false"
    assert values["BRAVE_SEARCH_INTERVAL_HOURS"] == "8"
    assert values["MAX_PAGE_BYTES"] == "5000000"
    assert values["MAX_CRAWL_DEPTH"] == "6"
    assert values["MAX_URLS_PER_JOB"] == "300"
    assert values["MAX_PAGES_PER_DOMAIN"] == "20"
    assert values["MAX_CONCURRENT_REQUESTS"] == "5"
    assert values["PER_DOMAIN_DELAY_SECONDS"] == "2"
    assert values["MAX_REDIRECTS"] == "5"
    assert values["MAX_MEDIA_BYTES"] == "10000000"


def test_template_does_not_contain_obsolete_r2_settings() -> None:
    values = parse_env_file()
    assert not {key for key in values if key.startswith("R2_")}


def test_env_is_ignored_but_template_is_tracked() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in gitignore
    assert "!.env.example" in gitignore
