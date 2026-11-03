"""One controlled Reddit-to-B2-to-Supabase-to-Discord vertical slice."""

from __future__ import annotations

import logging
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.config import Settings
from app.media.fetcher import MediaFetcher, OversizeMediaError, UnsupportedMediaError
from app.media.validation import MAX_MEDIA_BYTES, build_object_key, sanitize_filename
from app.reddit.connector import (
    RedditAccessNotApprovedError,
    RedditConfigurationError,
    RedditConnector,
)
from app.reddit.models import RedditCandidate

log = logging.getLogger(__name__)


class Phase2Status(StrEnum):
    PUBLISHED = "published"
    DUPLICATE = "duplicate"
    SKIPPED = "skipped"
    FAILED = "failed"
    RECOVERABLE = "recoverable"


@dataclass(frozen=True, slots=True)
class Phase2Result:
    status: Phase2Status
    reason: str
    submission_id: str | None = None
    sha256: str | None = None
    object_key: str | None = None
    media_item_id: str | None = None
    message_id: int | None = None
    duplicate: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "reason": self.reason,
            "submission_id": self.submission_id,
            "sha256": self.sha256,
            "object_key": self.object_key,
            "media_item_id": self.media_item_id,
            "message_id": self.message_id,
            "duplicate": self.duplicate,
            "details": self.details,
        }


