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
    "SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
    "B2_ENDPOINT_URL",
    "B2_REGION",
    "B2_BUCKET_NAME",
    "B2_KEY_ID",
    "B2_APPLICATION_KEY",
    "REDDIT_ACCESS_APPROVED",
    "BRAVE_HTML_ENABLED",
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
    assert values["SUPABASE_URL"] == ""
    assert values["SUPABASE_SERVICE_ROLE_KEY"] == ""
    assert values["B2_ENDPOINT_URL"] == ""
    assert values["B2_REGION"] == ""
    assert values["B2_BUCKET_NAME"] == ""
    assert values["B2_KEY_ID"] == ""
    assert values["B2_APPLICATION_KEY"] == ""
    assert values["REDDIT_ACCESS_APPROVED"] == "false"
    assert values["BRAVE_HTML_ENABLED"] == "false"
    assert values["MAX_MEDIA_BYTES"] == "10000000"


def test_template_does_not_contain_obsolete_r2_settings() -> None:
    values = parse_env_file()
    assert not {key for key in values if key.startswith("R2_")}


def test_env_is_ignored_but_template_is_tracked() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in gitignore
    assert "!.env.example" in gitignore
