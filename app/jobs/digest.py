"""Restart-safe daily digest delivery for verified archived media."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.bot.publisher import DiscordPublicationRejectedError, PublicationContext
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
_AMBIGUOUS_DELIVERY_PREFIXES = (
    "discord_delivery_ambiguous:",
    # Older records used this prefix for all Discord send exceptions.
    "discord_delivery_failed",
)


def _is_ambiguous_delivery_error(error: Any) -> bool:
    return isinstance(error, str) and error.startswith(_AMBIGUOUS_DELIVERY_PREFIXES)


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
    cycle: str


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
        self._quota_exhausted = False

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
            self._quota_exhausted = False
            processed: set[tuple[str, int]] = set()
            unaccounted: list[str] = []
            await self._process_candidates(window, processed, counters, errors, unaccounted)
            await self._process_retryable(window.cycle, processed, counters, errors, unaccounted)
            await self._process_exhausted_ambiguous(processed, counters, errors)
            if self._quota_exhausted:
                errors["global_daily_limit_reached"] += 1
                outcome = "partial"
            elif unaccounted:
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
        now = self._now().astimezone(UTC)
        due = latest_digest_due_at(now)
        cycle = due.astimezone(ZoneInfo(DIGEST_TIMEZONE)).date().isoformat()

        if start is not None and end is not None and (completed is None or completed < end):
            previous_cycle = str(state.get("digest_cycle") or cycle)
            if previous_cycle < cycle:
                # A full prior-cycle window carries its oldest remaining records
                # into the newly scheduled cycle without resetting on restart.
                previous_cycle = cycle
                end = now if now > end else end
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
            await self._repository.update_digest_state(
                {"digest_cycle": previous_cycle, "last_window_end": _iso(end)}
            )
            return DigestWindow(
                start=start, end=end, cursor=cursor, resumed=True, cycle=previous_cycle
            )

        window_start = end if end is not None else INITIAL_DIGEST_WINDOW_START
        window_end = now
        if window_end < window_start:
            window_end = window_start
        window = DigestWindow(
            start=window_start,
            end=window_end,
            cursor=window_start,
            resumed=False,
            cycle=cycle,
        )
        await self._repository.update_digest_state(
            {
                "last_window_start": _iso(window.start),
                "last_window_end": _iso(window.end),
                "cursor_media_created_at": _iso(window.cursor),
                "digest_cycle": window.cycle,
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
            page_cursor = persisted_cursor
            for media in rows:
                accounted = await self._process_candidate(
                    media,
                    window,
                    processed,
                    counters,
                    errors,
                    unaccounted,
                )
                if not accounted:
                    if self._quota_exhausted:
                        break
                    cursor_writable = False
                else:
                    created_at = _as_datetime(media.get("created_at"))
                    if created_at is not None and created_at > page_cursor:
                        page_cursor = created_at
            persisted_cursor = page_cursor
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
            if self._quota_exhausted:
                return
            if len(rows) < self._page_size:
                return

    async def _process_candidate(
        self,
        media: dict[str, Any],
        window: DigestWindow,
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
            reserve = getattr(self._repository, "reserve_digest_publication", None)
            if callable(reserve):
                publication = await reserve(media_id, channel_id, window.cycle, limit=50)
                if publication.get("quota_exhausted"):
                    self._quota_exhausted = True
                    return False
            else:
                publication = await self._repository.ensure_digest_publication(
                    media_id,
                    channel_id,
                )
            try:
                channel_id = int(publication.get("channel_id") or channel_id)
            except (TypeError, ValueError):
                pass
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
            cycle=window.cycle,
            counters=counters,
            errors=errors,
            unaccounted=unaccounted,
        )

    async def _process_retryable(
        self,
        cycle: str,
        processed: set[tuple[str, int]],
        counters: dict[str, int],
        errors: Counter[str],
        unaccounted: list[str],
    ) -> None:
        try:
            rows = await self._repository.list_retryable_digest_publications(
                attempt_limit=self._attempt_limit,
                limit=self._retry_batch_size,
                now=_iso(self._now()),
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
            if any(processed_media_id == media_id for processed_media_id, _ in processed):
                continue
            reserve = getattr(self._repository, "reserve_digest_publication", None)
            if callable(reserve):
                try:
                    publication = await reserve(media_id, channel_id, cycle, limit=50)
                except Exception as exc:
                    errors[f"retry_reservation_failed:{type(exc).__name__}"] += 1
                    continue
                if publication.get("quota_exhausted"):
                    self._quota_exhausted = True
                    break
                try:
                    channel_id = int(publication.get("channel_id") or channel_id)
                except (TypeError, ValueError):
                    pass
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
                cycle=cycle,
                counters=counters,
                errors=errors,
                unaccounted=unaccounted,
            )

    async def _process_exhausted_ambiguous(
        self,
        processed: set[tuple[str, int]],
        counters: dict[str, int],
        errors: Counter[str],
    ) -> None:
        """Reconcile exhausted ambiguous sends without granting delivery permission."""
        try:
            rows = await self._repository.list_exhausted_ambiguous_digest_publications(
                attempt_limit=self._attempt_limit,
                page_size=self._retry_batch_size,
                now=_iso(self._now()),
            )
        except Exception as exc:
            errors[f"reconciliation_queue_lookup_failed:{type(exc).__name__}"] += 1
            log.warning("Digest reconciliation queue lookup failed (%s)", type(exc).__name__)
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
            claim_token = str(uuid.uuid4())
            try:
                claim = await self._claim(publication, claim_token)
            except Exception as exc:
                errors[f"reconciliation_claim_failed:{type(exc).__name__}"] += 1
                continue
            if not claim:
                continue
            reconciliation = await self._reconcile(
                publication,
                channel_id=channel_id,
                errors=errors,
                claim_token=claim_token,
            )
            if reconciliation == "reconciled":
                counters["skipped_count"] += 1
                continue
            if reconciliation.startswith("unreconciled"):
                counters["failed_count"] += 1
                await self._finish_reconciliation(
                    publication,
                    claim_token,
                    reconciliation.partition(":")[2] or "delivery_unreconciled",
                    errors,
                )
                continue

            # A conclusive miss means this record is no longer ambiguous, but it
            # has exhausted its send budget and must not be retried.
            counters["skipped_count"] += 1
            try:
                await self._repository.finish_digest_failure(
                    str(publication["id"]),
                    claim_token,
                    "delivery_attempt_limit_reached:no_marker_found",
                )
            except Exception as exc:
                errors[f"exhausted_publication_update_failed:{type(exc).__name__}"] += 1
                log.warning(
                    "Digest exhausted publication state could not be updated publication=%s (%s)",
                    publication["id"],
                    type(exc).__name__,
                )

    async def _deliver(
        self,
        media: dict[str, Any],
        publication: dict[str, Any],
        *,
        channel_id: int,
        cycle: str,
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
        if str(publication.get("status") or "pending") == "published":
            counters["skipped_count"] += 1
            return True
        claim_token = str(uuid.uuid4())
        try:
            claimed = await self._claim(publication, claim_token)
        except Exception as exc:
            counters["failed_count"] += 1
            errors[f"delivery_claim_failed:{type(exc).__name__}"] += 1
            return True
        if not claimed:
            counters["skipped_count"] += 1
            return True
        publication = claimed
        attempts = self._attempt_count(publication)
        status = str(publication.get("status") or "pending")
        last_error = str(publication.get("last_error") or "")
        ambiguous_failure = _is_ambiguous_delivery_error(last_error)
        if attempts > 0 and (status == "pending" or ambiguous_failure):
            reconciliation = await self._reconcile(
                publication,
                channel_id=channel_id,
                errors=errors,
                claim_token=claim_token,
            )
            if reconciliation == "reconciled":
                counters["skipped_count"] += 1
                log.info(
                    "Digest reconciled existing publication=%s channel=%s",
                    publication_id,
                    channel_id,
                )
                return True
            if reconciliation.startswith("unreconciled"):
                counters["failed_count"] += 1
                await self._finish_reconciliation(
                    publication,
                    claim_token,
                    reconciliation.partition(":")[2] or "delivery_unreconciled",
                    errors,
                )
                log.warning(
                    "Digest held ambiguous publication=%s channel=%s",
                    publication_id,
                    channel_id,
                )
                return True
            # A conclusive miss retires the ambiguous hold. Any resend in a
            # newer cycle must acquire that cycle's slot before it can proceed.
            try:
                rollover = await self._repository.rollover_reconciled_digest_publication(
                    publication_id, claim_token, cycle, limit=50
                )
                if not rollover.get("claim_valid"):
                    counters["skipped_count"] += 1
                    return True
                if rollover.get("quota_exhausted"):
                    self._quota_exhausted = True
                    counters["skipped_count"] += 1
                    return True
                publication = rollover
                attempts = self._attempt_count(publication)
                last_error = "delivery_reconciled_no_marker"
                ambiguous_failure = False
            except Exception as exc:
                counters["failed_count"] += 1
                errors[f"retry_reservation_failed:{type(exc).__name__}"] += 1
                return True

        if attempts >= self._attempt_limit:
            if not _is_ambiguous_delivery_error(last_error):
                try:
                    await self._repository.mark_publication_failed(
                        publication_id, "delivery_attempt_limit_reached"
                    )
                except Exception as exc:
                    errors[f"publication_failure_update_failed:{type(exc).__name__}"] += 1
            counters["skipped_count"] += 1
            log.info(
                "Digest publication exhausted publication=%s attempts=%s",
                publication_id,
                attempts,
            )
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
                publication,
                claim_token,
                f"storage_download_failed:{type(exc).__name__}",
                counters,
                errors,
            )
            return True
        if not data or len(data) > MAX_DIGEST_MEDIA_BYTES:
            await self._record_failure(
                publication, claim_token, "invalid_downloaded_size", counters, errors
            )
            return True
        actual_sha256 = hashlib.sha256(data).hexdigest()
        if not hmac.compare_digest(actual_sha256, expected_sha256):
            await self._record_failure(
                publication, claim_token, "integrity_verification_failed", counters, errors
            )
            return True

        source: list[dict[str, Any]] = []
        try:
            list_sources = getattr(self._repository, "sources_for_media", None)
            if callable(list_sources):
                source = await list_sources(media_id)
            else:
                legacy_source = await self._repository.source_for_media(media_id)
                source = [legacy_source] if legacy_source else []
        except Exception as exc:
            await self._record_failure(
                publication,
                claim_token,
                f"attribution_lookup_failed:{type(exc).__name__}",
                counters,
                errors,
            )
            log.warning(
                "Digest source attribution lookup failed item=%s (%s)",
                media_id,
                type(exc).__name__,
            )
            return True
        context = PublicationContext.from_media_record(
            media,
            source,
            publication_marker=publication_marker(publication_id),
        )
        filename = str(media.get("filename") or f"archive.{media.get('extension') or 'bin'}")
        try:
            updated = await self._repository.advance_digest_attempt(
                publication_id,
                claim_token,
                expected_attempt_count=attempts,
                attempt_limit=self._attempt_limit,
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
        if not updated.get("attempt_advanced"):
            counters["skipped_count"] += 1
            return True
        attempt_number = self._attempt_count(updated)
        try:
            message_id = await self._publisher.publish(
                filename=filename,
                data=data,
                context=context,
                channel_id=channel_id,
            )
        except Exception as exc:
            definite_rejection = isinstance(exc, DiscordPublicationRejectedError)
            error_kind = "rejected" if definite_rejection else "ambiguous"
            await self._record_failure(
                publication,
                claim_token,
                f"discord_delivery_{error_kind}:{type(exc).__name__}",
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
            if not await self._repository.publish_digest_claimed(
                publication_id, claim_token, int(message_id)
            ):
                errors["publication_claim_lost_after_delivery"] += 1
                counters["stored_count"] += 1
                return True
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
        publication: dict[str, Any],
        claim_token: str,
        reason: str,
        counters: dict[str, int],
        errors: Counter[str],
    ) -> None:
        counters["failed_count"] += 1
        errors[reason] += 1
        try:
            await self._repository.finish_digest_failure(
                str(publication["id"]), claim_token, reason
            )
        except Exception as exc:
            errors[f"publication_failure_update_failed:{type(exc).__name__}"] += 1
            log.warning(
                "Digest failure could not be recorded publication=%s (%s)",
                publication.get("id"),
                type(exc).__name__,
            )

    async def _claim(self, publication: dict[str, Any], claim_token: str) -> dict[str, Any] | None:
        acquire = getattr(self._repository, "claim_digest_publication", None)
        if not callable(acquire):
            return None
        claimed = await acquire(str(publication["id"]), claim_token)
        if not claimed.get("claim_acquired"):
            return None
        return claimed

    async def _finish_reconciliation(
        self,
        publication: dict[str, Any],
        claim_token: str,
        reason: str,
        errors: Counter[str],
    ) -> None:
        try:
            await self._repository.finish_digest_reconciliation(
                str(publication["id"]), claim_token, reason
            )
        except Exception as exc:
            errors[f"reconciliation_state_write_failed:{type(exc).__name__}"] += 1

    async def _reconcile(
        self,
        publication: dict[str, Any],
        *,
        channel_id: int,
        errors: Counter[str],
        claim_token: str,
    ) -> str:
        publication_id = str(publication["id"])
        marker = publication_marker(publication_id)
        find_marker = getattr(self._publisher, "find_publication_marker", None)
        if not callable(find_marker):
            errors["reconciliation_unavailable"] += 1
            return "unreconciled:reconciliation_unavailable"
        try:
            lookup = await find_marker(channel_id=channel_id, marker=marker)
        except Exception as exc:
            reason = f"reconciliation_failed:{type(exc).__name__}"
            errors[reason] += 1
            return f"unreconciled:{reason}"

        message_id = getattr(lookup, "message_id", None)
        conclusive = bool(getattr(lookup, "conclusive", False))
        if message_id is not None:
            try:
                updated = await self._repository.publish_digest_claimed(
                    publication_id, claim_token, int(message_id)
                )
                if not updated:
                    return "unreconciled:reconciliation_claim_lost"
            except Exception as exc:
                reason = f"reconciliation_update_failed:{type(exc).__name__}"
                errors[reason] += 1
                return f"unreconciled:{reason}"
            return "reconciled"
        if not conclusive:
            errors["delivery_unreconciled"] += 1
            return "unreconciled:delivery_unreconciled"
        return "safe_to_retry"

    def _eligible(self, media: dict[str, Any]) -> bool:
        if str(media.get("media_type") or "").strip().lower() not in {
            "image",
            "animation",
            "video",
        }:
            return False
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
        media_type = str(media.get("media_type") or "").strip().lower()
        mappings = getattr(self._settings, "category_channels", None) or {}
        mapped = mappings.get(media_type) if isinstance(mappings, dict) else None
        if mapped:
            try:
                return int(mapped)
            except (TypeError, ValueError):
                pass
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
