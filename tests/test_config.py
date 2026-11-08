from __future__ import annotations

import pytest

from app.config import ConfigurationError, Settings


def test_defaults_do_not_require_credentials() -> None:
    settings = Settings.from_env(env={}, load_env_file=False)

    assert settings.app_env == "development"
    assert settings.app_version == "1.0.0"
    assert settings.log_level == "INFO"
    assert settings.timezone == "Asia/Kolkata"
    assert settings.port == 10000
    assert settings.discord_token == ""
    assert settings.discord_guild_id is None
    assert settings.owner_discord_id is None
    assert settings.discoveries_channel_id is None
    assert settings.saves_channel_id is None
    assert settings.supabase_configured is False
    assert settings.b2_configured is False
    assert settings.reddit_access_approved is False
    assert settings.reddit_seed_discovery_enabled is True
    assert settings.brave_html_enabled is False
    assert settings.perceptual_hash_enabled is False


def test_parses_phase_one_settings() -> None:
    settings = Settings.from_env(
        env={
            "APP_ENV": "Staging",
            "APP_VERSION": "2.0.1",
            "LOG_LEVEL": "debug",
            "TIMEZONE": "UTC",
            "PORT": "8080",
            "DISCORD_TOKEN": "token-value",
            "DISCORD_GUILD_ID": "123",
            "OWNER_DISCORD_ID": "456",
            "DISCORD_DISCOVERIES_CHANNEL_ID": "789",
            "DISCORD_SAVES_CHANNEL_ID": "1011",
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "service-key",
            "B2_ENDPOINT_URL": "https://s3.us-west-004.backblazeb2.com",
            "B2_REGION": "us-west-004",
            "B2_BUCKET_NAME": "private-bucket",
            "B2_KEY_ID": "key-id",
            "B2_APPLICATION_KEY": "application-key",
            "DISCORD_CATEGORY_CHANNELS_JSON": (
                '{"image": 1, "animation": 2, "video": 3, "dumpster": 4}'
            ),
            "REDDIT_ACCESS_APPROVED": "true",
            "REDDIT_SEED_DISCOVERY_ENABLED": "false",
            "BRAVE_HTML_ENABLED": "false",
            "PERCEPTUAL_HASH_ENABLED": "true",
        },
        load_env_file=False,
    )

    assert settings.app_env == "staging"
    assert settings.app_version == "2.0.1"
    assert settings.log_level == "DEBUG"
    assert settings.port == 8080
    assert settings.discord_token == "token-value"
    assert settings.discord_guild_id == 123
    assert settings.owner_discord_id == 456
    assert settings.discoveries_channel_id == 789
    assert settings.saves_channel_id == 1011
    assert settings.supabase_configured is True
    assert settings.b2_configured is True
    assert settings.reddit_access_approved is True
    assert settings.reddit_seed_discovery_enabled is False
    assert settings.brave_html_enabled is False
    assert settings.perceptual_hash_enabled is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("false", False),
        ("FALSE", False),
        ("0", False),
        ("no", False),
        ("off", False),
    ],
)
def test_boolean_parsing(raw: str, expected: bool) -> None:
    settings = Settings.from_env(
        env={"REDDIT_ACCESS_APPROVED": raw},
        load_env_file=False,
    )
    assert settings.reddit_access_approved is expected


def test_invalid_boolean_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="BRAVE_HTML_ENABLED"):
        Settings.from_env(
            env={"BRAVE_HTML_ENABLED": "sometimes"},
            load_env_file=False,
        )


def test_invalid_integer_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="PORT"):
        Settings.from_env(env={"PORT": "eighty"}, load_env_file=False)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("PORT", "boot"),
        ("DISCORD_GUILD_ID", "not-an-id"),
        ("OWNER_DISCORD_ID", "12.5"),
    ],
)
def test_invalid_integer_settings_are_rejected(key: str, value: str) -> None:
    with pytest.raises(ConfigurationError, match=key):
        Settings.from_env(env={key: value}, load_env_file=False)


def test_runtime_requires_discord_token() -> None:
    settings = Settings.from_env(env={}, load_env_file=False)
    with pytest.raises(ConfigurationError, match="DISCORD_TOKEN"):
        settings.validate_runtime()


