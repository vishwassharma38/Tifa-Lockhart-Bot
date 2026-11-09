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
    digest_cycle_at,
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
        self.reconciliation_queries: list[dict[str, object]] = []
        self.digest_success_calls = 0
        self.fail_ensure = False
        self.fail_mark_published = False
        self.fail_mark_success = False
        self.fail_source = False
        self.fail_finish_published = False
        self._claims: dict[str, tuple[str, datetime]] = {}
        self.current_time = NOW
        self.digest_limit = 50
        self._next_publication = 1
        self.validate_digest_cycles = False
        self.stale_reservation_count = 0
        self.advance_cycle_stale_count = 0
        self.on_cycle_stale = None
        self.reservation_calls: list[tuple[str, int, str]] = []
        self.attempt_cycle_calls: list[str] = []

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
            "retry_schedule_count": 0,
            "digest_terminal": False,
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

    async def reserve_digest_publication(
        self, media_item_id: str, channel_id: int, digest_cycle: str, *, limit: int = 50
    ) -> dict[str, object]:
        self.reservation_calls.append((media_item_id, channel_id, digest_cycle))
        if self.stale_reservation_count:
            self.stale_reservation_count -= 1
            return {"cycle_stale": True}
        if self.validate_digest_cycles and digest_cycle != digest_cycle_at(self.current_time):
            return {"cycle_stale": True}
        if self.fail_ensure:
            raise RuntimeError("publication insert failed")
        existing_id = next(
            (pid for (mid, _cid), pid in self.publication_by_key.items() if mid == media_item_id),
            None,
        )
        if existing_id:
            row = self.publications[existing_id]
            if row.get("digest_terminal"):
                return {**row, "terminal": True, "quota_exhausted": False}
            if row["status"] == "published":
                return dict(row)
            if int(row.get("attempt_count") or 0) == 0 and row.get("digest_cycle") not in (
                None,
                digest_cycle,
            ):
                reserved_now = sum(
                    item.get("digest_cycle") == digest_cycle and item.get("digest_slot_reserved")
                    for item in self.publications.values()
                    if item["id"] != existing_id
                )
                if reserved_now >= min(limit, self.digest_limit):
                    self._claims.pop(existing_id, None)
                    row.update(
                        digest_slot_reserved=False,
                        delivery_claim_token=None,
                        delivery_claim_expires_at=None,
                    )
                    return {"quota_exhausted": True}
                self._claims.pop(existing_id, None)
                row.update(
                    digest_cycle=digest_cycle,
                    digest_slot_reserved=True,
                    delivery_claim_token=None,
                    delivery_claim_expires_at=None,
                )
                return dict(row)
            if row.get("attempt_count", 0) and (
                row.get("status") == "pending"
                or str(row.get("last_error") or "").startswith(
                    ("discord_delivery_ambiguous:", "discord_delivery_failed")
                )
            ):
                return {**row, "requires_reconciliation": True}
            if row.get("digest_cycle") == digest_cycle and row.get("digest_slot_reserved"):
                return dict(row)
        reserved = sum(
            1
            for row in self.publications.values()
            if row.get("digest_cycle") == digest_cycle and row.get("digest_slot_reserved")
        )
        if reserved >= min(limit, self.digest_limit):
            return {"quota_exhausted": True}
        if existing_id:
            row = self.publications[existing_id]
            row.update(digest_cycle=digest_cycle, digest_slot_reserved=True)
            return dict(row)
        row = self.add_publication(media_item_id, channel_id)
        row.update(digest_cycle=digest_cycle, digest_slot_reserved=True)
        return dict(row)

    async def list_retryable_digest_publications(
        self, *, attempt_limit: int, limit: int = 100, now: str | None = None
    ) -> list[dict[str, object]]:
        now = now or iso(self.current_time)
        self.current_time = datetime.fromisoformat(now)
        self.retry_queries.append({"attempt_limit": attempt_limit, "limit": limit, "now": now})
        rows = [
            row
            for row in self.publications.values()
            if row["status"] in {"pending", "failed"}
            and int(row["attempt_count"]) < attempt_limit
            and not row.get("digest_terminal")
            and not (
                self._claims.get(str(row["id"]))
                and self._claims[str(row["id"])][1] > self.current_time
            )
            and (not row.get("next_attempt_at") or str(row["next_attempt_at"]) <= now)
        ]
        rows.sort(
            key=lambda row: (
                str(row.get("next_attempt_at") or ""),
                str(row.get("created_at") or ""),
                str(row["id"]),
            )
        )
        return [dict(row) for row in rows[:limit]]

    async def list_exhausted_ambiguous_digest_publications(
        self, *, attempt_limit: int, page_size: int = 100, now: str | None = None
    ) -> list[dict[str, object]]:
        now = now or iso(self.current_time)
        self.current_time = datetime.fromisoformat(now)
        self.reconciliation_queries.append({"attempt_limit": attempt_limit, "page_size": page_size})
        rows = [
            row
            for row in self.publications.values()
            if int(row["attempt_count"]) >= attempt_limit
            and row["status"] in {"pending", "failed"}
            and not row.get("digest_terminal")
            and (not row.get("next_attempt_at") or str(row["next_attempt_at"]) <= now)
            and (
                row["status"] == "pending"
                or str(row.get("last_error") or "").startswith(
                    ("discord_delivery_ambiguous:", "discord_delivery_failed")
                )
            )
        ]
        rows.sort(key=lambda row: (str(row.get("created_at") or ""), str(row["id"])))
        return [dict(row) for row in rows]

    async def get_media_item(self, media_item_id: str) -> dict[str, object] | None:
        row = self.media.get(media_item_id)
        return dict(row) if row is not None else None

    async def source_for_media(self, media_item_id: str) -> dict[str, object] | None:
        if self.fail_source:
            raise RuntimeError("source lookup failed")
        row = self.sources.get(media_item_id)
        return dict(row) if row is not None else None

    async def sources_for_media(self, media_item_id: str) -> list[dict[str, object]]:
        source = await self.source_for_media(media_item_id)
        return [source] if source else []

    async def record_publication_attempt(
        self, publication_id: str, *, expected_attempt_count: int
    ) -> dict[str, object] | None:
        row = self.publications[publication_id]
        if int(row["attempt_count"]) == expected_attempt_count:
            row["attempt_count"] = expected_attempt_count + 1
            row["status"] = "pending"
            row["last_error"] = None
            return dict(row)
        return None

    async def claim_digest_publication(
        self, publication_id: str, claim_token: str, *, lease_seconds: int = 900
    ) -> dict[str, object]:
        row = self.publications[publication_id]
        active = self._claims.get(publication_id)
        if active and active[1] > self.current_time:
            return {"claim_acquired": False}
        if row.get("digest_terminal"):
            return {"claim_acquired": False}
        if row.get("next_attempt_at") and str(row["next_attempt_at"]) > iso(self.current_time):
            return {"claim_acquired": False}
        self._claims[publication_id] = (
            claim_token,
            self.current_time + timedelta(seconds=lease_seconds),
        )
        row["delivery_claim_token"] = claim_token
        row["delivery_claim_expires_at"] = iso(self.current_time + timedelta(seconds=lease_seconds))
        return {**row, "claim_acquired": True}

    def _owns_claim(self, publication_id: str, claim_token: str) -> bool:
        active = self._claims.get(publication_id)
        return bool(active and active[0] == claim_token and active[1] > self.current_time)

    async def renew_digest_claim(
        self, publication_id: str, claim_token: str, *, lease_seconds: int = 900
    ) -> bool:
        if not self._owns_claim(publication_id, claim_token):
            return False
        self._claims[publication_id] = (
            claim_token,
            self.current_time + timedelta(seconds=lease_seconds),
        )
        return True

    async def advance_digest_attempt(
        self,
        publication_id: str,
        claim_token: str,
        digest_cycle: str,
        *,
        expected_attempt_count: int,
        attempt_limit: int,
        lease_seconds: int = 900,
    ) -> dict[str, object]:
        self.attempt_cycle_calls.append(digest_cycle)
        row = self.publications[publication_id]
        if self.advance_cycle_stale_count:
            self.advance_cycle_stale_count -= 1
            if self.on_cycle_stale is not None:
                self.on_cycle_stale()
            return {"attempt_advanced": False, "cycle_stale": True}
        if row.get("digest_cycle") != digest_cycle:
            return {"attempt_advanced": False, "cycle_stale": True}
        if (
            not self._owns_claim(publication_id, claim_token)
            or int(row["attempt_count"]) != expected_attempt_count
            or expected_attempt_count >= attempt_limit
        ):
            return {"attempt_advanced": False}
        row["attempt_count"] = expected_attempt_count + 1
        row["status"] = "pending"
        row["last_error"] = None
        return {**row, "attempt_advanced": True}

    async def retarget_digest_publication(
        self, publication_id: str, claim_token: str, channel_id: int
    ) -> dict[str, object]:
        if not self._owns_claim(publication_id, claim_token):
            return {"claim_valid": False}
        row = self.publications[publication_id]
        old_key = (str(row["media_item_id"]), int(row["channel_id"]))
        self.publication_by_key.pop(old_key, None)
        row["channel_id"] = channel_id
        self.publication_by_key[(str(row["media_item_id"]), channel_id)] = publication_id
        return {**row, "claim_valid": True}

    async def terminalize_digest_publication(
        self,
        publication_id: str,
        claim_token: str,
        error: str,
        *,
        attempt_limit: int,
        conclusive_no_delivery: bool,
    ) -> bool:
        if (
            not conclusive_no_delivery
            or not self._owns_claim(publication_id, claim_token)
            or int(self.publications[publication_id]["attempt_count"]) < attempt_limit
        ):
            return False
        row = self.publications[publication_id]
        row.update(
            status="failed",
            last_error=error,
            digest_terminal=True,
            digest_slot_reserved=False,
            next_attempt_at=None,
            delivery_claim_token=None,
            delivery_claim_expires_at=None,
        )
        self._claims.pop(publication_id, None)
        return True

    async def finish_digest_failure(
        self, publication_id: str, claim_token: str, error: str
    ) -> bool:
        if not self._owns_claim(publication_id, claim_token):
            return False
        row = self.publications[publication_id]
        row["status"] = "failed"
        row["last_error"] = error
        row["retry_schedule_count"] = int(row.get("retry_schedule_count") or 0) + 1
        row["next_attempt_at"] = iso(self.current_time + timedelta(minutes=15))
        if not error.startswith(("discord_delivery_ambiguous:", "discord_delivery_failed")):
            row["digest_slot_reserved"] = False
        self._claims.pop(publication_id, None)
        return True

    async def finish_digest_reconciliation(
        self, publication_id: str, claim_token: str, error: str
    ) -> bool:
        if not self._owns_claim(publication_id, claim_token):
            return False
        row = self.publications[publication_id]
        previous = str(row.get("last_error") or "")
        row["last_error"] = f"{previous};{error}"[:500] if previous else error[:500]
        row["next_attempt_at"] = iso(self.current_time + timedelta(minutes=15))
        row["retry_schedule_count"] = int(row.get("retry_schedule_count") or 0) + 1
        row["delivery_claim_token"] = None
        row["delivery_claim_expires_at"] = None
        self._claims.pop(publication_id, None)
        return True

    async def publish_digest_claimed(
        self, publication_id: str, claim_token: str, message_id: int
    ) -> bool:
        if self.fail_finish_published or not self._owns_claim(publication_id, claim_token):
            return False
        row = self.publications[publication_id]
        row.update(
            status="published",
            message_id=message_id,
            last_error=None,
            next_attempt_at=None,
            delivery_claim_token=None,
            delivery_claim_expires_at=None,
        )
        self._claims.pop(publication_id, None)
        return True

    async def rollover_reconciled_digest_publication(
        self, publication_id: str, claim_token: str, digest_cycle: str, *, limit: int = 50
    ) -> dict[str, object]:
        if not self._owns_claim(publication_id, claim_token):
            return {"claim_valid": False, "quota_exhausted": False}
        row = self.publications[publication_id]
        reserved = sum(
            1
            for item in self.publications.values()
            if item.get("digest_cycle") == digest_cycle
            and item.get("digest_slot_reserved")
            and item["id"] != publication_id
        )
        if (
            row.get("digest_cycle") != digest_cycle or not row.get("digest_slot_reserved")
        ) and reserved >= min(limit, self.digest_limit):
            row.update(
                status="failed",
                digest_slot_reserved=False,
                last_error="delivery_reconciled_no_marker",
            )
            self._claims.pop(publication_id, None)
            return {"claim_valid": True, "quota_exhausted": True}
        row.update(
            status="failed",
            digest_cycle=digest_cycle,
            digest_slot_reserved=True,
            last_error="delivery_reconciled_no_marker",
        )
        return {**row, "claim_valid": True, "quota_exhausted": False}

    async def mark_publication_published(
        self, publication_id: str, message_id: int
    ) -> dict[str, object]:
        if self.fail_mark_published:
            raise RuntimeError("database update failed")
        row = self.publications[publication_id]
        row["status"] = "published"
        row["message_id"] = message_id
        row["published_at"] = iso(self.current_time)
        row["last_error"] = None
        return dict(row)

    async def mark_publication_failed(self, publication_id: str, error: str) -> dict[str, object]:
        row = self.publications[publication_id]
        row["status"] = "failed"
        row["last_error"] = error
        if not error.startswith(("discord_delivery_ambiguous:", "discord_delivery_failed")):
            row["digest_slot_reserved"] = False
        return dict(row)

    async def record_publication_reconciliation_error(
        self,
        publication_id: str,
        error: str,
        *,
        preserve_error: str | None = None,
    ) -> dict[str, object]:
        row = self.publications[publication_id]
        row["last_error"] = f"{preserve_error};{error}" if preserve_error else error
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
        DISCORD_CATEGORY_CHANNELS_JSON='{"image": 222}',
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


