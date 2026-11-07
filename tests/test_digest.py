from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.bot.publisher import DiscordPublicationRejectedError, MarkerLookup
from app.jobs.digest import (
    INITIAL_DIGEST_WINDOW_START,
    DigestService,
    digest_window_incomplete,
    latest_digest_due_at,
    publication_marker,
)

NOW = datetime(2026, 1, 2, 5, 0, tzinfo=UTC)
MEDIA_BYTES = b"archived-media-bytes"


def iso(value: datetime) -> str:
    return value.isoformat()


def created(value: datetime) -> str:
    return value.isoformat()


class FakeDigestRepository:
    def __init__(self) -> None:
        self.state: dict[str, object] = {"id": 1}
        self.media: dict[str, dict[str, object]] = {}
        self.publications: dict[str, dict[str, object]] = {}
        self.publication_by_key: dict[tuple[str, int], str] = {}
        self.sources: dict[str, dict[str, object]] = {}
        self.jobs: dict[str, dict[str, object]] = {}
        self.finished_jobs: list[dict[str, object]] = []
        self.candidate_queries: list[dict[str, object]] = []
        self.retry_queries: list[dict[str, object]] = []
        self.digest_success_calls = 0
        self.fail_ensure = False
        self.fail_mark_published = False
        self.fail_mark_success = False
        self.fail_source = False
        self._next_publication = 1

    def add_media(
        self,
        media_id: str,
        *,
        data: bytes = MEDIA_BYTES,
        created_at: str = "2026-01-01T00:00:00+00:00",
        category: str | None = "fanart",
        verification_status: str = "verified",
        download_status: str = "stored",
        sha256: str | None = None,
        size_bytes: int | None = None,
        storage_object_key: str | None = None,
        filename: str | None = None,
        title: str | None = "Archived artwork",
        caption: str | None = None,
    ) -> dict[str, object]:
        row: dict[str, object] = {
            "id": media_id,
            "created_at": created_at,
            "category": category,
            "verification_status": verification_status,
            "download_status": download_status,
            "sha256": sha256 if sha256 is not None else hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data) if size_bytes is None else size_bytes,
            "storage_object_key": (
                storage_object_key if storage_object_key is not None else f"media/{media_id}"
            ),
            "filename": filename if filename is not None else f"{media_id}.png",
            "extension": "png",
            "title": title,
            "caption": caption,
            "media_type": "image",
        }
        self.media[media_id] = row
        self.sources.setdefault(
            media_id,
            {
                "title": title,
                "caption": caption,
                "creator_name": "artist",
                "creator_profile_url": "https://example.com/artist",
                "source_url": "https://example.com/post",
                "source_platform": "web",
                "source_post_id": "https://example.com/post",
            },
        )
        return row

    def add_publication(
        self,
        media_id: str,
        channel_id: int,
        *,
        status: str = "pending",
        attempt_count: int = 0,
        publication_id: str | None = None,
        last_error: str | None = None,
    ) -> dict[str, object]:
        publication_id = publication_id or f"publication-{self._next_publication}"
        self._next_publication += 1
        row: dict[str, object] = {
            "id": publication_id,
            "media_item_id": media_id,
            "channel_id": channel_id,
            "purpose": "daily_digest",
            "status": status,
            "attempt_count": attempt_count,
            "last_error": last_error,
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        self.publications[publication_id] = row
        self.publication_by_key[(media_id, channel_id)] = publication_id
        return row

    async def create_job_run(self, config: dict[str, object], job_type: str = "") -> str:
        assert job_type == "daily_digest"
        job_id = f"job-{len(self.jobs) + 1}"
        self.jobs[job_id] = {"config": config, "job_type": job_type}
        await asyncio.sleep(0)
        return job_id

    async def finish_job_run(
        self,
        job_id: str,
        counters: dict[str, int],
        outcome: str,
        error: str | None = None,
    ) -> None:
        self.finished_jobs.append(
            {
                "job_id": job_id,
                "counters": dict(counters),
                "outcome": outcome,
                "error": error,
            }
        )

    async def get_digest_state(self) -> dict[str, object]:
        return dict(self.state)

    async def update_digest_state(self, fields: dict[str, object]) -> dict[str, object]:
        self.state.update(fields)
        return dict(self.state)

    async def list_digest_candidates(
        self,
        *,
        window_start: str,
        window_end: str,
        cursor: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, object]]:
        self.candidate_queries.append(
            {
                "window_start": window_start,
                "window_end": window_end,
                "cursor": cursor,
                "limit": limit,
                "offset": offset,
            }
        )
        lower = datetime.fromisoformat(cursor or window_start)
        upper = datetime.fromisoformat(window_end)
        rows = [
            row
            for row in self.media.values()
            if lower <= datetime.fromisoformat(str(row["created_at"])) <= upper
        ]
        rows.sort(key=lambda row: (str(row["created_at"]), str(row["id"])))
        return [dict(row) for row in rows[offset : offset + limit]]

    async def ensure_digest_publication(
        self, media_item_id: str, channel_id: int
    ) -> dict[str, object]:
        if self.fail_ensure:
            raise RuntimeError("publication insert failed")
        publication_id = self.publication_by_key.get((media_item_id, channel_id))
        if publication_id is not None:
            return dict(self.publications[publication_id])
        row = self.add_publication(media_item_id, channel_id)
        return dict(row)

    async def list_retryable_digest_publications(
        self, *, attempt_limit: int, limit: int = 100
    ) -> list[dict[str, object]]:
        self.retry_queries.append({"attempt_limit": attempt_limit, "limit": limit})
        rows = [
            row
            for row in self.publications.values()
            if row["status"] in {"pending", "failed"} and int(row["attempt_count"]) < attempt_limit
        ]
        return [dict(row) for row in rows[:limit]]

    async def get_media_item(self, media_item_id: str) -> dict[str, object] | None:
        row = self.media.get(media_item_id)
        return dict(row) if row is not None else None

    async def source_for_media(self, media_item_id: str) -> dict[str, object] | None:
        if self.fail_source:
            raise RuntimeError("source lookup failed")
        row = self.sources.get(media_item_id)
        return dict(row) if row is not None else None

    async def record_publication_attempt(
        self, publication_id: str, *, expected_attempt_count: int
    ) -> dict[str, object]:
        row = self.publications[publication_id]
        if int(row["attempt_count"]) == expected_attempt_count:
            row["attempt_count"] = expected_attempt_count + 1
            row["status"] = "pending"
            row["last_error"] = None
        return dict(row)

    async def mark_publication_published(
        self, publication_id: str, message_id: int
    ) -> dict[str, object]:
        if self.fail_mark_published:
            raise RuntimeError("database update failed")
        row = self.publications[publication_id]
        row["status"] = "published"
        row["message_id"] = message_id
        row["published_at"] = iso(NOW)
        row["last_error"] = None
        return dict(row)

    async def mark_publication_failed(self, publication_id: str, error: str) -> dict[str, object]:
        row = self.publications[publication_id]
        row["status"] = "failed"
        row["last_error"] = error
        return dict(row)

    async def record_publication_reconciliation_error(
        self, publication_id: str, error: str
    ) -> dict[str, object]:
        row = self.publications[publication_id]
        row["last_error"] = error
        return dict(row)

    async def mark_digest_success(self) -> None:
        if self.fail_mark_success:
            raise RuntimeError("success update failed")
        self.digest_success_calls += 1