def test_runtime_validation_reports_all_core_errors_without_secrets() -> None:
    settings = Settings.from_env(
        env={
            "DISCORD_TOKEN": "super-secret-token-value",
            "APP_ENV": "",
            "APP_VERSION": "",
            "LOG_LEVEL": "LOUD",
            "TIMEZONE": "Not/A_Timezone",
            "PORT": "70000",
            "DISCORD_GUILD_ID": "0",
        },
        load_env_file=False,
    )

    with pytest.raises(ConfigurationError) as exc_info:
        settings.validate_runtime()

    message = str(exc_info.value)
    assert "APP_ENV" in message
    assert "APP_VERSION" in message
    assert "LOG_LEVEL" in message
    assert "TIMEZONE" in message
    assert "PORT" in message
    assert "DISCORD_GUILD_ID" in message
    assert "super-secret-token-value" not in message


def test_valid_runtime_settings_pass() -> None:
    settings = Settings.from_env(
        env={
            "DISCORD_TOKEN": "token",
            "TIMEZONE": "Asia/Kolkata",
            "PORT": "10000",
        },
        load_env_file=False,
    )
    settings.validate_runtime()


def test_completely_absent_optional_services_are_allowed() -> None:
    settings = Settings.from_env(env={}, load_env_file=False)
    settings.validate_service_configuration()


def test_partial_supabase_configuration_is_rejected() -> None:
    settings = Settings.from_env(
        env={"SUPABASE_URL": "https://example.supabase.co"},
        load_env_file=False,
    )
    with pytest.raises(ConfigurationError, match="SUPABASE_SERVICE_ROLE_KEY"):
        settings.validate_service_configuration()


def test_partial_b2_configuration_is_rejected() -> None:
    settings = Settings.from_env(
        env={"B2_BUCKET_NAME": "bucket"},
        load_env_file=False,
    )
    with pytest.raises(ConfigurationError, match="B2_ENDPOINT_URL"):
        settings.validate_service_configuration()


def test_invalid_service_urls_are_rejected_without_echoing_values() -> None:
    settings = Settings.from_env(
        env={
            "SUPABASE_URL": "not-a-url",
            "SUPABASE_SERVICE_ROLE_KEY": "service-secret",
            "B2_ENDPOINT_URL": "also-not-a-url",
            "B2_REGION": "us-west-004",
            "B2_BUCKET_NAME": "bucket",
            "B2_KEY_ID": "key-id",
            "B2_APPLICATION_KEY": "b2-secret",
        },
        load_env_file=False,
    )

    with pytest.raises(ConfigurationError) as exc_info:
        settings.validate_service_configuration()

    message = str(exc_info.value)
    assert "SUPABASE_URL" in message
    assert "B2_ENDPOINT_URL" in message
    assert "service-secret" not in message
    assert "b2-secret" not in message


def test_invalid_b2_region_is_rejected() -> None:
    settings = Settings.from_env(
        env={
            "B2_ENDPOINT_URL": "https://s3.us-west-004.backblazeb2.com",
            "B2_REGION": "auto",
            "B2_BUCKET_NAME": "bucket",
            "B2_KEY_ID": "key-id",
            "B2_APPLICATION_KEY": "application-key",
        },
        load_env_file=False,
    )

    with pytest.raises(ConfigurationError, match="B2_REGION"):
        settings.validate_service_configuration()


def test_b2_endpoint_must_match_region() -> None:
    settings = Settings.from_env(
        env={
            "B2_ENDPOINT_URL": "https://s3.us-west-002.backblazeb2.com",
            "B2_REGION": "us-west-004",
            "B2_BUCKET_NAME": "bucket",
            "B2_KEY_ID": "key-id",
            "B2_APPLICATION_KEY": "application-key",
        },
        load_env_file=False,
    )

    with pytest.raises(ConfigurationError, match="B2_ENDPOINT_URL"):
        settings.validate_service_configuration()


