from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from app.media.fetcher import DownloadedMedia, OversizeMediaError, UnsupportedMediaError
from app.media.validation import MediaInfo, build_object_key
from app.pipeline.phase2 import Phase2Pipeline, Phase2Status
from app.reddit.connector import (
    RedditAccessNotApprovedError,
    RedditConfigurationError,
)
from app.reddit.models import RedditCandidate

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"phase-two-pipeline"
PNG_SHA256 = hashlib.sha256(PNG_BYTES).hexdigest()
PNG_MEDIA = MediaInfo("png", "image/png", "image")


def make_candidate(**overrides: object) -> RedditCandidate:
    values = {
        "submission_id": "abc123",
        "permalink": "https://www.reddit.com/r/Tifa/comments/abc123/a_post",
        "source_url": "https://www.reddit.com/r/Tifa/comments/abc123/a_post",
        "title": "Tifa at the bar",
        "body": "Original artwork",
        "subreddit": "Tifa",
        "author_name": "artist_name",
        "author_profile_url": "https://www.reddit.com/user/artist_name",
        "created_at": datetime(2024, 1, 1, tzinfo=UTC),
        "media_url": "https://i.redd.it/example.png",
    }
    values.update(overrides)
    return RedditCandidate(**values)


class FakeConnector:
    def __init__(self, candidate=None, error: Exception | None = None) -> None:
        self.candidate = candidate or make_candidate()
        self.error = error
        self.calls: list[str] = []

    async def fetch_candidate(self, reference: str) -> RedditCandidate:
        self.calls.append(reference)
        if self.error is not None:
            raise self.error
        return self.candidate


class FakeFetcher:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, int]] = []

    async def download(
        self,
        url: str,
        *,
        destination_dir: Path,
        max_bytes: int,
    ) -> DownloadedMedia:
        self.calls.append((url, max_bytes))
        if self.error is not None:
            raise self.error
        destination_dir.mkdir(parents=True, exist_ok=True)
        path = destination_dir / "media.png"
        path.write_bytes(PNG_BYTES)
        return DownloadedMedia(
            path=path,
            sha256=PNG_SHA256,
            size_bytes=len(PNG_BYTES),
            media=PNG_MEDIA,
            source_url=url,
            declared_content_type="image/png",
        )


class FakeRepository:
    def __init__(
        self,
        *,
        existing: dict[str, object] | None = None,
        fail_lookup: bool = False,
        fail_insert: bool = False,
        fail_source: bool = False,
        fail_publication: bool = False,
        fail_update: bool = False,
    ) -> None:
        self.existing = existing
        self.fail_lookup = fail_lookup
        self.fail_insert = fail_insert
        self.fail_source = fail_source
        self.fail_publication = fail_publication
        self.fail_update = fail_update
        self.lookups: list[str] = []
        self.discoveries: list[RedditCandidate] = []
        self.created_media: list[dict[str, object]] = []
        self.sources: list[tuple[str, RedditCandidate]] = []
        self.publications: list[dict[str, object]] = []
        self.updates: list[dict[str, object]] = []

    async def get_media_by_sha256(self, sha256: str) -> dict[str, object] | None:
        self.lookups.append(sha256)
        if self.fail_lookup:
            raise RuntimeError("lookup failed")
        return self.existing

    async def upsert_discovery(self, candidate: RedditCandidate) -> dict[str, object]:
        self.discoveries.append(candidate)
        return {"id": "discovery-1"}

    async def create_media_item(self, payload: dict[str, object]) -> dict[str, object]:
        if self.fail_insert:
            raise RuntimeError("insert failed")
        self.created_media.append(payload)
        return {"id": "media-1", **payload}

    async def record_source(
        self,
        media_item_id: str,
        candidate: RedditCandidate,
        *,
        discovery_id: str | None = None,
        media_url: str | None = None,
    ) -> dict[str, object]:
        if self.fail_source:
            raise RuntimeError("source failed")
        self.sources.append((media_item_id, candidate))
        return {"id": "source-1", "discovery_id": discovery_id, "media_url": media_url}

    async def create_publication(
        self,
        media_item_id: str,
        channel_id: int,
        purpose: str,
    ) -> dict[str, object]:
        if self.fail_publication:
            raise RuntimeError("publication insert failed")
        payload = {
            "media_item_id": media_item_id,
            "channel_id": channel_id,
            "purpose": purpose,
        }
        self.publications.append(payload)
        return {"id": "publication-1", **payload}

    async def update_publication(
        self,
        publication_id: str,
        *,
        status: str,
        message_id: int | None = None,
        last_error: str | None = None,
    ) -> dict[str, object]:
        if self.fail_update:
            raise RuntimeError("update failed")
        payload = {
            "id": publication_id,
            "status": status,
            "message_id": message_id,
            "last_error": last_error,
        }
        self.updates.append(payload)
        return payload