class FakeStorage:
    def __init__(
        self,
        *,
        data: bytes = MEDIA_BYTES,
        error: Exception | None = None,
        data_by_key: dict[str, bytes] | None = None,
    ) -> None:
        self.data = data
        self.error = error
        self.data_by_key = data_by_key or {}
        self.calls: list[tuple[str, int | None]] = []

    async def download_bytes(
        self,
        object_key: str,
        *,
        max_bytes: int | None = None,
        timeout: float | None = None,
    ) -> bytes:
        del timeout
        self.calls.append((object_key, max_bytes))
        if self.error is not None:
            raise self.error
        return self.data_by_key.get(object_key, self.data)


class FakePublisher:
    def __init__(
        self,
        *,
        message_id: int = 555,
        error: Exception | None = None,
        fail_filenames: set[str] | None = None,
        marker_lookup: object = None,
    ) -> None:
        self.message_id = message_id
        self.error = error
        self.fail_filenames = fail_filenames or set()
        self.marker_lookup = marker_lookup
        self.calls: list[dict[str, object]] = []
        self.marker_calls: list[dict[str, object]] = []

    async def publish(
        self,
        *,
        filename: str,
        data: bytes,
        context: object,
        channel_id: int | None = None,
    ) -> int:
        self.calls.append(
            {
                "filename": filename,
                "data": data,
                "context": context,
                "channel_id": channel_id,
            }
        )
        if self.error is not None:
            raise self.error
        if filename in self.fail_filenames:
            raise RuntimeError("discord attachment failed")
        return self.message_id

    async def find_publication_marker(self, *, channel_id: int, marker: str) -> MarkerLookup:
        self.marker_calls.append({"channel_id": channel_id, "marker": marker})
        if isinstance(self.marker_lookup, Exception):
            raise self.marker_lookup
        if callable(self.marker_lookup):
            return self.marker_lookup(channel_id=channel_id, marker=marker)
        return self.marker_lookup or MarkerLookup(message_id=None, conclusive=True)