async def test_media_type_routing_ignores_content_category(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.add_media("media-1", category="cosplay_photography")
    publisher = FakePublisher()
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        DISCORD_CATEGORY_CHANNELS_JSON='{"image": 222}',
    )

    result = await service.run()

    assert result.outcome == "succeeded"
    assert publisher.calls[0]["channel_id"] == 222


async def test_missing_destination_fails_item_without_false_publication(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.add_media("media-1", category="other")
    publisher = FakePublisher()
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        DISCORD_CATEGORY_CHANNELS_JSON="{}",
    )

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
        DISCORD_CATEGORY_CHANNELS_JSON="{}",
    )

    first = await first_service.run()

    assert first.outcome == "partial"
    assert first.published_count == 0
    assert first.failed_count == 2
    assert "last_completed_at" not in repository.state
    assert repository.state["cursor_media_created_at"] == iso(INITIAL_DIGEST_WINDOW_START)

    second_service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        DISCORD_CATEGORY_CHANNELS_JSON='{"image": 222}',
    )

    second = await second_service.run()

    assert second.resumed is True
    assert second.published_count == 2
    assert second.skipped_count == 0
    assert second.outcome == "succeeded"
    assert [call["channel_id"] for call in publisher.calls] == [222, 222]
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
    assert publication["attempt_count"] == 0
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