class FakeStorage:
    bucket_name = "private-bucket"
    configured = True

    def __init__(
        self,
        *,
        fail_upload: bool = False,
        fail_download: bool = False,
    ) -> None:
        self.fail_upload = fail_upload
        self.fail_download = fail_download
        self.uploads: list[tuple[str, bytes, str]] = []
        self.downloads: list[str] = []
        self.deleted: list[str] = []

    async def verify_object(
        self, object_key: str, *, sha256: str, size_bytes: int, timeout: float | None = None
    ) -> bool:
        del timeout
        import hashlib

        return any(
            key == object_key
            and len(data) == size_bytes
            and hashlib.sha256(data).hexdigest() == sha256
            for key, data, _content_type in self.uploads
        )

    async def upload_bytes(
        self,
        object_key: str,
        payload: bytes,
        content_type: str,
        *,
        timeout: float | None = None,
    ) -> None:
        del timeout
        if self.fail_upload:
            raise RuntimeError("upload failed")
        self.uploads.append((object_key, payload, content_type))

    async def download_bytes(
        self,
        object_key: str,
        *,
        max_bytes: int | None = None,
        timeout: float | None = None,
    ) -> bytes:
        del max_bytes, timeout
        if self.fail_download:
            raise RuntimeError("download failed")
        self.downloads.append(object_key)
        return PNG_BYTES

    async def delete_object(self, object_key: str, *, timeout: float | None = None) -> None:
        del timeout
        self.deleted.append(object_key)


class FakePublisher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, object]] = []

    async def publish(
        self,
        *,
        filename: str,
        data: bytes,
        candidate: RedditCandidate,
    ) -> int:
        self.calls.append({"filename": filename, "data": data, "candidate": candidate})
        if self.fail:
            raise RuntimeError("discord delivery failed")
        return 555


def build_pipeline(
    make_settings,
    tmp_path: Path,
    *,
    connector: FakeConnector | None = None,
    fetcher: FakeFetcher | None = None,
    repository: FakeRepository | None = None,
    storage: FakeStorage | None = None,
    publisher: FakePublisher | None = None,
    channel_id: str | None = "999",
    saves_channel_id: str | None = None,
    publication_purpose: str = "test_discovery",
) -> tuple[Phase2Pipeline, FakeConnector, FakeFetcher, FakeRepository, FakeStorage, FakePublisher]:
    connector = connector or FakeConnector()
    fetcher = fetcher or FakeFetcher()
    repository = repository or FakeRepository()
    storage = storage or FakeStorage()
    publisher = publisher or FakePublisher()
    settings = make_settings(
        DISCORD_DISCOVERIES_CHANNEL_ID=channel_id,
        DISCORD_SAVES_CHANNEL_ID=saves_channel_id,
    )
    pipeline = Phase2Pipeline(
        settings=settings,
        connector=connector,
        fetcher=fetcher,
        repository=repository,
        storage=storage,
        publisher=publisher,
        publication_purpose=publication_purpose,
        temp_dir_factory=lambda: str(tmp_path / "work"),
    )
    return pipeline, connector, fetcher, repository, storage, publisher