def test_eu_central_b2_configuration_is_accepted() -> None:
    settings = Settings.from_env(
        env={
            "B2_ENDPOINT_URL": "https://s3.eu-central-003.backblazeb2.com",
            "B2_REGION": "eu-central-003",
            "B2_BUCKET_NAME": "bucket",
            "B2_KEY_ID": "key-id",
            "B2_APPLICATION_KEY": "application-key",
        },
        load_env_file=False,
    )

    settings.validate_service_configuration()

    assert settings.b2_configured is True
    assert settings.b2_region == "eu-central-003"


def test_eu_central_endpoint_must_match_region() -> None:
    settings = Settings.from_env(
        env={
            "B2_ENDPOINT_URL": "https://s3.us-west-004.backblazeb2.com",
            "B2_REGION": "eu-central-003",
            "B2_BUCKET_NAME": "bucket",
            "B2_KEY_ID": "key-id",
            "B2_APPLICATION_KEY": "application-key",
        },
        load_env_file=False,
    )

    with pytest.raises(ConfigurationError, match="B2_ENDPOINT_URL"):
        settings.validate_service_configuration()


def test_b2_endpoint_with_unexpected_host_is_rejected() -> None:
    settings = Settings.from_env(
        env={
            "B2_ENDPOINT_URL": "https://s3.eu-central-003.example.com",
            "B2_REGION": "eu-central-003",
            "B2_BUCKET_NAME": "bucket",
            "B2_KEY_ID": "key-id",
            "B2_APPLICATION_KEY": "application-key",
        },
        load_env_file=False,
    )

    with pytest.raises(ConfigurationError, match="B2_ENDPOINT_URL"):
        settings.validate_service_configuration()


@pytest.mark.parametrize(
    "region",
    ["auto", "", "eu-central-3", "EU-CENTRAL-003", "us-east-1"],
)
def test_unsupported_or_empty_b2_region_is_rejected(region: str) -> None:
    settings = Settings.from_env(
        env={
            "B2_ENDPOINT_URL": "https://s3.eu-central-003.backblazeb2.com",
            "B2_REGION": region,
            "B2_BUCKET_NAME": "bucket",
            "B2_KEY_ID": "key-id",
            "B2_APPLICATION_KEY": "application-key",
        },
        load_env_file=False,
    )

    with pytest.raises(ConfigurationError, match="B2_REGION"):
        settings.validate_service_configuration()


def test_b2_validation_errors_do_not_expose_credentials() -> None:
    settings = Settings.from_env(
        env={
            "B2_ENDPOINT_URL": "https://s3.eu-central-003.example.com",
            "B2_REGION": "eu-central-003",
            "B2_BUCKET_NAME": "bucket",
            "B2_KEY_ID": "b2-key-id-secret",
            "B2_APPLICATION_KEY": "b2-application-key-secret",
        },
        load_env_file=False,
    )

    with pytest.raises(ConfigurationError) as exc_info:
        settings.validate_service_configuration()

    message = str(exc_info.value)
    assert "B2_ENDPOINT_URL" in message
    assert "b2-key-id-secret" not in message
    assert "b2-application-key-secret" not in message


def test_b2_endpoint_requires_https() -> None:
    settings = Settings.from_env(
        env={
            "B2_ENDPOINT_URL": "http://s3.us-west-004.backblazeb2.com",
            "B2_REGION": "us-west-004",
            "B2_BUCKET_NAME": "bucket",
            "B2_KEY_ID": "key-id",
            "B2_APPLICATION_KEY": "application-key",
        },
        load_env_file=False,
    )

    with pytest.raises(ConfigurationError, match="https"):
        settings.validate_service_configuration()


def test_legacy_r2_environment_variables_do_not_configure_b2() -> None:
    settings = Settings.from_env(
        env={
            "R2_ENDPOINT_URL": "https://example.r2.cloudflarestorage.com",
            "R2_ACCESS_KEY_ID": "access-key",
            "R2_SECRET_ACCESS_KEY": "secret-key",
            "R2_BUCKET_NAME": "bucket",
            "R2_REGION": "auto",
        },
        load_env_file=False,
    )

    assert settings.b2_configured is False
    settings.validate_service_configuration()