async def test_failed_ambiguous_delivery_stays_reconcilable_across_runs(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.add_media("media-1")
    publication = repository.add_publication(
        "media-1",
        999,
        status="failed",
        attempt_count=1,
        last_error="discord_delivery_ambiguous:TimeoutError",
    )
    publication["digest_cycle"] = "2026-01-02"
    publication["digest_slot_reserved"] = True
    publisher = FakePublisher(marker_lookup=MarkerLookup(message_id=None, conclusive=False))
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
    )

    await service.run()
    first_error = str(repository.publications[publication["id"]]["last_error"])
    repository.current_time = NOW + timedelta(minutes=16)
    service._now = lambda: NOW + timedelta(minutes=16)
    second = await service.run()

    assert first_error.startswith("discord_delivery_ambiguous:TimeoutError;")
    assert repository.publications[publication["id"]]["digest_slot_reserved"] is True
    assert str(repository.publications[publication["id"]]["last_error"]).startswith(
        "discord_delivery_ambiguous:TimeoutError;"
    )
    assert len(publisher.marker_calls) == 2
    assert publisher.calls == []
    assert second.failed_count == 1
    assert repository.publications[publication["id"]]["status"] == "failed"


async def test_legacy_ambiguous_error_still_triggers_reconciliation(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.add_media("media-1")
    publication = repository.add_publication(
        "media-1", 999, status="failed", attempt_count=1, last_error="discord_delivery_failed"
    )
    publisher = FakePublisher(marker_lookup=MarkerLookup(message_id=877, conclusive=True))
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
    )

    await service.run()

    assert publisher.calls == []
    assert len(publisher.marker_calls) == 1
    assert repository.publications[publication["id"]]["status"] == "published"
    assert repository.publications[publication["id"]]["message_id"] == 877


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

    repository.current_time = NOW + timedelta(minutes=16)
    service._now = lambda: NOW + timedelta(minutes=16)
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
    assert publication["last_error"] == "discord_delivery_rejected:DiscordPublicationRejectedError"
    assert publication["digest_slot_reserved"] is False

    repository.current_time = NOW + timedelta(minutes=16)
    service._now = lambda: NOW + timedelta(minutes=16)
    retry = await service.run()

    assert retry.published_count == 0
    assert len(publisher.calls) == 2
    assert publisher.marker_calls == []
    assert publication["attempt_count"] == 2


def _set_completed_digest_window(repository: FakeDigestRepository) -> None:
    completed_at = NOW - timedelta(hours=1)
    repository.state = {
        "id": 1,
        "last_window_start": iso(completed_at - timedelta(days=1)),
        "last_window_end": iso(completed_at),
        "last_completed_at": iso(completed_at),
        "cursor_media_created_at": iso(completed_at),
    }