async def test_new_asset_uses_exact_key_and_is_published(make_settings, tmp_path) -> None:
    pipeline, connector, _fetcher, repository, storage, publisher = build_pipeline(
        make_settings, tmp_path
    )

    result = await pipeline.run("abc123")

    expected_key = build_object_key(PNG_SHA256, "png")
    assert result.status is Phase2Status.PUBLISHED
    assert result.reason == "published"
    assert result.object_key == expected_key
    assert result.media_item_id == "media-1"
    assert result.message_id == 555
    assert connector.calls == ["abc123"]
    assert storage.uploads == [(expected_key, PNG_BYTES, "image/png")]
    assert storage.downloads == [expected_key]
    assert repository.created_media[0]["sha256"] == PNG_SHA256
    assert repository.created_media[0]["download_status"] == "stored"
    assert repository.created_media[0]["storage_bucket"] == "private-bucket"
    assert repository.created_media[0]["storage_object_key"] == expected_key
    assert repository.sources[0][0] == "media-1"
    assert publisher.calls[0]["data"] == PNG_BYTES
    assert publisher.calls[0]["filename"] == "reddit-abc123.png"
    assert repository.updates[-1]["status"] == "published"
    assert repository.updates[-1]["message_id"] == 555
    assert not (tmp_path / "work").exists()


async def test_duplicate_records_new_source_without_upload_or_publication(
    make_settings, tmp_path
) -> None:
    repository = FakeRepository(existing={"id": "existing-media", "sha256": PNG_SHA256})
    pipeline, _connector, _fetcher, repository, storage, publisher = build_pipeline(
        make_settings, tmp_path, repository=repository
    )

    result = await pipeline.run("abc123")

    assert result.status is Phase2Status.DUPLICATE
    assert result.duplicate is True
    assert result.media_item_id == "existing-media"
    assert storage.uploads == []
    assert publisher.calls == []
    assert repository.created_media == []
    assert repository.sources[0][0] == "existing-media"
    assert repository.sources[0][1].submission_id == "abc123"
    assert not (tmp_path / "work").exists()


async def test_upload_failure_does_not_create_stored_media_row(make_settings, tmp_path) -> None:
    storage = FakeStorage(fail_upload=True)
    pipeline, _connector, _fetcher, repository, storage, publisher = build_pipeline(
        make_settings, tmp_path, storage=storage
    )

    result = await pipeline.run("abc123")

    assert result.status is Phase2Status.FAILED
    assert result.reason == "storage_upload_failed"
    assert repository.created_media == []
    assert repository.sources == []
    assert publisher.calls == []
    assert storage.deleted == []


async def test_database_failure_after_upload_is_recoverable_and_non_destructive(
    make_settings, tmp_path
) -> None:
    repository = FakeRepository(fail_insert=True)
    pipeline, _connector, _fetcher, repository, storage, publisher = build_pipeline(
        make_settings, tmp_path, repository=repository
    )

    result = await pipeline.run("abc123")

    assert result.status is Phase2Status.RECOVERABLE
    assert result.reason == "database_write_failed_recoverable"
    assert result.object_key == build_object_key(PNG_SHA256, "png")
    assert len(storage.uploads) == 1
    assert storage.deleted == []
    assert publisher.calls == []


async def test_publication_failure_keeps_archived_asset_and_records_failure(
    make_settings, tmp_path
) -> None:
    publisher = FakePublisher(fail=True)
    pipeline, _connector, _fetcher, repository, storage, publisher = build_pipeline(
        make_settings, tmp_path, publisher=publisher
    )

    result = await pipeline.run("abc123")

    assert result.status is Phase2Status.FAILED
    assert result.reason == "publication_failed"
    assert result.object_key == build_object_key(PNG_SHA256, "png")
    assert len(storage.uploads) == 1
    assert storage.deleted == []
    assert len(repository.created_media) == 1
    assert repository.updates[-1]["status"] == "failed"
    assert repository.updates[-1]["last_error"] == "publication_failed"
    assert not (tmp_path / "work").exists()


async def test_destination_not_configured_stops_before_source_access(
    make_settings, tmp_path
) -> None:
    pipeline, connector, fetcher, repository, storage, publisher = build_pipeline(
        make_settings,
        tmp_path,
        channel_id=None,
    )

    result = await pipeline.run("abc123")

    assert result.status is Phase2Status.FAILED
    assert result.reason == "destination_not_configured"
    assert connector.calls == []
    assert fetcher.calls == []
    assert repository.lookups == []
    assert storage.uploads == []
    assert publisher.calls == []