def build_service(
    make_settings,
    repository: FakeDigestRepository,
    storage: FakeStorage,
    publisher: FakePublisher,
    *,
    attempt_limit: int = 3,
    page_size: int = 100,
    now: datetime = NOW,
    **setting_overrides: object,
) -> DigestService:
    settings = make_settings(**setting_overrides)
    return DigestService(
        settings=settings,
        repository=repository,
        storage=storage,
        publisher=publisher,
        attempt_limit=attempt_limit,
        page_size=page_size,
        now=lambda: now,
    )


async def test_first_run_processes_backlog_to_mapped_category_channel(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.add_media(
        "media-1",
        created_at="2020-01-01T00:00:00+00:00",
        category="fanart",
    )
    storage = FakeStorage()
    publisher = FakePublisher()
    service = build_service(
        make_settings,
        repository,
        storage,
        publisher,
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
        DISCORD_CATEGORY_CHANNELS_JSON='{"fanart": 222}',
    )

    result = await service.run(trigger="scheduled")

    assert result.outcome == "succeeded"
    assert result.processed_count == 1
    assert result.published_count == 1
    assert result.failed_count == 0
    assert result.window_start == iso(INITIAL_DIGEST_WINDOW_START)
    assert result.window_end == iso(NOW)
    assert result.resumed is False
    assert result.job_id == "job-1"
    assert storage.calls == [("media/media-1", 10_000_000)]
    assert len(publisher.calls) == 1
    call = publisher.calls[0]
    assert call["channel_id"] == 222
    assert call["filename"] == "media-1.png"
    assert call["data"] == MEDIA_BYTES
    context = call["context"]
    assert context.category == "fanart"
    assert context.title == "Archived artwork"
    assert context.creator_name == "artist"
    publication = repository.publications["publication-1"]
    assert context.publication_marker == publication_marker("publication-1")
    assert publication["status"] == "published"
    assert publication["message_id"] == 555
    assert publication["attempt_count"] == 1
    assert repository.digest_success_calls == 1
    assert repository.finished_jobs[-1]["outcome"] == "succeeded"
    assert repository.finished_jobs[-1]["counters"] == {
        "processed_count": 1,
        "stored_count": 1,
        "skipped_count": 0,
        "failed_count": 0,
    }
    assert repository.state["last_window_start"] == iso(INITIAL_DIGEST_WINDOW_START)
    assert repository.state["last_window_end"] == iso(NOW)
    assert repository.state["last_completed_at"] == iso(NOW)
    assert repository.state["cursor_media_created_at"] == iso(NOW)


async def test_unmapped_category_uses_discoveries_fallback(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.add_media("media-1", category="cosplay_photography")
    publisher = FakePublisher()
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
        DISCORD_CATEGORY_CHANNELS_JSON='{"fanart": 222}',
    )

    result = await service.run()

    assert result.outcome == "succeeded"
    assert publisher.calls[0]["channel_id"] == 999


async def test_missing_destination_fails_item_without_false_publication(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.add_media("media-1", category="other")
    publisher = FakePublisher()
    service = build_service(make_settings, repository, FakeStorage(), publisher)

    result = await service.run()

    assert result.outcome == "partial"
    assert result.failed_count == 1
    assert "destination_not_configured" in (result.error_summary or "")
    assert publisher.calls == []
    assert repository.publications == {}
    assert repository.digest_success_calls == 0
    assert "last_completed_at" not in repository.state
    assert repository.state["last_window_end"] == iso(NOW)


async def test_unaccounted_item_holds_window_until_destination_is_configured(
    make_settings,
) -> None:
    repository = FakeDigestRepository()
    repository.add_media(
        "media-1",
        category="other",
        created_at="2026-01-01T00:00:00+00:00",
    )
    repository.add_media(
        "media-2",
        category="fanart",
        created_at="2026-01-01T00:00:01+00:00",
    )
    publisher = FakePublisher()
    first_service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        DISCORD_CATEGORY_CHANNELS_JSON='{"fanart": 222}',
    )

    first = await first_service.run()

    assert first.outcome == "partial"
    assert first.published_count == 1
    assert first.failed_count == 1
    assert "last_completed_at" not in repository.state
    assert repository.state["cursor_media_created_at"] == iso(INITIAL_DIGEST_WINDOW_START)

    second_service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        DISCORD_CATEGORY_CHANNELS_JSON='{"fanart": 222, "other": 333}',
    )

    second = await second_service.run()

    assert second.resumed is True
    assert second.published_count == 1
    assert second.skipped_count == 1
    assert second.outcome == "succeeded"
    assert [call["channel_id"] for call in publisher.calls] == [222, 333]
    assert repository.state["last_completed_at"] == iso(NOW)