async def test_large_exhausted_ambiguous_queue_is_separate_from_delivery_retries(
    make_settings,
) -> None:
    repository = FakeDigestRepository()
    for index in range(125):
        media_id = f"old-{index:03}"
        repository.add_media(media_id, created_at="2025-12-01T00:00:00+00:00")
        repository.add_publication(
            media_id,
            999,
            status="failed",
            attempt_count=3,
            last_error="discord_delivery_ambiguous:TimeoutError",
        )
    _set_completed_digest_window(repository)
    publisher = FakePublisher(marker_lookup=MarkerLookup(message_id=None, conclusive=False))
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
    )

    await service.run()

    assert repository.retry_queries[-1]["attempt_limit"] == 3
    assert repository.reconciliation_queries == [{"attempt_limit": 3, "page_size": 100}]
    assert len(publisher.marker_calls) == 125
    assert publisher.calls == []
    assert all(row["attempt_count"] == 3 for row in repository.publications.values())


async def test_exhausted_backlog_does_not_starve_newer_delivery_retries(make_settings) -> None:
    repository = FakeDigestRepository()
    for index in range(125):
        media_id = f"old-{index:03}"
        repository.add_media(media_id, created_at="2025-12-01T00:00:00+00:00")
        repository.add_publication(
            media_id,
            999,
            status="failed",
            attempt_count=3,
            last_error="discord_delivery_ambiguous:TimeoutError",
        )
    retry_media_ids = ["new-retry-a", "new-retry-b", "new-retry-c"]
    for media_id in retry_media_ids:
        repository.add_media(media_id, created_at="2025-12-02T00:00:00+00:00")
        repository.add_publication(
            media_id,
            999,
            status="failed",
            attempt_count=1,
            last_error="storage_download_failed:TimeoutError",
        )
    _set_completed_digest_window(repository)
    publisher = FakePublisher(marker_lookup=MarkerLookup(message_id=None, conclusive=False))
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
    )

    result = await service.run()

    assert result.published_count == 3
    assert {str(call["filename"]) for call in publisher.calls} == {
        f"{media_id}.png" for media_id in retry_media_ids
    }
    assert len(publisher.marker_calls) == 125
    assert all(
        repository.publications[repository.publication_by_key[(media_id, 999)]]["attempt_count"]
        == 2
        for media_id in retry_media_ids
    )


async def test_large_reconciliation_queue_rechecks_records_beyond_first_page(make_settings) -> None:
    repository = FakeDigestRepository()
    for index in range(225):
        media_id = f"old-{index:03}"
        repository.add_media(media_id, created_at="2025-12-01T00:00:00+00:00")
        repository.add_publication(
            media_id,
            999,
            status="failed",
            attempt_count=3,
            last_error="discord_delivery_ambiguous:TimeoutError",
        )
    _set_completed_digest_window(repository)
    publisher = FakePublisher(marker_lookup=MarkerLookup(message_id=None, conclusive=False))
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
    )

    await service.run()
    repository.current_time = NOW + timedelta(minutes=16)
    service._now = lambda: NOW + timedelta(minutes=16)
    await service.run()

    assert len(publisher.marker_calls) == 450
    markers = [str(call["marker"]) for call in publisher.marker_calls]
    assert len(set(markers[:225])) == 225
    assert set(markers[:225]) == set(markers[225:])
    assert publisher.calls == []


async def test_exhausted_ambiguous_publication_is_reconciled_and_recovered(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.add_media("media-1", created_at="2025-12-01T00:00:00+00:00")
    publication = repository.add_publication(
        "media-1",
        999,
        status="failed",
        attempt_count=3,
        last_error="discord_delivery_ambiguous:TimeoutError",
    )
    _set_completed_digest_window(repository)
    publisher = FakePublisher(marker_lookup=MarkerLookup(message_id=4545, conclusive=True))
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
    )

    result = await service.run()

    assert result.published_count == 0
    assert len(publisher.marker_calls) == 1
    assert publisher.calls == []
    assert repository.retry_queries[-1]["attempt_limit"] == 3
    assert repository.publications[publication["id"]]["status"] == "published"
    assert repository.publications[publication["id"]]["message_id"] == 4545
    assert repository.publications[publication["id"]]["attempt_count"] == 3


async def test_exhausted_ambiguous_publication_remains_reconcilable_without_fourth_send(
    make_settings,
) -> None:
    repository = FakeDigestRepository()
    repository.add_media("media-1", created_at="2025-12-01T00:00:00+00:00")
    publication = repository.add_publication(
        "media-1",
        999,
        status="failed",
        attempt_count=3,
        last_error="discord_delivery_ambiguous:TimeoutError",
    )
    _set_completed_digest_window(repository)
    publisher = FakePublisher(marker_lookup=MarkerLookup(message_id=None, conclusive=False))
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
    )

    await service.run()
    repository.current_time = NOW + timedelta(minutes=16)
    service._now = lambda: NOW + timedelta(minutes=16)
    await service.run()

    assert len(publisher.marker_calls) == 2
    assert publisher.calls == []
    assert repository.publications[publication["id"]]["attempt_count"] == 3
    assert repository.publications[publication["id"]]["status"] == "failed"
    assert str(repository.publications[publication["id"]]["last_error"]).startswith(
        "discord_delivery_ambiguous:TimeoutError;"
    )


async def test_exhausted_ambiguous_missing_marker_does_not_send_again(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.add_media("media-1", created_at="2025-12-01T00:00:00+00:00")
    publication = repository.add_publication(
        "media-1",
        999,
        status="failed",
        attempt_count=3,
        last_error="discord_delivery_ambiguous:TimeoutError",
    )
    _set_completed_digest_window(repository)
    publisher = FakePublisher(marker_lookup=MarkerLookup(message_id=None, conclusive=True))
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
    )

    await service.run()

    assert len(publisher.marker_calls) == 1
    assert publisher.calls == []
    assert repository.publications[publication["id"]]["attempt_count"] == 3


