from app.config import Settings


def test_safe_collection_defaults(monkeypatch):
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    monkeypatch.delenv("REDDIT_ACCESS_APPROVED", raising=False)
    monkeypatch.delenv("BRAVE_HTML_ENABLED", raising=False)
    settings = Settings.from_env()
    assert settings.discord_token == ""
    assert settings.reddit_access_approved is False
    assert settings.brave_html_enabled is False


def test_boolean_environment_values(monkeypatch):
    monkeypatch.setenv("REDDIT_ACCESS_APPROVED", "true")
    monkeypatch.setenv("BRAVE_HTML_ENABLED", "0")
    settings = Settings.from_env()
    assert settings.reddit_access_approved is True
    assert settings.brave_html_enabled is False