async def test_ineligible_media_is_skipped_without_publication(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.add_media("pending", verification_status="pending")
    repository.add_media("unverified", verification_status="dumpster")
    repository.add_media("failed", download_status="failed")
    repository.add_media("skipped", download_status="skipped")
    repository.add_media("missing-key", storage_object_key="")
    repository.add_media("bad-sha", sha256="not-a-sha", storage_object_key="media/bad-sha")
    repository.add_media("oversize", size_bytes=10_000_001)
    repository.add_media("too-small", size_bytes=0)
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        FakePublisher(),
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
    )

    result = await service.run()

    assert result.outcome == "succeeded"
    assert result.processed_count == 8
    assert result.skipped_count == 8
    assert result.published_count == 0
    assert repository.publications == {}


async def test_pagination_processes_more_than_one_database_page(make_settings) -> None:
    repository = FakeDigestRepository()
    for index in range(5):
        repository.add_media(
            f"media-{index}",
            created_at=f"2025-12-31T00:0{index}:00+00:00",
        )
    publisher = FakePublisher()
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        page_size=2,
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
    )

    result = await service.run()

    assert result.published_count == 5
    assert len(publisher.calls) == 5
    assert [query["offset"] for query in repository.candidate_queries] == [0, 2, 4]
    assert all(query["limit"] == 2 for query in repository.candidate_queries)