async def test_database_update_after_send_is_recovered_by_marker(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.add_media("media-1")
    repository.fail_finish_published = True
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
    assert "publication_claim_lost_after_delivery" in (first.error_summary or "")
    publication = repository.publications["publication-1"]
    assert publication["status"] == "pending"
    assert publication["attempt_count"] == 1
    assert len(publisher.calls) == 1

    repository.fail_finish_published = False
    repository.current_time = NOW + timedelta(minutes=16)
    service._now = lambda: NOW + timedelta(minutes=16)
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


async def test_changed_destination_reconciles_original_marker_before_routing(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.add_media("media-1", category="fanart", created_at="2025-12-01T00:00:00+00:00")
    existing = repository.add_publication(
        "media-1",
        999,
        status="failed",
        attempt_count=1,
        last_error="discord_delivery_ambiguous:TimeoutError",
    )
    existing.update(digest_cycle="2026-01-02", digest_slot_reserved=True)
    repository.state = {
        "id": 1,
        "last_window_start": iso(NOW - timedelta(days=1)),
        "last_window_end": iso(NOW - timedelta(hours=1)),
        "last_completed_at": iso(NOW - timedelta(hours=1)),
        "cursor_media_created_at": iso(NOW - timedelta(hours=1)),
    }
    publisher = FakePublisher(marker_lookup=MarkerLookup(message_id=8181, conclusive=True))
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
        DISCORD_CATEGORY_CHANNELS_JSON='{"image": 222}',
    )

    result = await service.run()

    assert result.published_count == 0
    assert result.skipped_count == 1
    assert publisher.calls == []
    assert publisher.marker_calls == [
        {"channel_id": 999, "marker": publication_marker(str(existing["id"]))}
    ]
    assert repository.publications[existing["id"]]["attempt_count"] == 1
    assert repository.publications[existing["id"]]["status"] == "published"
    assert repository.publications[existing["id"]]["channel_id"] == 999
    assert repository.publications[existing["id"]].get("delivery_claim_token") is None


async def test_changed_destination_conclusive_absence_retargets_under_claim(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.add_media("media-1", category="fanart", created_at="2025-12-01T00:00:00+00:00")
    existing = repository.add_publication(
        "media-1",
        999,
        status="failed",
        attempt_count=1,
        last_error="discord_delivery_ambiguous:TimeoutError",
    )
    existing.update(digest_cycle="2026-01-02", digest_slot_reserved=True)
    repository.state = {
        "id": 1,
        "last_window_start": iso(NOW - timedelta(days=1)),
        "last_window_end": iso(NOW - timedelta(hours=1)),
        "last_completed_at": iso(NOW - timedelta(hours=1)),
        "cursor_media_created_at": iso(NOW - timedelta(hours=1)),
    }
    publisher = FakePublisher(marker_lookup=MarkerLookup(message_id=None, conclusive=True))
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        DISCORD_CATEGORY_CHANNELS_JSON='{"image": 222}',
    )

    result = await service.run()

    assert result.published_count == 1
    assert publisher.marker_calls[0]["channel_id"] == 999
    assert [call["channel_id"] for call in publisher.calls] == [222]
    assert repository.publications[existing["id"]]["channel_id"] == 222
    assert repository.publications[existing["id"]]["attempt_count"] == 2
    assert repository.publications[existing["id"]]["status"] == "published"
    assert repository.publications[existing["id"]].get("delivery_claim_token") is None


async def test_changed_destination_inconclusive_reconciliation_is_scheduled(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.add_media("media-1", category="fanart", created_at="2025-12-01T00:00:00+00:00")
    existing = repository.add_publication(
        "media-1",
        999,
        status="failed",
        attempt_count=1,
        last_error="discord_delivery_ambiguous:TimeoutError",
    )
    existing.update(digest_cycle="2026-01-02", digest_slot_reserved=True)
    repository.state = {
        "id": 1,
        "last_window_start": iso(NOW - timedelta(days=1)),
        "last_window_end": iso(NOW - timedelta(hours=1)),
        "last_completed_at": iso(NOW - timedelta(hours=1)),
        "cursor_media_created_at": iso(NOW - timedelta(hours=1)),
    }
    publisher = FakePublisher(marker_lookup=PermissionError("history inaccessible"))
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        DISCORD_CATEGORY_CHANNELS_JSON='{"image": 222}',
    )

    await service.run()

    row = repository.publications[existing["id"]]
    assert publisher.marker_calls[0]["channel_id"] == 999
    assert publisher.calls == []
    assert row["channel_id"] == 999
    assert row["status"] == "failed"
    assert row.get("delivery_claim_token") is None
    assert datetime.fromisoformat(str(row["next_attempt_at"])) > NOW
    assert existing["id"] not in {
        row["id"]
        for row in await repository.list_retryable_digest_publications(
            attempt_limit=3, limit=100, now=iso(NOW)
        )
    }


async def test_changed_destination_retarget_rejects_stale_claim(make_settings) -> None:
    repository = FakeDigestRepository()
    row = repository.add_publication("media-1", 999, status="failed", attempt_count=1)
    acquired = await repository.claim_digest_publication(row["id"], "current-owner")
    assert acquired["claim_acquired"] is True

    repository._claims[row["id"]] = ("replacement-owner", NOW + timedelta(minutes=15))
    row["delivery_claim_token"] = "replacement-owner"
    retargeted = await repository.retarget_digest_publication(row["id"], "current-owner", 222)

    assert retargeted["claim_valid"] is False
    assert row["channel_id"] == 999
    assert row["delivery_claim_token"] == "replacement-owner"


async def test_published_history_is_not_reposted_after_routing_change(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.add_media("media-1", category="fanart")
    existing = repository.add_publication("media-1", 999, status="published")
    publisher = FakePublisher()
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        DISCORD_CATEGORY_CHANNELS_JSON=(
            '{"image": 222, "animation": 333, "video": 444, "dumpster": 555}'
        ),
    )

    result = await service.run()

    assert result.published_count == 0
    assert publisher.calls == []
    assert len(repository.publications) == 1
    assert repository.publications[existing["id"]]["channel_id"] == 999


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


async def test_source_lookup_failure_fails_closed_and_can_retry(make_settings) -> None:
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

    publication = next(iter(repository.publications.values()))
    assert result.outcome == "partial"
    assert publisher.calls == []
    assert publication["status"] == "failed"
    assert publication["attempt_count"] == 0
    assert str(publication["last_error"]).startswith("attribution_lookup_failed:")
    assert publication["next_attempt_at"]

    repository.fail_source = False
    repository.current_time = NOW + timedelta(minutes=16)
    retry_publisher = FakePublisher()
    retry = DigestService(
        settings=service._settings,
        repository=repository,
        storage=FakeStorage(),
        publisher=retry_publisher,
        now=lambda: NOW + timedelta(minutes=16),
    )
    retry_result = await retry.run()
    assert retry_result.published_count == 1
    assert retry_publisher.calls[0]["context"].source_url == "https://example.com/post"
    assert repository.publications[publication["id"]]["status"] == "published"


async def test_retry_schedule_prevents_unresolved_rows_from_starving_later_rows() -> None:
    repository = FakeDigestRepository()
    rows = [
        repository.add_publication(
            f"media-{index:03}",
            999,
            status="pending",
            attempt_count=1,
            last_error="discord_delivery_ambiguous:TimeoutError",
        )
        for index in range(150)
    ]

    first_run = await repository.list_retryable_digest_publications(
        attempt_limit=3, limit=100, now=iso(NOW)
    )
    expected_ids = sorted(str(row["id"]) for row in rows)
    assert [row["id"] for row in first_run] == expected_ids[:100]
    for row in first_run:
        repository.publications[str(row["id"])]["next_attempt_at"] = iso(NOW + timedelta(hours=1))

    second_run = await repository.list_retryable_digest_publications(
        attempt_limit=3, limit=100, now=iso(NOW)
    )
    assert [row["id"] for row in second_run] == expected_ids[100:]

    later = NOW + timedelta(hours=2)
    for row in second_run:
        repository.publications[str(row["id"])]["next_attempt_at"] = iso(later + timedelta(hours=1))
    third_run = await repository.list_retryable_digest_publications(
        attempt_limit=3, limit=100, now=iso(later)
    )
    assert [row["id"] for row in third_run] == expected_ids[:100]
    assert len(repository.publications) == 150
    assert all(row["status"] == "pending" for row in repository.publications.values())


async def test_unattempted_old_cycle_reservations_must_reacquire_current_quota() -> None:
    repository = FakeDigestRepository()
    old = repository.add_publication("old-media", 999, attempt_count=0)
    old.update(digest_cycle="2026-01-01", digest_slot_reserved=True)
    for index in range(50):
        row = repository.add_publication(f"current-{index}", 999)
        row.update(digest_cycle="2026-01-02", digest_slot_reserved=True)

    result = await repository.reserve_digest_publication("old-media", 999, "2026-01-02", limit=50)
    assert result.get("quota_exhausted") is True
    assert old["digest_cycle"] == "2026-01-01"
    assert old["attempt_count"] == 0
    assert (
        sum(
            row.get("digest_cycle") == "2026-01-02" and row.get("digest_slot_reserved")
            for row in repository.publications.values()
        )
        == 50
    )

    current_id = repository.publication_by_key[("current-0", 999)]
    repository.publications[current_id]["digest_slot_reserved"] = False
    transferred = await repository.reserve_digest_publication(
        "old-media", 999, "2026-01-02", limit=50
    )
    duplicate = await repository.reserve_digest_publication(
        "old-media", 999, "2026-01-02", limit=50
    )
    assert transferred["digest_cycle"] == "2026-01-02"
    assert duplicate["id"] == transferred["id"]
    assert (
        sum(
            row.get("digest_cycle") == "2026-01-02" and row.get("digest_slot_reserved")
            for row in repository.publications.values()
        )
        == 50
    )


async def test_attempted_ambiguous_old_cycle_reservation_keeps_reconciliation_context() -> None:
    repository = FakeDigestRepository()
    old = repository.add_publication(
        "uncertain-media",
        999,
        attempt_count=1,
        last_error="discord_delivery_ambiguous:TimeoutError",
    )
    old.update(digest_cycle="2026-01-01", digest_slot_reserved=True)

    result = await repository.reserve_digest_publication(
        "uncertain-media", 999, "2026-01-02", limit=50
    )

    assert result["requires_reconciliation"] is True
    assert result["digest_cycle"] == "2026-01-01"
    assert result["last_error"] == "discord_delivery_ambiguous:TimeoutError"
    assert result["attempt_count"] == 1


async def test_terminal_exhaustion_releases_claim_and_prevents_future_reservation(
    make_settings,
) -> None:
    repository = FakeDigestRepository()
    repository.add_media("old-media", created_at="2025-12-01T00:00:00+00:00")
    publication = repository.add_publication(
        "old-media",
        999,
        status="failed",
        attempt_count=3,
        last_error="discord_delivery_ambiguous:TimeoutError",
    )
    publication.update(digest_cycle="2026-01-01", digest_slot_reserved=True)
    _set_completed_digest_window(repository)
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        FakePublisher(marker_lookup=MarkerLookup(message_id=None, conclusive=True)),
        DISCORD_DISCOVERIES_CHANNEL_ID="999",
    )

    result = await service.run()
    terminal = repository.publications[publication["id"]]
    assert result.failed_count == 0
    assert terminal["digest_terminal"] is True
    assert terminal["digest_slot_reserved"] is False
    assert terminal["delivery_claim_token"] is None
    assert terminal["delivery_claim_expires_at"] is None

    later = await repository.reserve_digest_publication("old-media", 999, "2026-01-02", limit=50)
    assert later["terminal"] is True
    assert later["id"] == publication["id"]
    assert later["digest_cycle"] == "2026-01-01"