class Phase2Pipeline:
    """Coordinate source, acquisition, storage, persistence, and publication."""

    def __init__(
        self,
        *,
        settings: Settings,
        connector: RedditConnector,
        fetcher: MediaFetcher,
        repository: Any,
        storage: Any,
        publisher: Any,
        max_media_bytes: int = MAX_MEDIA_BYTES,
        publication_purpose: str = "test_discovery",
        temp_dir_factory: Callable[[], str] | None = None,
    ) -> None:
        self._settings = settings
        self._connector = connector
        self._fetcher = fetcher
        self._repository = repository
        self._storage = storage
        self._publisher = publisher
        self._max_media_bytes = max_media_bytes
        self._publication_purpose = publication_purpose
        self._temp_dir_factory = temp_dir_factory or (
            lambda: tempfile.mkdtemp(prefix="tifa-phase2-")
        )

    async def run(self, submission_reference: str) -> Phase2Result:
        if not self._settings.discoveries_channel_id:
            return Phase2Result(
                Phase2Status.FAILED,
                "destination_not_configured",
            )

        try:
            candidate = await self._connector.fetch_candidate(submission_reference)
        except RedditAccessNotApprovedError:
            return Phase2Result(
                Phase2Status.FAILED,
                "reddit_access_not_approved",
            )
        except RedditConfigurationError:
            return Phase2Result(
                Phase2Status.FAILED,
                "reddit_credentials_missing",
            )
        except ValueError:
            return Phase2Result(Phase2Status.FAILED, "invalid_submission_reference")
        except Exception as exc:
            log.warning("Reddit candidate fetch failed (%s)", type(exc).__name__)
            return Phase2Result(Phase2Status.FAILED, "reddit_fetch_failed")

        base = Phase2Result(
            Phase2Status.SKIPPED,
            candidate.extraction_reason or "no_supported_media",
            submission_id=candidate.submission_id,
        )
        if not candidate.media_url:
            return base

        temp_dir = Path(self._temp_dir_factory())
        try:
            return await self._process_candidate(candidate, temp_dir)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    async def _process_candidate(
        self,
        candidate: RedditCandidate,
        temp_dir: Path,
    ) -> Phase2Result:
        assert candidate.media_url is not None

        try:
            downloaded = await self._fetcher.download(
                candidate.media_url,
                destination_dir=temp_dir,
                max_bytes=self._max_media_bytes,
            )
        except OversizeMediaError:
            return Phase2Result(
                Phase2Status.SKIPPED,
                "oversized_media",
                submission_id=candidate.submission_id,
            )
        except UnsupportedMediaError:
            return Phase2Result(
                Phase2Status.SKIPPED,
                "unsupported_media",
                submission_id=candidate.submission_id,
            )
        except Exception as exc:
            log.warning("Media download failed (%s)", type(exc).__name__)
            return Phase2Result(
                Phase2Status.FAILED,
                "media_download_failed",
                submission_id=candidate.submission_id,
            )

        sha256 = downloaded.sha256
        try:
            existing = await self._repository.get_media_by_sha256(sha256)
        except Exception as exc:
            log.warning("Media lookup failed (%s)", type(exc).__name__)
            return Phase2Result(
                Phase2Status.FAILED,
                "database_lookup_failed",
                submission_id=candidate.submission_id,
                sha256=sha256,
            )

        if existing is not None:
            return await self._record_duplicate(candidate, existing, sha256)

        object_key = build_object_key(sha256, downloaded.media.extension)
        filename = sanitize_filename(
            f"reddit-{candidate.submission_id}.{downloaded.media.extension}"
        )

        try:
            await self._storage.upload_bytes(
                object_key,
                downloaded.read_bytes(),
                downloaded.media.mime_type,
            )
        except Exception as exc:
            log.warning("B2 upload failed (%s); no media row will be stored", type(exc).__name__)
            return Phase2Result(
                Phase2Status.FAILED,
                "storage_upload_failed",
                submission_id=candidate.submission_id,
                sha256=sha256,
                object_key=object_key,
            )

        try:
            discovery = await self._repository.upsert_discovery(candidate)
            media_row = await self._repository.create_media_item(
                {
                    "sha256": sha256,
                    "filename": filename,
                    "extension": downloaded.media.extension,
                    "mime_type": downloaded.media.mime_type,
                    "media_type": downloaded.media.media_type,
                    "category": "other",
                    "size_bytes": downloaded.size_bytes,
                    "storage_bucket": self._storage.bucket_name,
                    "storage_object_key": object_key,
                    "verification_status": "verified",
                    "download_status": "stored",
                    "downloaded_at": datetime.now(tz=UTC).isoformat(),
                }
            )
            media_item_id = str(media_row["id"])
            await self._repository.record_source(
                media_item_id,
                candidate,
                discovery_id=str(discovery["id"]) if discovery.get("id") else None,
                media_url=candidate.media_url,
            )
        except Exception as exc:
            log.error(
                "Database write failed after B2 upload (%s); archived object %s retained "
                "for recovery",
                type(exc).__name__,
                object_key,
            )
            return Phase2Result(
                Phase2Status.RECOVERABLE,
                "database_write_failed_recoverable",
                submission_id=candidate.submission_id,
                sha256=sha256,
                object_key=object_key,
            )

        return await self._publish(
            candidate,
            sha256=sha256,
            object_key=object_key,
            media_item_id=media_item_id,
            filename=filename,
        )

    async def _record_duplicate(
        self,
        candidate: RedditCandidate,
        existing: dict[str, Any],
        sha256: str,
    ) -> Phase2Result:
        media_item_id = existing.get("id")
        if media_item_id is None:
            return Phase2Result(
                Phase2Status.RECOVERABLE,
                "existing_media_record_invalid",
                submission_id=candidate.submission_id,
                sha256=sha256,
            )

        try:
            discovery = await self._repository.upsert_discovery(candidate)
            await self._repository.record_source(
                str(media_item_id),
                candidate,
                discovery_id=str(discovery["id"]) if discovery.get("id") else None,
                media_url=candidate.media_url,
            )
        except Exception as exc:
            log.warning(
                "Duplicate source attribution failed (%s); existing asset retained",
                type(exc).__name__,
            )
            return Phase2Result(
                Phase2Status.RECOVERABLE,
                "duplicate_source_attribution_failed",
                submission_id=candidate.submission_id,
                sha256=sha256,
                media_item_id=str(media_item_id),
                duplicate=True,
            )

        return Phase2Result(
            Phase2Status.DUPLICATE,
            "duplicate_source_recorded",
            submission_id=candidate.submission_id,
            sha256=sha256,
            media_item_id=str(media_item_id),
            duplicate=True,
        )

    async def _publish(
        self,
        candidate: RedditCandidate,
        *,
        sha256: str,
        object_key: str,
        media_item_id: str,
        filename: str,
    ) -> Phase2Result:
        channel_id = int(self._settings.discoveries_channel_id or 0)
        try:
            publication = await self._repository.create_publication(
                media_item_id,
                channel_id,
                self._publication_purpose,
            )
            publication_id = str(publication["id"])
        except Exception as exc:
            log.error(
                "Publication record could not be created (%s); archived object %s retained",
                type(exc).__name__,
                object_key,
            )
            return Phase2Result(
                Phase2Status.RECOVERABLE,
                "publication_record_failed_recoverable",
                submission_id=candidate.submission_id,
                sha256=sha256,
                object_key=object_key,
                media_item_id=media_item_id,
            )

        try:
            data = await self._storage.download_bytes(
                object_key,
                max_bytes=self._max_media_bytes,
            )
            message_id = await self._publisher.publish(
                filename=filename,
                data=data,
                candidate=candidate,
            )
        except Exception as exc:
            log.warning(
                "Discord publication failed (%s); archived asset %s retained",
                type(exc).__name__,
                object_key,
            )
            await self._safe_fail_publication(publication_id)
            return Phase2Result(
                Phase2Status.FAILED,
                "publication_failed",
                submission_id=candidate.submission_id,
                sha256=sha256,
                object_key=object_key,
                media_item_id=media_item_id,
            )

        try:
            await self._repository.update_publication(
                publication_id,
                status="published",
                message_id=message_id,
            )
        except Exception as exc:
            log.warning(
                "Publication succeeded but status update failed (%s)",
                type(exc).__name__,
            )
            return Phase2Result(
                Phase2Status.RECOVERABLE,
                "publication_status_update_failed",
                submission_id=candidate.submission_id,
                sha256=sha256,
                object_key=object_key,
                media_item_id=media_item_id,
                message_id=message_id,
            )

        return Phase2Result(
            Phase2Status.PUBLISHED,
            "published",
            submission_id=candidate.submission_id,
            sha256=sha256,
            object_key=object_key,
            media_item_id=media_item_id,
            message_id=message_id,
        )

    async def _safe_fail_publication(self, publication_id: str) -> None:
        try:
            await self._repository.update_publication(
                publication_id,
                status="failed",
                last_error="publication_failed",
            )
        except Exception as exc:
            log.warning(
                "Failed to record publication failure (%s)",
                type(exc).__name__,
            )
