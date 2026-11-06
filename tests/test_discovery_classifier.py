from __future__ import annotations

from app.discovery.classifier import (
    DUMPSTER,
    REJECTED,
    VERIFIED,
    classify_page,
)


def test_page_metadata_with_full_name_is_verified() -> None:
    result = classify_page(title="Tifa Lockhart gallery")

    assert result.status == VERIFIED
    assert result.score == 4
    assert result.reasons == ("page_metadata_tifa_lockhart",)
    assert result.is_verified is True
    assert result.allows_child_crawl is True


def test_tifa_metadata_with_final_fantasy_context_reaches_the_threshold() -> None:
    result = classify_page(
        title="Tifa",
        description="Fan art from Final Fantasy VII",
    )

    assert result.status == VERIFIED
    assert result.score == 4
    assert "page_metadata_tifa" in result.reasons
    assert "final_fantasy_vii_context" in result.reasons


def test_tifa_in_metadata_alone_is_staged_in_the_dumpster() -> None:
    result = classify_page(title="Tifa renders")

    assert result.status == DUMPSTER
    assert result.score == 3
    assert "insufficient_attribution" in result.reasons


def test_partial_name_alone_is_staged_in_the_dumpster() -> None:
    result = classify_page(title="Lockhart photography")

    assert result.status == DUMPSTER
    assert result.score == 1


def test_tifa_only_in_visible_text_is_staged_in_the_dumpster() -> None:
    result = classify_page(
        title="Character gallery",
        text="A collection featuring Tifa and other characters.",
    )

    assert result.status == DUMPSTER
    assert result.score == 1
    assert "visible_text_tifa" in result.reasons


def test_full_name_in_metadata_and_body_is_verified() -> None:
    result = classify_page(
        title="Tifa",
        text="This page contains Tifa Lockhart artwork.",
    )

    assert result.status == VERIFIED
    assert result.score == 5


def test_nibelheim_context_can_verify_tifa_metadata() -> None:
    result = classify_page(title="Tifa", tags=("Nibelheim",))

    assert result.status == VERIFIED
    assert result.score == 4
    assert "tifa_source_context" in result.reasons


def test_search_metadata_alone_can_never_verify_a_page() -> None:
    result = classify_page(
        title="An unrelated character page",
        text="Aerith is the subject of this article.",
        search_title="Tifa Lockhart",
        search_snippet="Tifa Lockhart fanart",
        search_phrase="Tifa Lockhart",
    )

    assert result.status == DUMPSTER
    assert result.score == 1
    assert "search_metadata_only" in result.reasons
    assert result.is_verified is False


def test_search_metadata_can_only_add_one_point_to_page_evidence() -> None:
    result = classify_page(
        title="Tifa",
        search_title="Tifa Lockhart",
        search_snippet="Tifa Lockhart fanart",
    )

    assert result.status == VERIFIED
    assert result.score == 4
    assert result.evidence["direct_score"] == 3
    assert result.evidence["search_score"] == 1


def test_missing_page_content_is_staged_for_review() -> None:
    result = classify_page(search_phrase="Tifa Lockhart")

    assert result.status == DUMPSTER
    assert "missing_page_metadata" in result.reasons


def test_no_tifa_evidence_is_rejected() -> None:
    result = classify_page(
        title="Cloud Strife",
        text="A page about a different Final Fantasy VII character.",
    )

    assert result.status == REJECTED
    assert result.score == 1
    assert "no_tifa_evidence" in result.reasons
    assert result.allows_child_crawl is False


def test_repeated_name_does_not_double_count_visible_text() -> None:
    result = classify_page(text="Tifa Tifa Tifa Lockhart Lockhart")

    assert result.score == 2
    assert result.reasons.count("visible_text_tifa_lockhart") == 1


def test_tifa_inside_another_word_is_not_a_match() -> None:
    result = classify_page(title="Tifanny and Lockharted")

    assert result.status == REJECTED
    assert result.score == 0


def test_classification_metadata_is_json_friendly() -> None:
    result = classify_page(title="Tifa Lockhart")

    metadata = result.to_metadata()
    assert metadata["status"] == VERIFIED
    assert metadata["score"] == 4
    assert isinstance(metadata["reasons"], list)
    assert metadata["evidence"]["tifa_in_page_metadata"] is True