async def test_terminal_exhaustion_rejects_invalid_claim_token() -> None:
    repository = FakeDigestRepository()
    publication = repository.add_publication(
        "media-1",
        999,
        status="failed",
        attempt_count=3,
        last_error="discord_delivery_ambiguous:TimeoutError",
    )
    publication["digest_slot_reserved"] = True
    claim = await repository.claim_digest_publication(publication["id"], "valid-token")
    assert claim["claim_acquired"] is True

    assert (
        await repository.terminalize_digest_publication(
            publication["id"],
            "wrong-token",
            "delivery_attempt_limit_reached:no_marker_found",
            attempt_limit=3,
            conclusive_no_delivery=True,
        )
        is False
    )
    assert publication["digest_terminal"] is False
    assert publication["digest_slot_reserved"] is True


async def test_active_old_cycle_never_attempted_claim_is_revoked_and_reacquires_quota() -> None:
    repository = FakeDigestRepository()
    publication = repository.add_publication("media-1", 999, attempt_count=0)
    publication.update(digest_cycle="2026-01-01", digest_slot_reserved=True)
    claimed = await repository.claim_digest_publication(publication["id"], "old-worker-token")
    assert claimed["claim_acquired"] is True

    moved = await repository.reserve_digest_publication("media-1", 999, "2026-01-02", limit=50)
    assert moved["digest_cycle"] == "2026-01-02"
    assert moved["digest_slot_reserved"] is True
    assert moved["delivery_claim_token"] is None
    assert publication["attempt_count"] == 0
    assert (
        await repository.publish_digest_claimed(publication["id"], "old-worker-token", 12345)
        is False
    )
    assert (
        await repository.finish_digest_failure(
            publication["id"], "old-worker-token", "stale-worker-error"
        )
        is False
    )
    assert publication["status"] == "pending"