def test_complete_service_configuration_passes() -> None:
    settings = Settings.from_env(
        env={
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "service-key",
            "B2_ENDPOINT_URL": "https://s3.us-west-004.backblazeb2.com",
            "B2_REGION": "us-west-004",
            "B2_BUCKET_NAME": "bucket",
            "B2_KEY_ID": "key-id",
            "B2_APPLICATION_KEY": "application-key",
            "DISCORD_CATEGORY_CHANNELS_JSON": (
                '{"image": 1, "animation": 2, "video": 3, "dumpster": 4}'
            ),
        },
        load_env_file=False,
    )
    settings.validate_service_configuration()


def test_digest_requires_four_distinct_channels() -> None:
    settings = Settings.from_env(
        env={
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "service-key",
            "B2_ENDPOINT_URL": "https://s3.us-west-004.backblazeb2.com",
            "B2_REGION": "us-west-004",
            "B2_BUCKET_NAME": "bucket",
            "B2_KEY_ID": "key-id",
            "B2_APPLICATION_KEY": "application-key",
            "DISCORD_CATEGORY_CHANNELS_JSON": (
                '{"image": 1, "animation": 1, "video": 3, "dumpster": 4}'
            ),
        },
        load_env_file=False,
    )
    with pytest.raises(ConfigurationError, match="four distinct channel IDs"):
        settings.validate_service_configuration()


def test_secret_values_and_safe_summary(make_settings) -> None:
    settings = make_settings(
        DISCORD_TOKEN="discord-secret-123",
        SUPABASE_URL="https://example.supabase.co",
        SUPABASE_SERVICE_ROLE_KEY="service-secret-123",
        B2_ENDPOINT_URL="https://s3.us-west-004.backblazeb2.com",
        B2_REGION="us-west-004",
        B2_BUCKET_NAME="bucket",
        B2_KEY_ID="key-id-secret-123",
        B2_APPLICATION_KEY="b2-secret-123",
    )

    secrets = settings.secret_values
    assert "discord-secret-123" in secrets
    assert "service-secret-123" in secrets
    assert "key-id-secret-123" in secrets
    assert "b2-secret-123" in secrets
    assert all(secrets)

    summary = settings.safe_summary()
    assert "discord-secret-123" not in summary
    assert "service-secret-123" not in summary
    assert "key-id-secret-123" not in summary
    assert "b2-secret-123" not in summary
    assert "discord=configured" in summary
    assert "supabase=configured" in summary
    assert "b2=configured" in summary


def test_category_channels_default_to_empty_mapping() -> None:
    settings = Settings.from_env(env={}, load_env_file=False)

    assert settings.category_channels == {}


def test_empty_category_channel_object_is_accepted() -> None:
    settings = Settings.from_env(
        env={"DISCORD_CATEGORY_CHANNELS_JSON": "{}"},
        load_env_file=False,
    )

    assert settings.category_channels == {}


def test_category_channel_mapping_is_parsed() -> None:
    settings = Settings.from_env(
        env={
            "DISCORD_CATEGORY_CHANNELS_JSON": (
                '{"fanart": 123456789012345678, "cosplay_photography": 123456789012345679, '
                '"official_artwork": 1, "wallpaper": 2, "video": 3, "other": 4}'
            )
        },
        load_env_file=False,
    )

    assert settings.category_channels == {
        "fanart": 123456789012345678,
        "cosplay_photography": 123456789012345679,
        "official_artwork": 1,
        "wallpaper": 2,
        "video": 3,
        "other": 4,
    }


@pytest.mark.parametrize(
    ("raw", "expected_message"),
    [
        ('{"fanart": ', "valid JSON"),
        ('["fanart"]', "JSON object"),
        ('{"portrait": 123}', "unsupported media type"),
        ('{"fanart": "123"}', "integer channel IDs"),
        ('{"fanart": 12.5}', "integer channel IDs"),
        ('{"fanart": true}', "integer channel IDs"),
        ('{"fanart": 0}', "positive channel IDs"),
        ('{"fanart": -5}', "positive channel IDs"),
    ],
)
def test_invalid_category_channel_configuration_is_rejected(
    raw: str, expected_message: str
) -> None:
    with pytest.raises(ConfigurationError, match=expected_message) as exc_info:
        Settings.from_env(
            env={"DISCORD_CATEGORY_CHANNELS_JSON": raw},
            load_env_file=False,
        )

    assert "DISCORD_CATEGORY_CHANNELS_JSON" in str(exc_info.value)
