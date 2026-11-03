from __future__ import annotations

from scripts.phase2_run import _validate_prerequisites

READY_ENV = {
    "REDDIT_ACCESS_APPROVED": "true",
    "REDDIT_CLIENT_ID": "client-id",
    "REDDIT_CLIENT_SECRET": "client-secret",
    "REDDIT_USER_AGENT": "test-user-agent",
    "DISCORD_DISCOVERIES_CHANNEL_ID": "123",
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_SERVICE_ROLE_KEY": "service-role-key",
    "B2_ENDPOINT_URL": "https://s3.us-west-004.backblazeb2.com",
    "B2_REGION": "us-west-004",
    "B2_BUCKET_NAME": "private-bucket",
    "B2_KEY_ID": "key-id",
    "B2_APPLICATION_KEY": "application-key",
}


def test_phase2_run_refuses_without_reddit_approval(make_settings) -> None:
    error = _validate_prerequisites(make_settings())

    assert error is not None
    assert "REDDIT_ACCESS_APPROVED" in error


def test_phase2_run_refuses_without_reddit_credentials(make_settings) -> None:
    settings = make_settings(REDDIT_ACCESS_APPROVED="true")

    error = _validate_prerequisites(settings)

    assert error is not None
    assert "REDDIT_CLIENT_ID" in error
    assert "REDDIT_CLIENT_SECRET" in error
    assert "REDDIT_USER_AGENT" in error


def test_phase2_run_requires_destination_and_services(make_settings) -> None:
    missing_destination = _validate_prerequisites(
        make_settings(**{**READY_ENV, "DISCORD_DISCOVERIES_CHANNEL_ID": None})
    )
    assert missing_destination is not None
    assert "DISCORD_DISCOVERIES_CHANNEL_ID" in missing_destination

    missing_supabase = _validate_prerequisites(make_settings(**{**READY_ENV, "SUPABASE_URL": None}))
    assert missing_supabase is not None
    assert "SUPABASE_URL" in missing_supabase

    missing_b2 = _validate_prerequisites(make_settings(**{**READY_ENV, "B2_BUCKET_NAME": None}))
    assert missing_b2 is not None
    assert "B2" in missing_b2


def test_phase2_run_prerequisites_pass_when_fully_configured(make_settings) -> None:
    assert _validate_prerequisites(make_settings(**READY_ENV)) is None