async def test_gate_and_credential_failures_are_reported(make_settings, tmp_path) -> None:
    gate_pipeline, _connector, _fetcher, _repository, _storage, _publisher = build_pipeline(
        make_settings,
        tmp_path,
        connector=FakeConnector(error=RedditAccessNotApprovedError("not approved")),
    )
    gate_result = await gate_pipeline.run("abc123")
    assert gate_result.status is Phase2Status.FAILED
    assert gate_result.reason == "reddit_access_not_approved"

    credentials_pipeline, *_rest = build_pipeline(
        make_settings,
        tmp_path / "credentials",
        connector=FakeConnector(error=RedditConfigurationError("missing credentials")),
    )
    credentials_result = await credentials_pipeline.run("abc123")
    assert credentials_result.status is Phase2Status.FAILED
    assert credentials_result.reason == "reddit_credentials_missing"


async def test_unsupported_and_oversized_media_are_skipped_and_cleaned(
    make_settings, tmp_path
) -> None:
    unsupported_pipeline, *_rest = build_pipeline(
        make_settings,
        tmp_path / "unsupported",
        fetcher=FakeFetcher(error=UnsupportedMediaError("unsupported")),
    )
    unsupported = await unsupported_pipeline.run("abc123")
    assert unsupported.status is Phase2Status.SKIPPED
    assert unsupported.reason == "unsupported_media"

    oversized_pipeline, *_rest = build_pipeline(
        make_settings,
        tmp_path / "oversized",
        fetcher=FakeFetcher(error=OversizeMediaError("oversized")),
    )
    oversized = await oversized_pipeline.run("abc123")
    assert oversized.status is Phase2Status.SKIPPED
    assert oversized.reason == "oversized_media"


async def test_candidate_without_supported_media_is_skipped(make_settings, tmp_path) -> None:
    candidate = make_candidate(
        media_url=None,
        extraction_reason="reddit_hosted_video_not_supported_in_phase2",
    )
    pipeline, _connector, fetcher, _repository, _storage, _publisher = build_pipeline(
        make_settings,
        tmp_path,
        connector=FakeConnector(candidate=candidate),
    )

    result = await pipeline.run("abc123")

    assert result.status is Phase2Status.SKIPPED
    assert result.reason == "reddit_hosted_video_not_supported_in_phase2"
    assert fetcher.calls == []


async def test_publication_record_failure_is_recoverable(make_settings, tmp_path) -> None:
    repository = FakeRepository(fail_publication=True)
    pipeline, *_rest = build_pipeline(
        make_settings,
        tmp_path,
        repository=repository,
    )

    result = await pipeline.run("abc123")

    assert result.status is Phase2Status.RECOVERABLE
    assert result.reason == "publication_record_failed_recoverable"
    assert result.object_key == build_object_key(PNG_SHA256, "png")


async def test_publication_status_update_failure_is_recoverable(make_settings, tmp_path) -> None:
    repository = FakeRepository(fail_update=True)
    pipeline, *_rest = build_pipeline(
        make_settings,
        tmp_path,
        repository=repository,
    )

    result = await pipeline.run("abc123")

    assert result.status is Phase2Status.RECOVERABLE
    assert result.reason == "publication_status_update_failed"
    assert result.message_id == 555
    assert result.object_key == build_object_key(PNG_SHA256, "png")


async def test_manual_save_publishes_immediately_to_saves_destination(
    make_settings, tmp_path
) -> None:
    pipeline, _connector, _fetcher, repository, storage, publisher = build_pipeline(
        make_settings,
        tmp_path,
        channel_id="999",
        saves_channel_id="888",
        publication_purpose="manual_save",
    )

    result = await pipeline.run("abc123")

    assert result.status is Phase2Status.PUBLISHED
    assert result.reason == "published"
    assert len(publisher.calls) == 1
    assert repository.publications == [
        {
            "media_item_id": "media-1",
            "channel_id": 888,
            "purpose": "manual_save",
        }
    ]
    assert repository.updates[-1]["status"] == "published"
    assert repository.updates[-1]["message_id"] == 555
    assert storage.downloads == [build_object_key(PNG_SHA256, "png")]
