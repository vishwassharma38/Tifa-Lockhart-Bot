from __future__ import annotations

from app.discovery.challenges import detect_challenge


def test_strong_challenge_markers_are_detected() -> None:
    assert detect_challenge("<html><body><div class='cf-challenge'></div></body></html>")
    assert detect_challenge("<html><body><div class='g-recaptcha'></div></body></html>")
    assert detect_challenge("<html><body><p>Checking proof-of-work...</p></body></html>")
    assert detect_challenge("<html><body><div id='challenge-platform'></div></body></html>")


def test_weak_markers_require_multiple_independent_signals() -> None:
    assert detect_challenge("<html><body><p>Access denied</p></body></html>") is None
    assert (
        detect_challenge(
            "<html><body><p>Access denied</p><p>Please verify you are human</p></body></html>"
        )
        == "anti_automation_challenge"
    )


def test_normal_page_content_is_not_treated_as_a_challenge() -> None:
    html = """
    <html><head><title>Tifa Lockhart gallery</title></head>
    <body><p>This article discusses access denied errors in web servers.</p></body></html>
    """

    assert detect_challenge(html) is None


def test_empty_responses_are_not_challenges() -> None:
    assert detect_challenge(None) is None
    assert detect_challenge("") is None
