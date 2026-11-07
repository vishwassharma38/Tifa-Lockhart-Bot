"""Restart-safe daily digest delivery for verified archived media."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.bot.publisher import PublicationContext
from app.config import Settings

log = logging.getLogger(__name__)

DIGEST_TIMEZONE = "Asia/Kolkata"
DIGEST_HOUR = 4
DIGEST_MINUTE = 0
DEFAULT_DIGEST_ATTEMPT_LIMIT = 3
DEFAULT_PAGE_SIZE = 100
DEFAULT_RETRY_BATCH_SIZE = 100
MAX_DIGEST_MEDIA_BYTES = 10_000_000
INITIAL_DIGEST_WINDOW_START = datetime(1970, 1, 1, tzinfo=UTC)
_PUBLICATION_MARKER_PREFIX = "tifa-digest-publication:"


def publication_marker(publication_id: str) -> str:
    """Return the deterministic marker used for Discord/database reconciliation."""
    return f"{_PUBLICATION_MARKER_PREFIX}{publication_id}"


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def digest_window_incomplete(state: dict[str, Any] | None) -> bool:
    """Return whether a persisted window has not been durably accounted for."""
    if not isinstance(state, dict):
        return False
    start = _as_datetime(state.get("last_window_start"))
    end = _as_datetime(state.get("last_window_end"))
    if start is None or end is None:
        return False
    completed = _as_datetime(state.get("last_completed_at"))
    return completed is None or completed < end


def latest_digest_due_at(
    now: datetime,
    *,
    timezone: str = DIGEST_TIMEZONE,
) -> datetime:
    """Return the most recent 04:00 digest occurrence at or before ``now``."""
    local = now.astimezone(ZoneInfo(timezone))
    due = local.replace(
        hour=DIGEST_HOUR,
        minute=DIGEST_MINUTE,
        second=0,
        microsecond=0,
    )
    if local < due:
        due -= timedelta(days=1)
    return due.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class DigestWindow:
    """Persisted upper and lower boundaries for one digest run."""

    start: datetime
    end: datetime
    cursor: datetime
    resumed: bool


@dataclass(frozen=True, slots=True)
class DigestRunResult:
    """Truthful outcome of one digest invocation."""

    outcome: str
    processed_count: int = 0
    published_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    window_start: str | None = None
    window_end: str | None = None
    resumed: bool = False
    job_id: str | None = None
    error_summary: str | None = None


class DigestService:
    """Process eligible archived media through the existing publisher."""

    def __init__(
        self,
        *,
        settings: Settings,
        repository: Any,
        storage: Any,
        publisher: Any,
        attempt_limit: int = DEFAULT_DIGEST_ATTEMPT_LIMIT,
        page_size: int = DEFAULT_PAGE_SIZE,
        retry_batch_size: int = DEFAULT_RETRY_BATCH_SIZE,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._storage = storage
        self._publisher = publisher
        self._attempt_limit = max(1, attempt_limit)
        self._page_size = max(1, page_size)
        self._retry_batch_size = max(1, retry_batch_size)
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = asyncio.Lock()

    @property
    def attempt_limit(self) -> int:
        return self._attempt_limit

    async def catch_up_required(self) -> bool:
        """Report whether startup must resume or catch up a digest window."""
        state = await self._repository.get_digest_state()
        if digest_window_incomplete(state):
            return True
        completed = _as_datetime(state.get("last_completed_at"))
        if completed is None:
            return True
        return completed < latest_digest_due_at(self._now())

    async def run(self, *, trigger: str = "scheduled") -> DigestRunResult:
        """Run once, serialized with every other digest invocation."""
        if self._lock.locked():
            log.info("Digest skipped because another run is active trigger=%s", trigger)
            return DigestRunResult(
                outcome="skipped",
                error_summary="digest_already_running",
            )
        async with self._lock:
            return await self._run_locked(trigger=trigger)

    async def _run_locked(self, *, trigger: str) -> DigestRunResult:
        counters = {
            "processed_count": 0,
            "stored_count": 0,
            "skipped_count": 0,
            "failed_count": 0,
        }
        errors: Counter[str] = Counter()
        job_id: str | None = None
        window: DigestWindow | None = None
        outcome = "failed"
        try:
            job_id = await self._repository.create_job_run(
                {"trigger": trigger},
                job_type="daily_digest",
            )
            window = await self._prepare_window()
            processed: set[tuple[str, int]] = set()
            unaccounted: list[str] = []
            await self._process_candidates(window, processed, counters, errors, unaccounted)
            await self._process_retryable(processed, counters, errors, unaccounted)
            if unaccounted:
                errors["unaccounted_items"] += len(unaccounted)
                outcome = "partial"
                log.warning(
                    "Digest window held incomplete because %s item(s) lack a durable "
                    "publication record",
                    len(unaccounted),
                )
            else:
                await self._repository.update_digest_state(
                    {
                        "last_window_start": _iso(window.start),
                        "last_window_end": _iso(window.end),
                        "last_completed_at": _iso(self._now()),
                        "cursor_media_created_at": _iso(window.end),
                    }
                )
                if errors:
                    outcome = "partial"
                else:
                    outcome = "succeeded"
                    try:
                        await self._repository.mark_digest_success()
                    except Exception as exc:
                        errors[f"digest_success_update_failed:{type(exc).__name__}"] += 1
                        outcome = "partial"
                        log.warning(
                            "Digest completed but last-success update failed (%s)",
                            type(exc).__name__,
                        )
            log.info(
                "Digest outcome=%s resumed=%s window_start=%s window_end=%s "
                "processed=%s published=%s skipped=%s failed=%s",
                outcome,
                window.resumed,
                _iso(window.start),
                _iso(window.end),
                counters["processed_count"],
                counters["stored_count"],
                counters["skipped_count"],
                counters["failed_count"],
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            outcome = "failed"
            counters["failed_count"] += 1
            errors[f"digest_failed:{type(exc).__name__}"] += 1
            log.warning("Digest failed (%s)", type(exc).__name__)
        error_summary = self._summarize(errors)
        if job_id is not None:
            await self._finish_job(job_id, counters, outcome, error_summary)
        return DigestRunResult(
            outcome=outcome,
            processed_count=counters["processed_count"],
            published_count=counters["stored_count"],
            skipped_count=counters["skipped_count"],
            failed_count=counters["failed_count"],
            window_start=_iso(window.start) if window else None,
            window_end=_iso(window.end) if window else None,
            resumed=window.resumed if window else False,
            job_id=job_id,
            error_summary=error_summary,
        )

    async def _finish_job(
        self,
        job_id: str,
        counters: dict[str, int],
        outcome: str,
        error_summary: str | None,
    ) -> None:
        try:
            await self._repository.finish_job_run(
                job_id,
                counters,
                outcome,
                error_summary,
            )
        except Exception as exc:
            log.warning("Digest job completion could not be recorded (%s)", type(exc).__name__)

    async def _prepare_window(self) -> DigestWindow:
        state = await self._repository.get_digest_state()
        start = _as_datetime(state.get("last_window_start"))
        end = _as_datetime(state.get("last_window_end"))
        completed = _as_datetime(state.get("last_completed_at"))
        cursor = _as_datetime(state.get("cursor_media_created_at"))

        if start is not None and end is not None and (completed is None or completed < end):
            if cursor is None or cursor < start:
                cursor = start
            if cursor > end:
                cursor = end
            log.info(
                "Resuming incomplete digest window start=%s end=%s cursor=%s",
                _iso(start),
                _iso(end),
                _iso(cursor),
            )
            return DigestWindow(start=start, end=end, cursor=cursor, resumed=True)

        window_start = end if end is not None else INITIAL_DIGEST_WINDOW_START
        window_end = self._now().astimezone(UTC)
        if window_end < window_start:
            window_end = window_start
        window = DigestWindow(
            start=window_start,
            end=window_end,
            cursor=window_start,
            resumed=False,
        )
        await self._repository.update_digest_state(
            {
                "last_window_start": _iso(window.start),
                "last_window_end": _iso(window.end),
                "cursor_media_created_at": _iso(window.cursor),
            }
        )
        return window

    async def _process_candidates(
        self,
        window: DigestWindow,
        processed: set[tuple[str, int]],
        counters: dict[str, int],
        errors: Counter[str],
        unaccounted: list[str],
    ) -> None:
        scan_cursor = window.cursor
        persisted_cursor = window.cursor
        cursor_writable = True
        offset = 0
        while True:
            rows = await self._repository.list_digest_candidates(
                window_start=_iso(window.start),
                window_end=_iso(window.end),
                cursor=_iso(scan_cursor),
                limit=self._page_size,
                offset=offset,
            )
            if not rows:
                return
            for media in rows:
                accounted = await self._process_candidate(
                    media,
                    processed,
                    counters,
                    errors,
                    unaccounted,
                )
                if not accounted:
                    cursor_writable = False
            last_created = _as_datetime(rows[-1].get("created_at"))
            if last_created is not None and last_created > persisted_cursor:
                persisted_cursor = last_created
            if cursor_writable:
                try:
                    await self._repository.update_digest_state(
                        {
                            "last_window_start": _iso(window.start),
                            "last_window_end": _iso(window.end),
                            "cursor_media_created_at": _iso(persisted_cursor),
                        }
                    )
                except Exception as exc:
                    errors[f"digest_cursor_update_failed:{type(exc).__name__}"] += 1
                    log.warning("Digest cursor update failed (%s)", type(exc).__name__)
            offset += len(rows)
            if len(rows) < self._page_size:
                return

    async def _process_candidate(
        self,
        media: dict[str, Any],
        processed: set[tuple[str, int]],
        counters: dict[str, int],
        errors: Counter[str],
        unaccounted: list[str],
    ) -> bool:
        media_id = str(media.get("id") or "")
        if not media_id:
            counters["processed_count"] += 1
            counters["failed_count"] += 1
            errors["media_id_missing"] += 1
            return True
        if not self._eligible(media):
            counters["processed_count"] += 1
            counters["skipped_count"] += 1
            log.info("Digest skipped ineligible media item=%s", media_id)
            return True
        channel_id = self._destination_channel(media)
        if channel_id <= 0:
            counters["processed_count"] += 1
            counters["failed_count"] += 1
            errors["destination_not_configured"] += 1
            unaccounted.append(media_id)
            log.warning("Digest destination is not configured for media item=%s", media_id)
            return False
        key = (media_id, channel_id)
        if key in processed:
            counters["processed_count"] += 1
            counters["skipped_count"] += 1
            return True
        processed.add(key)
        counters["processed_count"] += 1
        try:
            publication = await self._repository.ensure_digest_publication(
                media_id,
                channel_id,
            )
        except Exception as exc:
            counters["failed_count"] += 1
            errors[f"publication_record_failed:{type(exc).__name__}"] += 1
            unaccounted.append(media_id)
            log.warning(
                "Digest publication record could not be created item=%s (%s)",
                media_id,
                type(exc).__name__,
            )
            return False
        return await self._deliver(
            media,
            publication,
            channel_id=channel_id,
            counters=counters,
            errors=errors,
            unaccounted=unaccounted,
        )

    async def _process_retryable(
        self,
        processed: set[tuple[str, int]],
        counters: dict[str, int],
        errors: Counter[str],
        unaccounted: list[str],
    ) -> None:
        try:
            rows = await self._repository.list_retryable_digest_publications(
                attempt_limit=self._attempt_limit,
                limit=self._retry_batch_size,
            )
        except Exception as exc:
            errors[f"retry_lookup_failed:{type(exc).__name__}"] += 1
            log.warning("Digest retry lookup failed (%s)", type(exc).__name__)
            return
        for publication in rows:
            media_id = str(publication.get("media_item_id") or "")
            try:
                channel_id = int(publication.get("channel_id") or 0)
            except (TypeError, ValueError):
                channel_id = 0
            if not media_id or channel_id <= 0:
                continue
            key = (media_id, channel_id)
            if key in processed:
                continue
            processed.add(key)
            counters["processed_count"] += 1
            try:
                media = await self._repository.get_media_item(media_id)
            except Exception as exc:
                counters["failed_count"] += 1
                errors[f"media_lookup_failed:{type(exc).__name__}"] += 1
                log.warning(
                    "Digest retry media lookup failed item=%s (%s)",
                    media_id,
                    type(exc).__name__,
                )
                continue
            if media is None or not self._eligible(media):
                counters["skipped_count"] += 1
                log.info("Digest retry skipped unavailable media item=%s", media_id)
                continue
            if self._destination_channel(media) != channel_id:
                counters["skipped_count"] += 1
                log.info(
                    "Digest retry skipped because destination changed item=%s channel=%s",
                    media_id,
                    channel_id,
                )
                continue
            await self._deliver(
                media,
                publication,
                channel_id=channel_id,
                counters=counters,
                errors=errors,
                unaccounted=unaccounted,
            )

    async def _deliver(
        self,
        media: dict[str, Any],
        publication: dict[str, Any],
        *,
        channel_id: int,
        counters: dict[str, int],
        errors: Counter[str],
        unaccounted: list[str],
    ) -> bool:
        publication_id = str(publication.get("id") or "")
        if not publication_id:
            counters["failed_count"] += 1
            errors["publication_id_missing"] += 1
            unaccounted.append(str(media.get("id") or "unknown"))
            return False
        status = str(publication.get("status") or "pending")
        attempts = self._attempt_count(publication)
        if status == "published":
            counters["skipped_count"] += 1
            return True
        if attempts >= self._attempt_limit:
            counters["skipped_count"] += 1
            log.info(
                "Digest publication exhausted publication=%s attempts=%s",
                publication_id,
                attempts,
            )
            return True
        if status == "pending" and attempts > 0:
            reconciliation = await self._reconcile(
                publication,
                channel_id=channel_id,
                errors=errors,
            )
            if reconciliation == "reconciled":
                counters["skipped_count"] += 1
                log.info(
                    "Digest reconciled existing publication=%s channel=%s",
                    publication_id,
                    channel_id,
                )
                return True
            if reconciliation == "unreconciled":
                counters["failed_count"] += 1
                log.warning(
                    "Digest held ambiguous publication=%s channel=%s",
                    publication_id,
                    channel_id,
                )
                return True

        try:
            updated = await self._repository.record_publication_attempt(
                publication_id,
                expected_attempt_count=attempts,
            )
        except Exception as exc:
            counters["failed_count"] += 1
            errors[f"attempt_record_failed:{type(exc).__name__}"] += 1
            log.warning(
                "Digest attempt could not be recorded publication=%s (%s)",
                publication_id,
                type(exc).__name__,
            )
            return True
        attempt_number = self._attempt_count(updated)
        if attempt_number > self._attempt_limit:
            counters["skipped_count"] += 1
            return True

        media_id = str(media.get("id") or "")
        object_key = str(media.get("storage_object_key") or "")
        expected_sha256 = str(media.get("sha256") or "")
        try:
            data = await self._storage.download_bytes(
                object_key,
                max_bytes=MAX_DIGEST_MEDIA_BYTES,
            )
        except Exception as exc:
            await self._record_failure(
                publication_id,
                f"storage_download_failed:{type(exc).__name__}",
                counters,
                errors,
            )
            return True
        if not data or len(data) > MAX_DIGEST_MEDIA_BYTES:
            await self._record_failure(
                publication_id,
                "invalid_downloaded_size",
                counters,
                errors,
            )
            return True
        actual_sha256 = hashlib.sha256(data).hexdigest()
        if not hmac.compare_digest(actual_sha256, expected_sha256):
            await self._record_failure(
                publication_id,
                "integrity_verification_failed",
                counters,
                errors,
            )
            return True

        source: dict[str, Any] | None = None
        try:
            source = await self._repository.source_for_media(media_id)
        except Exception as exc:
            log.warning(
                "Digest source attribution lookup failed item=%s (%s)",
                media_id,
                type(exc).__name__,
            )
        context = PublicationContext.from_media_record(
            media,
            source,
            publication_marker=publication_marker(publication_id),
        )
        filename = str(media.get("filename") or f"archive.{media.get('extension') or 'bin'}")
        try:
            message_id = await self._publisher.publish(
                filename=filename,
                data=data,
                context=context,
                channel_id=channel_id,
            )
        except Exception as exc:
            await self._record_failure(
                publication_id,
                f"discord_delivery_failed:{type(exc).__name__}",
                counters,
                errors,
            )
            log.warning(
                "Digest delivery failed item=%s publication=%s channel=%s attempt=%s (%s)",
                media_id,
                publication_id,
                channel_id,
                attempt_number,
                type(exc).__name__,
            )
            return True

        try:
            await self._repository.mark_publication_published(
                publication_id,
                int(message_id),
            )
        except Exception as exc:
            counters["stored_count"] += 1
            errors["publication_status_update_failed"] += 1
            log.warning(
                "Digest delivered but publication status update failed publication=%s (%s)",
                publication_id,
                type(exc).__name__,
            )
            return True
        counters["stored_count"] += 1
        log.info(
            "Digest published item=%s publication=%s channel=%s attempt=%s",
            media_id,
            publication_id,
            channel_id,
            attempt_number,
        )
        return True

    async def _record_failure(
        self,
        publication_id: str,
        reason: str,
        counters: dict[str, int],
        errors: Counter[str],
    ) -> None:
        counters["failed_count"] += 1
        errors[reason] += 1
        try:
            await self._repository.mark_publication_failed(publication_id, reason)
        except Exception as exc:
            errors[f"publication_failure_update_failed:{type(exc).__name__}"] += 1
            log.warning(
                "Digest failure could not be recorded publication=%s (%s)",
                publication_id,
                type(exc).__name__,
            )

    async def _reconcile(
        self,
        publication: dict[str, Any],
        *,
        channel_id: int,
        errors: Counter[str],
    ) -> str:
        publication_id = str(publication["id"])
        marker = publication_marker(publication_id)
        find_marker = getattr(self._publisher, "find_publication_marker", None)
        if not callable(find_marker):
            await self._record_reconciliation_error(
                publication_id,
                "reconciliation_unavailable",
                errors,
            )
            return "unreconciled"
        try:
            lookup = await find_marker(channel_id=channel_id, marker=marker)
        except Exception as exc:
            await self._record_reconciliation_error(
                publication_id,
                f"reconciliation_failed:{type(exc).__name__}",
                errors,
            )
            return "unreconciled"

        message_id = getattr(lookup, "message_id", None)
        conclusive = bool(getattr(lookup, "conclusive", False))
        if message_id is not None:
            try:
                await self._repository.mark_publication_published(
                    publication_id,
                    int(message_id),
                )
            except Exception as exc:
                await self._record_reconciliation_error(
                    publication_id,
                    f"reconciliation_update_failed:{type(exc).__name__}",
                    errors,
                )
                return "unreconciled"
            return "reconciled"
        if not conclusive:
            await self._record_reconciliation_error(
                publication_id,
                "delivery_unreconciled",
                errors,
            )
            return "unreconciled"
        return "safe_to_retry"

    async def _record_reconciliation_error(
        self,
        publication_id: str,
        reason: str,
        errors: Counter[str],
    ) -> None:
        errors[reason] += 1
        try:
            await self._repository.record_publication_reconciliation_error(
                publication_id,
                reason,
            )
        except Exception as exc:
            errors[f"reconciliation_error_write_failed:{type(exc).__name__}"] += 1
            log.warning(
                "Digest reconciliation state could not be recorded publication=%s (%s)",
                publication_id,
                type(exc).__name__,
            )

    def _eligible(self, media: dict[str, Any]) -> bool:
        if media.get("verification_status") != "verified":
            return False
        if media.get("download_status") != "stored":
            return False
        sha256 = media.get("sha256")
        if not isinstance(sha256, str) or len(sha256) != 64:
            return False
        try:
            int(sha256, 16)
        except ValueError:
            return False
        object_key = media.get("storage_object_key")
        if not isinstance(object_key, str) or not object_key:
            return False
        try:
            size_bytes = int(media.get("size_bytes") or 0)
        except (TypeError, ValueError):
            return False
        return 1 <= size_bytes <= MAX_DIGEST_MEDIA_BYTES

    def _destination_channel(self, media: dict[str, Any]) -> int:
        category = str(media.get("category") or "").strip().lower()
        mappings = getattr(self._settings, "category_channels", None) or {}
        mapped = mappings.get(category) if isinstance(mappings, dict) else None
        if mapped:
            try:
                return int(mapped)
            except (TypeError, ValueError):
                pass
        fallback = getattr(self._settings, "discoveries_channel_id", None) or 0
        try:
            return int(fallback)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _attempt_count(publication: dict[str, Any]) -> int:
        try:
            return int(publication.get("attempt_count") or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _summarize(errors: Counter[str]) -> str | None:
        if not errors:
            return None
        return "; ".join(f"{category}: {count}" for category, count in sorted(errors.items()))[:500]