async def test_published_publication_is_skipped_without_download(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.add_media("media-1")
    repository.add_publication("media-1", 999, status="published", attempt_count=1)
    storage = FakeStorage()
    publisher = FakePublisher()
    service = build_service(
        make_settings,
        repository,
        storage,
        publisher,
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
    )

    result = await service.run()

    assert result.outcome == "succeeded"
    assert result.skipped_count == 1
    assert result.published_count == 0
    assert storage.calls == []
    assert publisher.calls == []
    assert len(repository.publications) == 1


async def test_existing_pending_publication_is_reused(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.add_media("media-1")
    existing = repository.add_publication("media-1", 999, status="pending", attempt_count=0)
    publisher = FakePublisher()
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
    )

    result = await service.run()

    assert result.published_count == 1
    assert len(repository.publications) == 1
    assert repository.publications[existing["id"]]["status"] == "published"
    assert repository.publications[existing["id"]]["attempt_count"] == 1


async def test_failed_publication_below_limit_is_retried(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.add_media("media-1")
    existing = repository.add_publication(
        "media-1", 999, status="failed", attempt_count=1, last_error="discord_delivery_failed"
    )
    publisher = FakePublisher()
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
    )

    result = await service.run()

    assert result.published_count == 1
    assert len(publisher.calls) == 1
    assert repository.publications[existing["id"]]["status"] == "published"
    assert repository.publications[existing["id"]]["attempt_count"] == 2
    assert repository.retry_queries[-1]["attempt_limit"] == 3


async def test_exhausted_publication_is_not_retried(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.add_media("media-1")
    existing = repository.add_publication(
        "media-1",
        999,
        status="failed",
        attempt_count=3,
        last_error="discord_delivery_failed",
    )
    publisher = FakePublisher()
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
    )

    result = await service.run()

    assert result.published_count == 0
    assert result.skipped_count == 1
    assert publisher.calls == []
    assert repository.publications[existing["id"]]["attempt_count"] == 3
    assert repository.publications[existing["id"]]["status"] == "failed"


async def test_sha256_mismatch_prevents_delivery(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.add_media("media-1", data=b"original")
    storage = FakeStorage(data=b"tampered")
    publisher = FakePublisher()
    service = build_service(
        make_settings,
        repository,
        storage,
        publisher,
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
    )

    result = await service.run()

    assert result.outcome == "partial"
    assert result.failed_count == 1
    assert result.published_count == 0
    assert publisher.calls == []
    publication = repository.publications["publication-1"]
    assert publication["status"] == "failed"
    assert publication["last_error"] == "integrity_verification_failed"
    assert publication["attempt_count"] == 1
    assert repository.state["last_completed_at"] == iso(NOW)


async def test_storage_failure_marks_retryable_failure(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.add_media("media-1")
    storage = FakeStorage(error=RuntimeError("download unavailable"))
    publisher = FakePublisher()
    service = build_service(
        make_settings,
        repository,
        storage,
        publisher,
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
    )

    result = await service.run()

    assert result.outcome == "partial"
    assert result.failed_count == 1
    assert publisher.calls == []
    assert storage.calls == [("media/media-1", 10_000_000)]
    publication = repository.publications["publication-1"]
    assert publication["status"] == "failed"
    assert publication["last_error"] == "storage_download_failed:RuntimeError"


async def test_empty_download_is_rejected(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.add_media("media-1")
    publisher = FakePublisher()
    service = build_service(
        make_settings,
        repository,
        FakeStorage(data=b""),
        publisher,
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
    )

    result = await service.run()

    assert result.failed_count == 1
    assert publisher.calls == []
    assert repository.publications["publication-1"]["last_error"] == "invalid_downloaded_size"


async def test_ambiguous_pending_marker_is_reconciled_without_resend(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.add_media("media-1")
    existing = repository.add_publication("media-1", 999, status="pending", attempt_count=1)
    publisher = FakePublisher(marker_lookup=MarkerLookup(message_id=777, conclusive=True))
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
    )

    result = await service.run()

    assert result.outcome == "succeeded"
    assert result.skipped_count == 1
    assert publisher.calls == []
    assert publisher.marker_calls == [
        {
            "channel_id": 999,
            "marker": publication_marker(str(existing["id"])),
        }
    ]
    publication = repository.publications[existing["id"]]
    assert publication["status"] == "published"
    assert publication["message_id"] == 777
    assert publication["attempt_count"] == 1


async def test_ambiguous_pending_without_marker_is_retried_conclusively(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.add_media("media-1")
    existing = repository.add_publication("media-1", 999, status="pending", attempt_count=1)
    publisher = FakePublisher(marker_lookup=MarkerLookup(message_id=None, conclusive=True))
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
    )

    result = await service.run()

    assert result.published_count == 1
    assert len(publisher.calls) == 1
    assert repository.publications[existing["id"]]["attempt_count"] == 2
    assert repository.publications[existing["id"]]["status"] == "published"


async def test_ambiguous_pending_inconclusive_lookup_fails_closed(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.add_media("media-1")
    existing = repository.add_publication("media-1", 999, status="pending", attempt_count=1)
    publisher = FakePublisher(marker_lookup=MarkerLookup(message_id=None, conclusive=False))
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
    )

    result = await service.run()

    assert result.outcome == "partial"
    assert result.failed_count == 1
    assert result.published_count == 0
    assert publisher.calls == []
    publication = repository.publications[existing["id"]]
    assert publication["status"] == "pending"
    assert publication["attempt_count"] == 1
    assert publication["last_error"] == "delivery_unreconciled"
    assert repository.digest_success_calls == 0


async def test_reconciliation_lookup_error_fails_closed(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.add_media("media-1")
    existing = repository.add_publication("media-1", 999, status="pending", attempt_count=1)
    publisher = FakePublisher(marker_lookup=RuntimeError("history unavailable"))
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
    )

    result = await service.run()

    assert result.failed_count == 1
    assert publisher.calls == []
    assert repository.publications[existing["id"]]["status"] == "pending"
    assert (
        repository.publications[existing["id"]]["last_error"]
        == "reconciliation_failed:RuntimeError"
    )


async def test_ambiguous_discord_send_failure_is_reconciled_before_retry(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.add_media("media-1")

    class AcceptedThenTimedOutPublisher(FakePublisher):
        accepted: list[int] = []

        async def publish(self, **kwargs: object) -> int:
            self.calls.append(kwargs)
            self.accepted.append(4242)
            raise TimeoutError("response lost after acceptance")

        async def find_publication_marker(self, *, channel_id: int, marker: str) -> MarkerLookup:
            self.marker_calls.append({"channel_id": channel_id, "marker": marker})
            return MarkerLookup(message_id=self.accepted[0], conclusive=True)

    publisher = AcceptedThenTimedOutPublisher()
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
    )

    result = await service.run()

    assert result.outcome == "partial"
    assert result.failed_count == 1
    assert result.published_count == 0
    publication = repository.publications["publication-1"]
    assert publication["status"] == "failed"
    assert publication["attempt_count"] == 1
    assert publication["last_error"] == "discord_delivery_ambiguous:TimeoutError"

    retry = await service.run()

    assert retry.published_count == 0
    assert retry.skipped_count >= 1
    assert len(publisher.calls) == 1
    assert publisher.accepted == [4242]
    assert publisher.marker_calls == [
        {"channel_id": 999, "marker": publication_marker(str(publication["id"]))}
    ]
    assert publication["status"] == "published"
    assert publication["message_id"] == 4242


async def test_definite_discord_rejection_leaves_retryable_failed_record(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.add_media("media-1")
    publisher = FakePublisher(error=DiscordPublicationRejectedError("HTTP 403"))
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
    )

    result = await service.run()

    assert result.outcome == "partial"
    assert result.failed_count == 1
    publication = repository.publications["publication-1"]
    assert publication["status"] == "failed"
    assert publication["last_error"] == "discord_delivery_failed:DiscordPublicationRejectedError"


async def test_database_update_after_send_is_recovered_by_marker(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.add_media("media-1")
    repository.fail_mark_published = True
    storage = FakeStorage()
    publisher = FakePublisher(marker_lookup=MarkerLookup(message_id=888, conclusive=True))
    service = build_service(
        make_settings,
        repository,
        storage,
        publisher,
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
    )

    first = await service.run()

    assert first.outcome == "partial"
    assert first.published_count == 1
    assert "publication_status_update_failed" in (first.error_summary or "")
    publication = repository.publications["publication-1"]
    assert publication["status"] == "pending"
    assert publication["attempt_count"] == 1
    assert len(publisher.calls) == 1

    repository.fail_mark_published = False
    second = await service.run()

    assert second.outcome == "succeeded"
    assert second.skipped_count == 1
    assert len(publisher.calls) == 1
    assert len(storage.calls) == 1
    publication = repository.publications["publication-1"]
    assert publication["status"] == "published"
    assert publication["message_id"] == 888


async def test_failed_attachment_does_not_stop_unrelated_items(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.add_media("media-a", filename="a.png", created_at="2026-01-01T00:00:00+00:00")
    repository.add_media("media-b", filename="b.png", created_at="2026-01-01T00:00:01+00:00")
    publisher = FakePublisher(fail_filenames={"a.png"})
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
    )

    result = await service.run()

    assert result.processed_count == 2
    assert result.failed_count == 1
    assert result.published_count == 1
    assert result.outcome == "partial"
    assert repository.publications["publication-1"]["status"] == "failed"
    assert repository.publications["publication-2"]["status"] == "published"


async def test_retryable_publication_outside_window_is_retried(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.add_media("media-1", created_at="2025-12-01T00:00:00+00:00")
    existing = repository.add_publication(
        "media-1", 999, status="failed", attempt_count=1, last_error="discord_delivery_failed"
    )
    repository.state = {
        "id": 1,
        "last_window_start": iso(NOW - timedelta(days=1)),
        "last_window_end": iso(NOW - timedelta(hours=1)),
        "last_completed_at": iso(NOW - timedelta(hours=1)),
        "cursor_media_created_at": iso(NOW - timedelta(hours=1)),
    }
    publisher = FakePublisher()
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
    )

    result = await service.run()

    assert result.published_count == 1
    assert len(publisher.calls) == 1
    assert repository.publications[existing["id"]]["status"] == "published"


async def test_retry_is_skipped_when_resolved_destination_changed(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.add_media("media-1", category="fanart", created_at="2025-12-01T00:00:00+00:00")
    existing = repository.add_publication(
        "media-1", 999, status="failed", attempt_count=1, last_error="discord_delivery_failed"
    )
    repository.state = {
        "id": 1,
        "last_window_start": iso(NOW - timedelta(days=1)),
        "last_window_end": iso(NOW - timedelta(hours=1)),
        "last_completed_at": iso(NOW - timedelta(hours=1)),
        "cursor_media_created_at": iso(NOW - timedelta(hours=1)),
    }
    publisher = FakePublisher()
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
        DISCORD_CATEGORY_CHANNELS_JSON='{"fanart": 222}',
    )

    result = await service.run()

    assert result.published_count == 0
    assert result.skipped_count == 1
    assert publisher.calls == []
    assert repository.publications[existing["id"]]["attempt_count"] == 1


async def test_publication_record_failure_is_isolated(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.add_media("media-1")
    repository.fail_ensure = True
    publisher = FakePublisher()
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
    )

    result = await service.run()

    assert result.outcome == "partial"
    assert result.failed_count == 1
    assert publisher.calls == []
    assert "publication_record_failed:RuntimeError" in (result.error_summary or "")
    assert "unaccounted_items" in (result.error_summary or "")
    assert "last_completed_at" not in repository.state


async def test_missing_source_attribution_does_not_fabricate_metadata(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.add_media("media-1", title="Title from media", caption=None)
    repository.sources.clear()
    publisher = FakePublisher()
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
    )

    result = await service.run()

    assert result.outcome == "succeeded"
    context = publisher.calls[0]["context"]
    assert context.title == "Title from media"
    assert context.creator_name is None
    assert context.source_url is None


async def test_source_lookup_failure_still_delivers_truthfully(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.add_media("media-1")
    repository.fail_source = True
    publisher = FakePublisher()
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
    )

    result = await service.run()

    assert result.outcome == "succeeded"
    context = publisher.calls[0]["context"]
    assert context.creator_name is None
    assert context.source_url is None


async def test_incomplete_window_is_resumed_from_persisted_cursor(make_settings) -> None:
    repository = FakeDigestRepository()
    window_start = NOW - timedelta(days=2)
    window_end = NOW - timedelta(days=1)
    cursor = window_start + timedelta(hours=1)
    repository.state = {
        "id": 1,
        "last_window_start": iso(window_start),
        "last_window_end": iso(window_end),
        "last_completed_at": None,
        "cursor_media_created_at": iso(cursor),
    }
    repository.add_media(
        "media-after-cursor",
        created_at=iso(cursor + timedelta(minutes=5)),
    )
    repository.add_media(
        "media-before-cursor",
        created_at=iso(window_start + timedelta(minutes=5)),
    )
    publisher = FakePublisher()
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
    )

    result = await service.run()

    assert result.resumed is True
    assert result.window_start == iso(window_start)
    assert result.window_end == iso(window_end)
    assert repository.candidate_queries[0]["cursor"] == iso(cursor)
    assert len(publisher.calls) == 1
    assert publisher.calls[0]["context"].title == "Archived artwork"
    assert repository.state["last_window_end"] == iso(window_end)
    assert repository.state["last_completed_at"] == iso(NOW)


async def test_next_window_starts_at_previous_end(make_settings) -> None:
    previous_end = NOW - timedelta(hours=1)
    repository = FakeDigestRepository()
    repository.state = {
        "id": 1,
        "last_window_start": iso(previous_end - timedelta(days=1)),
        "last_window_end": iso(previous_end),
        "last_completed_at": iso(previous_end),
        "cursor_media_created_at": iso(previous_end),
    }
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        FakePublisher(),
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
    )

    result = await service.run()

    assert result.resumed is False
    assert result.window_start == iso(previous_end)
    assert result.window_end == iso(NOW)
    assert repository.candidate_queries[0]["cursor"] == iso(previous_end)


async def test_overlapping_run_is_rejected_without_job_record(make_settings) -> None:
    repository = FakeDigestRepository()
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        FakePublisher(),
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
    )

    async with service._lock:
        result = await service.run()

    assert result.outcome == "skipped"
    assert result.error_summary == "digest_already_running"
    assert repository.jobs == {}
    assert repository.candidate_queries == []


async def test_success_update_failure_marks_partial(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.add_media("media-1")
    repository.fail_mark_success = True
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        FakePublisher(),
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
    )

    result = await service.run()

    assert result.outcome == "partial"
    assert result.published_count == 1
    assert "digest_success_update_failed:RuntimeError" in (result.error_summary or "")
    assert repository.finished_jobs[-1]["outcome"] == "partial"


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"id": 1}, True),
        (
            {
                "last_window_start": iso(NOW - timedelta(days=3)),
                "last_window_end": iso(NOW - timedelta(days=2)),
                "last_completed_at": iso(NOW - timedelta(days=2)),
            },
            True,
        ),
    ],
)
async def test_catch_up_required_for_fresh_and_missed_state(
    make_settings, state: dict[str, object], expected: bool
) -> None:
    repository = FakeDigestRepository()
    repository.state = dict(state)
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        FakePublisher(),
        now=datetime(2026, 1, 2, 5, 0, tzinfo=UTC),
    )

    assert await service.catch_up_required() is expected