async def test_startup_catchup_crossing_0400_refreshes_cycle_and_delivers_once(
    make_settings,
) -> None:
    before_boundary = datetime(2026, 1, 1, 22, 29, tzinfo=UTC)
    after_boundary = datetime(2026, 1, 1, 22, 31, tzinfo=UTC)
    clock = [before_boundary]
    repository = FakeDigestRepository()
    repository.current_time = before_boundary
    repository.validate_digest_cycles = True
    repository.advance_cycle_stale_count = 1

    def cross_boundary() -> None:
        clock[0] = after_boundary
        repository.current_time = after_boundary

    repository.on_cycle_stale = cross_boundary
    repository.add_media("catchup-media", created_at="2026-01-01T21:00:00+00:00")
    publisher = FakePublisher()
    service = DigestService(
        settings=make_settings(DISCORD_DISCOVERIES_CHANNEL_ID="999"),
        repository=repository,
        storage=FakeStorage(),
        publisher=publisher,
        now=lambda: clock[0],
    )

    result = await service.run(trigger="startup")

    publication = next(iter(repository.publications.values()))
    assert result.published_count == 1
    assert [call[2] for call in repository.reservation_calls] == ["2026-01-01", "2026-01-02"]
    assert repository.attempt_cycle_calls == ["2026-01-01", "2026-01-02"]
    assert publication["digest_cycle"] == "2026-01-02"
    assert publication["digest_slot_reserved"] is True
    assert publication["status"] == "published"
    assert len(publisher.calls) == 1


async def test_retry_crossing_0400_is_rescheduled_for_the_current_cycle(make_settings) -> None:
    before_boundary = datetime(2026, 1, 1, 22, 29, tzinfo=UTC)
    after_boundary = datetime(2026, 1, 1, 22, 31, tzinfo=UTC)
    clock = [before_boundary]
    repository = FakeDigestRepository()
    repository.current_time = before_boundary
    repository.validate_digest_cycles = True
    repository.advance_cycle_stale_count = 1

    def cross_boundary() -> None:
        clock[0] = after_boundary
        repository.current_time = after_boundary

    repository.on_cycle_stale = cross_boundary
    repository.add_media("retry-media", created_at="2025-12-01T00:00:00+00:00")
    publication = repository.add_publication(
        "retry-media",
        999,
        status="failed",
        attempt_count=1,
        last_error="storage_download_failed:TimeoutError",
    )
    publication.update(digest_cycle="2026-01-01", digest_slot_reserved=True)
    repository.state = {
        "id": 1,
        "last_window_start": iso(before_boundary - timedelta(days=1)),
        "last_window_end": iso(before_boundary - timedelta(hours=1)),
        "last_completed_at": iso(before_boundary - timedelta(hours=1)),
        "cursor_media_created_at": iso(before_boundary - timedelta(hours=1)),
        "digest_cycle": "2026-01-01",
    }
    publisher = FakePublisher()
    service = DigestService(
        settings=make_settings(DISCORD_DISCOVERIES_CHANNEL_ID="999"),
        repository=repository,
        storage=FakeStorage(),
        publisher=publisher,
        now=lambda: clock[0],
    )

    first_result = await service.run(trigger="startup")
    assert first_result.outcome == "partial"
    assert publisher.calls == []
    assert publication["delivery_claim_token"] is None
    assert datetime.fromisoformat(str(publication["next_attempt_at"])) > after_boundary

    clock[0] = after_boundary + timedelta(minutes=16)
    repository.current_time = clock[0]
    second_result = await service.run(trigger="scheduled")

    assert second_result.published_count == 1
    assert publication["digest_cycle"] == "2026-01-02"
    assert publication["status"] == "published"
    assert len(publisher.calls) == 1


async def test_cycle_refresh_retries_reservation_once_then_stops(make_settings) -> None:
    repository = FakeDigestRepository()
    repository.stale_reservation_count = 3
    service = build_service(make_settings, repository, FakeStorage(), FakePublisher())

    result, cycle = await service._reserve_with_cycle_refresh("media-1", 999, "2026-01-01")

    assert result == {"cycle_stale": True}
    assert cycle == digest_cycle_at(NOW)
    assert len(repository.reservation_calls) == 2


async def test_retry_selector_skips_active_claims_but_recovers_expired_ones() -> None:
    repository = FakeDigestRepository()
    rows = [repository.add_publication(f"media-{index:03}", 999) for index in range(110)]
    for row in rows[:105]:
        assert (await repository.claim_digest_publication(row["id"], f"token-{row['id']}"))[
            "claim_acquired"
        ]

    eligible = await repository.list_retryable_digest_publications(
        attempt_limit=3, limit=100, now=iso(NOW)
    )
    assert [row["id"] for row in eligible] == [row["id"] for row in rows[105:]]

    expired_id = str(rows[0]["id"])
    repository._claims[expired_id] = (f"token-{expired_id}", NOW - timedelta(seconds=1))
    reclaimed = await repository.claim_digest_publication(expired_id, "replacement-token")
    assert reclaimed["claim_acquired"] is True


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


