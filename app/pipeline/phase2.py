"""Reddit-to-B2-to-Supabase-to-Discord media processing pipeline."""

from __future__ import annotations

import asyncio
import inspect
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
from app.media.fetcher import (
    DownloadedMedia,
    MediaFetcher,
    OversizeMediaError,
    UnsupportedMediaError,
)
from app.media.hashing import PerceptualHasher, find_similar_hashes
from app.media.metadata import extract_media_metadata
from app.media.validation import MAX_MEDIA_BYTES, build_object_key, sanitize_filename
from app.media.video import (
    OversizeVideoError,
    VideoDownloader,
    VideoDownloadError,
    VideoDownloadUnavailableError,
)
from app.reddit.connector import (
    RedditAccessNotApprovedError,
    RedditConfigurationError,
    RedditConnector,
)
from app.reddit.models import MediaAsset, RedditCandidate

log = logging.getLogger(__name__)


class Phase2Status(StrEnum):
    ARCHIVED = "archived"
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


def _accepts_keyword(function: Any, name: str) -> bool:
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False
    if name in parameters:
        return True
    return any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())


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
        video_downloader: Any | None = None,
        perceptual_hasher: PerceptualHasher | None = None,
        ffprobe_runner: Callable[[Path], dict[str, Any] | None] | None = None,
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
        self._video_downloader = video_downloader or VideoDownloader()
        self._perceptual_hasher = perceptual_hasher or PerceptualHasher(
            enabled=bool(getattr(settings, "perceptual_hash_enabled", False))
        )
        self._ffprobe_runner = ffprobe_runner
        self._source_accepts_media_id = _accepts_keyword(
            repository.record_source, "source_media_id"
        )

    async def run(self, submission_reference: str) -> Phase2Result:
        """Process every supported asset, preserving the single-asset result shape."""
        candidate, results = await self._execute(submission_reference)
        if len(results) == 1:
            return results[0]
        return self._aggregate(candidate, results)

    async def run_all(self, submission_reference: str) -> list[Phase2Result]:
        """Process every supported asset and return one result per asset."""
        _candidate, results = await self._execute(submission_reference)
        return results

    async def process_candidate(
        self, candidate: RedditCandidate, *, publish: bool = True
    ) -> list[Phase2Result]:
        """Process an already normalized submission through the shared asset pipeline."""
        _candidate, results = await self._process_candidate(candidate, publish=publish)
        return results

    async def _execute(
        self,
        submission_reference: str,
    ) -> tuple[RedditCandidate | None, list[Phase2Result]]:
        if not self._publication_channel_id:
            return None, [Phase2Result(Phase2Status.FAILED, "destination_not_configured")]

        try:
            candidate = await self._connector.fetch_candidate(submission_reference)
        except RedditAccessNotApprovedError:
            return None, [Phase2Result(Phase2Status.FAILED, "reddit_access_not_approved")]
        except RedditConfigurationError:
            return None, [Phase2Result(Phase2Status.FAILED, "reddit_credentials_missing")]
        except ValueError:
            return None, [Phase2Result(Phase2Status.FAILED, "invalid_submission_reference")]
        except Exception as exc:
            log.warning("Reddit candidate fetch failed (%s)", type(exc).__name__)
            return None, [Phase2Result(Phase2Status.FAILED, "reddit_fetch_failed")]

        return await self._process_candidate(candidate, publish=True)

    async def _process_candidate(
        self, candidate: RedditCandidate, *, publish: bool
    ) -> tuple[RedditCandidate, list[Phase2Result]]:
        assets = candidate.assets
        if not assets:
            details = {}
            diagnostics = candidate.media_metadata.get("gallery_diagnostics")
            if isinstance(diagnostics, dict):
                details["gallery"] = diagnostics
            return candidate, [
                Phase2Result(
                    Phase2Status.SKIPPED,
                    candidate.extraction_reason or "no_supported_media",
                    submission_id=candidate.submission_id,
                    details=details,
                )
            ]

        temp_dir = Path(self._temp_dir_factory())
        temp_dir.mkdir(parents=True, exist_ok=True)
        try:
            results: list[Phase2Result] = []
            seen_assets: set[str] = set()
            for asset in assets:
                if asset.asset_key in seen_assets:
                    results.append(
                        Phase2Result(
                            Phase2Status.SKIPPED,
                            "duplicate_asset_in_submission",
                            submission_id=candidate.submission_id,
                            details={"asset_url": asset.url},
                        )
                    )
                    continue
                seen_assets.add(asset.asset_key)
                results.append(
                    await self._process_asset(candidate, asset, temp_dir, publish=publish)
                )
            return candidate, results
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    async def _process_asset(
        self,
        candidate: RedditCandidate,
        asset: MediaAsset,
        temp_dir: Path,
        *,
        publish: bool = True,
    ) -> Phase2Result:
        downloaded = await self._download(asset, temp_dir)
        if isinstance(downloaded, Phase2Result):
            return downloaded

        metadata = await asyncio.to_thread(
            extract_media_metadata,
            downloaded.path,
            downloaded.media,
            size_bytes=downloaded.size_bytes,
            ffprobe_runner=self._ffprobe_runner,
        )
        perceptual_hash = await asyncio.to_thread(
            self._perceptual_hasher.compute,
            downloaded.path,
            downloaded.media,
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
            return await self._record_duplicate(
                candidate,
                asset,
                existing,
                sha256,
                metadata,
                perceptual_hash,
            )

        object_key = build_object_key(sha256, downloaded.media.extension)
        filename = sanitize_filename(self._filename_for(candidate, asset, downloaded))

        try:
            await self._store_object(downloaded, object_key)
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
        except Exception as exc:
            log.error(
                "Discovery write failed after B2 upload (%s); archived object %s retained",
                type(exc).__name__,
                object_key,
            )
            return Phase2Result(
                Phase2Status.RECOVERABLE,
                "discovery_write_failed_recoverable",
                submission_id=candidate.submission_id,
                sha256=sha256,
                object_key=object_key,
            )

        payload = self._media_payload(
            downloaded,
            metadata,
            object_key=object_key,
            filename=filename,
            perceptual_hash=perceptual_hash,
        )
        try:
            media_row = await self._repository.create_media_item(payload)
        except Exception as exc:
            log.error(
                "Media item write failed after B2 upload (%s); archived object %s retained",
                type(exc).__name__,
                object_key,
            )
            recovered = await self._recover_existing_media(candidate, sha256)
            if recovered is None:
                return Phase2Result(
                    Phase2Status.RECOVERABLE,
                    "database_write_failed_recoverable",
                    submission_id=candidate.submission_id,
                    sha256=sha256,
                    object_key=object_key,
                )
            media_row = recovered

        media_item_id = str(media_row["id"])
        source_result = await self._store_source(
            candidate,
            asset,
            media_item_id,
            discovery,
        )
        if source_result is not None:
            return source_result

        if media_row.get("_recovered"):
            return Phase2Result(
                Phase2Status.DUPLICATE,
                "concurrent_duplicate_source_recorded",
                submission_id=candidate.submission_id,
                sha256=sha256,
                object_key=object_key,
                media_item_id=media_item_id,
                duplicate=True,
            )

        if not publish:
            return Phase2Result(
                Phase2Status.ARCHIVED,
                "archived_for_future_digest",
                submission_id=candidate.submission_id,
                sha256=sha256,
                object_key=object_key,
                media_item_id=media_item_id,
            )
        return await self._publish(
            candidate,
            sha256=sha256,
            object_key=object_key,
            media_item_id=media_item_id,
            filename=filename,
        )

    async def _download(
        self,
        asset: MediaAsset,
        temp_dir: Path,
    ) -> DownloadedMedia | Phase2Result:
        if asset.requires_video_download:
            try:
                return await self._video_downloader.download(
                    asset.download_url or asset.url,
                    destination_dir=temp_dir,
                    max_bytes=self._max_media_bytes,
                )
            except VideoDownloadUnavailableError:
                return Phase2Result(
                    Phase2Status.SKIPPED,
                    "video_downloader_unavailable",
                    details={"asset_url": asset.url},
                )
            except OversizeVideoError:
                return Phase2Result(
                    Phase2Status.SKIPPED,
                    "oversized_media",
                    details={"asset_url": asset.url},
                )
            except VideoDownloadError as exc:
                log.warning("Video download failed (%s)", type(exc).__name__)
                return Phase2Result(
                    Phase2Status.SKIPPED,
                    "video_download_failed",
                    details={"asset_url": asset.url},
                )
            except Exception as exc:
                log.warning("Video download failed (%s)", type(exc).__name__)
                return Phase2Result(
                    Phase2Status.FAILED,
                    "video_download_failed",
                    details={"asset_url": asset.url},
                )

        try:
            return await self._fetcher.download(
                asset.url,
                destination_dir=temp_dir,
                max_bytes=self._max_media_bytes,
            )
        except OversizeMediaError:
            return Phase2Result(
                Phase2Status.SKIPPED,
                "oversized_media",
                details={"asset_url": asset.url},
            )
        except UnsupportedMediaError:
            return Phase2Result(
                Phase2Status.SKIPPED,
                "unsupported_media",
                details={"asset_url": asset.url},
            )
        except Exception as exc:
            log.warning("Media download failed (%s)", type(exc).__name__)
            return Phase2Result(
                Phase2Status.FAILED,
                "media_download_failed",
                details={"asset_url": asset.url},
            )

    async def _store_object(self, downloaded: DownloadedMedia, object_key: str) -> None:
        object_exists = getattr(self._storage, "object_exists", None)
        if callable(object_exists):
            try:
                if await object_exists(object_key):
                    verify_object = getattr(self._storage, "verify_object", None)
                    if callable(verify_object) and await verify_object(
                        object_key, sha256=downloaded.sha256, size_bytes=downloaded.size_bytes
                    ):
                        log.info("Verified existing B2 object %s", object_key)
                        return
                    log.warning("Existing B2 object failed byte verification; replacing it")
            except Exception as exc:
                log.warning(
                    "B2 object existence check failed (%s); attempting upload",
                    type(exc).__name__,
                )

        upload_file = getattr(self._storage, "upload_file", None)
        if callable(upload_file):
            await upload_file(
                object_key,
                downloaded.path,
                downloaded.media.mime_type,
            )
        else:
            await self._storage.upload_bytes(
                object_key,
                downloaded.read_bytes(),
                downloaded.media.mime_type,
            )
        verify_object = getattr(self._storage, "verify_object", None)
        if not callable(verify_object) or not await verify_object(
            object_key, sha256=downloaded.sha256, size_bytes=downloaded.size_bytes
        ):
            raise RuntimeError("Stored object failed byte integrity verification")

    def _media_payload(
        self,
        downloaded: DownloadedMedia,
        metadata: Any,
        *,
        object_key: str,
        filename: str,
        perceptual_hash: str | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "sha256": downloaded.sha256,
            "filename": filename,
            "extension": downloaded.media.extension,
            "mime_type": downloaded.media.mime_type,
            "media_type": downloaded.media.media_type,
            "category": "video" if downloaded.media.media_type == "video" else "other",
            "size_bytes": downloaded.size_bytes,
            "storage_bucket": self._storage.bucket_name,
            "storage_object_key": object_key,
            "verification_status": "verified",
            "download_status": "stored",
            "downloaded_at": datetime.now(tz=UTC).isoformat(),
        }
        payload.update(metadata.media_item_fields())
        if perceptual_hash:
            payload["perceptual_hash"] = perceptual_hash
        return payload

    def _filename_for(
        self,
        candidate: RedditCandidate,
        asset: MediaAsset,
        downloaded: DownloadedMedia,
    ) -> str:
        extension = downloaded.media.extension
        if asset.gallery_index is not None:
            return f"reddit-{candidate.submission_id}-{asset.gallery_index + 1}.{extension}"
        if asset.requires_video_download:
            return f"reddit-{candidate.submission_id}.{extension}"
        if asset.filename:
            return asset.filename
        return f"reddit-{candidate.submission_id}.{extension}"

    async def _recover_existing_media(
        self,
        candidate: RedditCandidate,
        sha256: str,
    ) -> dict[str, Any] | None:
        try:
            existing = await self._repository.get_media_by_sha256(sha256)
        except Exception as exc:
            log.warning("Recovery lookup failed (%s)", type(exc).__name__)
            return None
        if existing is None:
            return None
        return {**existing, "_recovered": True}

    async def _record_duplicate(
        self,
        candidate: RedditCandidate,
        asset: MediaAsset,
        existing: dict[str, Any],
        sha256: str,
        metadata: Any,
        perceptual_hash: str | None,
    ) -> Phase2Result:
        media_item_id = existing.get("id")
        if media_item_id is None:
            return Phase2Result(
                Phase2Status.RECOVERABLE,
                "existing_media_record_invalid",
                submission_id=candidate.submission_id,
                sha256=sha256,
            )

        await self._fill_missing_metadata(str(media_item_id), metadata, perceptual_hash)
        similarity = await self._similarity_signal(perceptual_hash, str(media_item_id))

        try:
            discovery = await self._repository.upsert_discovery(candidate)
            await self._record_source(
                str(media_item_id),
                candidate,
                asset,
                discovery_id=str(discovery["id"]) if discovery.get("id") else None,
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
                details={"similar_media": similarity},
            )

        return Phase2Result(
            Phase2Status.DUPLICATE,
            "duplicate_source_recorded",
            submission_id=candidate.submission_id,
            sha256=sha256,
            media_item_id=str(media_item_id),
            duplicate=True,
            details={"similar_media": similarity},
        )

    async def _fill_missing_metadata(
        self,
        media_item_id: str,
        metadata: Any,
        perceptual_hash: str | None,
    ) -> None:
        update = getattr(self._repository, "update_media_item_metadata", None)
        if not callable(update):
            return
        fields = metadata.media_item_fields()
        if perceptual_hash:
            fields["perceptual_hash"] = perceptual_hash
        if not fields:
            return
        try:
            await update(media_item_id, fields)
        except Exception as exc:
            log.warning("Metadata fill-in failed (%s)", type(exc).__name__)

    async def _similarity_signal(
        self,
        perceptual_hash: str | None,
        media_item_id: str,
    ) -> list[dict[str, Any]]:
        if not perceptual_hash:
            return []
        list_hashes = getattr(self._repository, "list_media_with_perceptual_hash", None)
        if not callable(list_hashes):
            return []
        try:
            candidates = await list_hashes()
        except Exception as exc:
            log.warning("Perceptual similarity lookup failed (%s)", type(exc).__name__)
            return []
        return [
            signal
            for signal in find_similar_hashes(perceptual_hash, candidates)
            if str(signal.get("media_item_id")) != media_item_id
        ]

    async def _store_source(
        self,
        candidate: RedditCandidate,
        asset: MediaAsset,
        media_item_id: str,
        discovery: dict[str, Any],
    ) -> Phase2Result | None:
        try:
            await self._record_source(
                media_item_id,
                candidate,
                asset,
                discovery_id=str(discovery["id"]) if discovery.get("id") else None,
            )
        except Exception as exc:
            log.error(
                "Source attribution failed after media item write (%s); media row retained",
                type(exc).__name__,
            )
            return Phase2Result(
                Phase2Status.RECOVERABLE,
                "source_attribution_failed_recoverable",
                submission_id=candidate.submission_id,
                media_item_id=media_item_id,
            )
        return None

    async def _record_source(
        self,
        media_item_id: str,
        candidate: RedditCandidate,
        asset: MediaAsset,
        *,
        discovery_id: str | None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "discovery_id": discovery_id,
            "media_url": asset.url,
        }
        if self._source_accepts_media_id:
            kwargs["source_media_id"] = asset.source_media_id
        return await self._repository.record_source(media_item_id, candidate, **kwargs)

    async def _publish(
        self,
        candidate: RedditCandidate,
        *,
        sha256: str,
        object_key: str,
        media_item_id: str,
        filename: str,
    ) -> Phase2Result:
        channel_id = self._publication_channel_id
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

    @property
    def _publication_channel_id(self) -> int:
        if self._publication_purpose == "manual_save":
            return int(
                self._settings.saves_channel_id or self._settings.discoveries_channel_id or 0
            )
        return int(self._settings.discoveries_channel_id or 0)

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

    def _aggregate(
        self,
        candidate: RedditCandidate | None,
        results: list[Phase2Result],
    ) -> Phase2Result:
        statuses = {result.status for result in results}
        if Phase2Status.RECOVERABLE in statuses:
            status = Phase2Status.RECOVERABLE
            reason = "partial_failure_recoverable"
        elif Phase2Status.FAILED in statuses:
            status = Phase2Status.FAILED
            reason = "partial_failure"
        elif Phase2Status.PUBLISHED in statuses:
            status = Phase2Status.PUBLISHED
            reason = "published_multiple_assets"
        elif Phase2Status.DUPLICATE in statuses:
            status = Phase2Status.DUPLICATE
            reason = "duplicates_recorded"
        else:
            status = Phase2Status.SKIPPED
            reason = "no_supported_media"

        return Phase2Result(
            status,
            reason,
            submission_id=candidate.submission_id if candidate else None,
            duplicate=bool(results) and all(result.duplicate for result in results),
            details={
                "asset_count": len(results),
                "published_count": sum(
                    1 for result in results if result.status is Phase2Status.PUBLISHED
                ),
                "duplicate_count": sum(
                    1 for result in results if result.status is Phase2Status.DUPLICATE
                ),
                "skipped_count": sum(
                    1 for result in results if result.status is Phase2Status.SKIPPED
                ),
                "failed_count": sum(
                    1 for result in results if result.status is Phase2Status.FAILED
                ),
                "recoverable_count": sum(
                    1 for result in results if result.status is Phase2Status.RECOVERABLE
                ),
                "asset_results": [result.to_dict() for result in results],
            },
        )