async def test_catch_up_not_required_after_latest_due_completion(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.state = {
        "id": 1,
        "last_window_start": iso(NOW - timedelta(days=1)),
        "last_window_end": iso(NOW - timedelta(hours=1)),
        "last_completed_at": iso(NOW - timedelta(hours=1)),
        "cursor_media_created_at": iso(NOW - timedelta(hours=1)),
    }
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        FakePublisher(),
        now=datetime(2026, 1, 2, 5, 0, tzinfo=UTC),
    )

    assert await service.catch_up_required() is False


async def test_incomplete_window_forces_catch_up_even_after_due_completion(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.state = {
        "id": 1,
        "last_window_start": iso(NOW - timedelta(hours=3)),
        "last_window_end": iso(NOW - timedelta(hours=2)),
        "last_completed_at": iso(NOW - timedelta(hours=4)),
        "cursor_media_created_at": iso(NOW - timedelta(hours=3)),
    }
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        FakePublisher(),
        now=datetime(2026, 1, 2, 5, 0, tzinfo=UTC),
    )

    assert await service.catch_up_required() is True


def test_latest_digest_due_at_uses_kolkata_schedule() -> None:
    kolkata = ZoneInfo("Asia/Kolkata")

    before = datetime(2026, 1, 2, 3, 59, tzinfo=kolkata)
    at_time = datetime(2026, 1, 2, 4, 0, tzinfo=kolkata)
    after = datetime(2026, 1, 2, 4, 1, tzinfo=kolkata)

    assert latest_digest_due_at(before) == datetime(2026, 1, 1, 4, 0, tzinfo=kolkata)
    assert latest_digest_due_at(at_time) == at_time
    assert latest_digest_due_at(after) == at_time


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (None, False),
        ({}, False),
        ({"last_window_start": iso(NOW), "last_window_end": iso(NOW)}, True),
        (
            {
                "last_window_start": iso(NOW - timedelta(hours=1)),
                "last_window_end": iso(NOW),
                "last_completed_at": iso(NOW - timedelta(minutes=30)),
            },
            True,
        ),
        (
            {
                "last_window_start": iso(NOW - timedelta(hours=1)),
                "last_window_end": iso(NOW),
                "last_completed_at": iso(NOW),
            },
            False,
        ),
    ],
)
def test_digest_window_incomplete(state: dict[str, object] | None, expected: bool) -> None:
    assert digest_window_incomplete(state) is expected


async def test_service_ignores_collection_enabled_state(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.state = {"id": 1, "collection_enabled": False}
    repository.add_media("media-1")
    publisher = FakePublisher()
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
    )

    result = await service.run(trigger="startup_catch_up")

    assert result.outcome == "succeeded"
    assert result.published_count == 1
    assert repository.jobs["job-1"]["config"] == {"trigger": "startup_catch_up"}
    assert len(publisher.calls) == 1


async def test_concurrent_runs_are_serialized(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.add_media("media-1")
    publisher = FakePublisher()
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
    )

    first, second = await asyncio.gather(
        service.run(trigger="scheduled"),
        service.run(trigger="startup_catch_up"),
    )

    assert {first.outcome, second.outcome} == {"succeeded", "skipped"}
    assert len(publisher.calls) == 1