async def test_global_digest_budget_stops_after_fifty_unique_items(make_settings) -> None:
    repository = FakeDigestRepository()
    for index in range(60):
        row = repository.add_media(
            f"media-{index:03}",
            created_at=iso(NOW - timedelta(days=60 - index)),
        )
        row["media_type"] = ("image", "animation", "video")[index % 3]
    publisher = FakePublisher()
    routes = '{"image": 111, "animation": 222, "video": 333, "dumpster": 444}'
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        page_size=17,
        DISCORD_CATEGORY_CHANNELS_JSON=routes,
    )

    result = await service.run()

    assert result.outcome == "partial"
    assert result.published_count == 50
    assert len(publisher.calls) == 50
    assert sum(row["status"] == "published" for row in repository.publications.values()) == 50
    assert len({row["media_item_id"] for row in repository.publications.values()}) == 50
    assert {row["media_item_id"] for row in repository.publications.values()} == {
        f"media-{index:03}" for index in range(50)
    }
    assert {row["digest_cycle"] for row in repository.publications.values()} == {"2026-01-02"}
    assert "global_daily_limit_reached" in (result.error_summary or "")
    assert "last_completed_at" not in repository.state


async def test_exactly_fifty_single_category_completes_the_cycle(make_settings) -> None:
    repository = FakeDigestRepository()
    for index in range(50):
        repository.add_media(f"image-{index:02}")
    publisher = FakePublisher()
    service = build_service(make_settings, repository, FakeStorage(), publisher)

    result = await service.run()

    assert result.outcome == "succeeded"
    assert result.published_count == 50
    assert len({call["channel_id"] for call in publisher.calls}) == 1
    assert repository.state["last_completed_at"] == iso(NOW)


async def test_unknown_and_dumpster_types_remain_unpublished(make_settings) -> None:
    repository = FakeDigestRepository()
    ordinary = repository.add_media("unknown")
    ordinary["media_type"] = "unknown"
    dumpster = repository.add_media("misc")
    dumpster["media_type"] = "other"
    dumpster["verification_status"] = "dumpster"
    publisher = FakePublisher()
    service = build_service(make_settings, repository, FakeStorage(), publisher)

    result = await service.run()

    assert result.published_count == 0
    assert publisher.calls == []
    assert repository.publications == {}


async def test_digest_backlog_carries_forward_and_cycle_resets_once(make_settings) -> None:
    repository = FakeDigestRepository()
    for index in range(55):
        repository.add_media(
            f"media-{index:03}",
            created_at="2026-01-01T00:00:00+00:00",
        )
    settings = make_settings(
        DISCORD_CATEGORY_CHANNELS_JSON=(
            '{"image": 111, "animation": 222, "video": 333, "dumpster": 444}'
        )
    )
    first_publisher = FakePublisher()
    first = DigestService(
        settings=settings,
        repository=repository,
        storage=FakeStorage(),
        publisher=first_publisher,
        now=lambda: NOW,
    )
    first_result = await first.run()

    assert first_result.published_count == 50
    assert [call["context"].title for call in first_publisher.calls][-1] == "Archived artwork"
    assert repository.state.get("last_completed_at") is None

    # Restarting in the same scheduled cycle reuses the persisted reservations.
    same_cycle_publisher = FakePublisher()
    same_cycle = DigestService(
        settings=settings,
        repository=repository,
        storage=FakeStorage(),
        publisher=same_cycle_publisher,
        now=lambda: NOW + timedelta(hours=1),
    )
    same_result = await same_cycle.run()
    assert same_result.published_count == 0
    assert len(same_cycle_publisher.calls) == 0

    # The next 04:00 IST cycle resumes the oldest remaining item.
    next_day_publisher = FakePublisher()
    next_day = DigestService(
        settings=settings,
        repository=repository,
        storage=FakeStorage(),
        publisher=next_day_publisher,
        now=lambda: NOW + timedelta(days=1),
    )
    next_result = await next_day.run()
    assert next_result.published_count == 5
    assert len(next_day_publisher.calls) == 5
    assert {row["digest_cycle"] for row in repository.publications.values()} == {
        "2026-01-02",
        "2026-01-03",
    }


async def test_media_type_routes_to_one_dedicated_channel(make_settings) -> None:
    repository = FakeDigestRepository()
    expected = {"image": 111, "animation": 222, "video": 333}
    for index, media_type in enumerate(expected):
        row = repository.add_media(f"media-{index}")
        row["media_type"] = media_type
    publisher = FakePublisher()
    service = build_service(
        make_settings,
        repository,
        FakeStorage(),
        publisher,
        DISCORD_CATEGORY_CHANNELS_JSON=(
            '{"image": 111, "animation": 222, "video": 333, "dumpster": 444}'
        ),
    )

    result = await service.run()

    assert result.published_count == 3
    assert {
        row["media_item_id"]: row["channel_id"] for row in repository.publications.values()
    } == {"media-0": 111, "media-1": 222, "media-2": 333}


async def test_retry_uses_its_existing_slot_and_blocks_a_new_item(make_settings) -> None:
    repository = FakeDigestRepository()
    cycle = "2026-01-02"
    for index in range(49):
        media_id = f"published-{index:02}"
        repository.add_media(media_id)
        row = repository.add_publication(media_id, 999, status="published")
        row.update(digest_cycle=cycle, digest_slot_reserved=True)
    retry_media = repository.add_media("retry")
    retry_media["created_at"] = "2026-01-01T00:00:01+00:00"
    retry = repository.add_publication(
        "retry", 999, status="failed", attempt_count=1, last_error="storage_download_failed"
    )
    retry.update(digest_cycle=cycle, digest_slot_reserved=True)
    fresh = repository.add_media("fresh")
    fresh["created_at"] = "2026-01-01T00:00:02+00:00"
    publisher = FakePublisher()
    service = build_service(make_settings, repository, FakeStorage(), publisher)

    result = await service.run()

    assert result.published_count == 1
    assert [call["context"].title for call in publisher.calls] == ["Archived artwork"]
    assert retry["status"] == "published"
    assert retry["digest_slot_reserved"] is True
    assert "fresh" not in {row["media_item_id"] for row in repository.publications.values()}
