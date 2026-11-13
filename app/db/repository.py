"""Supabase repository methods used by the Phase 2 media pipeline."""

from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from app.db.client import SupabaseDatabase
from app.discovery.domains import domain_identity, identity_from_row
from app.reddit.models import RedditCandidate

log = logging.getLogger(__name__)

# Keep simultaneous PostgREST work bounded across overlapping scheduled jobs.
# The bot currently has at most a handful of jobs sharing one SDK client.
_MAX_DB_ATTEMPTS = 3
_RETRY_BASE_DELAY_SECONDS = 0.5
_TRANSIENT_DB_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.ReadTimeout,
)

_DEFAULT_DISCOVERY_BATCH_SIZE = 25
_DEFAULT_DISCOVERY_EDGE_BATCH_SIZE = 50
_DEFAULT_RUNTIME_STATE_CACHE_TTL_SECONDS = 3.0
_MAX_BATCH_SIZE = 200
_MAX_RUNTIME_STATE_CACHE_TTL_SECONDS = 60.0
_DISCOVERY_EDGE_RPC = "record_discovery_edges"

_JOB_TYPES = ("reddit_collection", "bing_discovery", "crawl", "daily_digest")
_DIGEST_STATE_FIELDS = frozenset(
    {
        "last_window_start",
        "last_window_end",
        "last_completed_at",
        "cursor_media_created_at",
        "digest_cycle",
    }
)
_DISCOVERY_STATUSES = frozenset(
    {"queued", "processing", "verified", "dumpster", "rejected", "blocked", "failed", "oversize"}
)
_DISCOVERY_UPDATE_FIELDS = frozenset(
    {
        "status",
        "http_status",
        "retry_count",
        "last_error",
        "fetched_at",
        "verified_at",
        "next_attempt_at",
        "metadata_json",
        "title",
        "description",
        "creator_name",
        "creator_profile_url",
        "domain",
        "hostname",
        "registrable_domain",
        "crawl_priority",
        "claimed_at",
    }
)


class RepositoryError(RuntimeError):
    """Raised when a repository operation cannot be completed."""


def _operation_name(function: Callable[..., Any]) -> str:
    name = getattr(function, "__name__", "database_operation")
    return name.removeprefix("_").removesuffix("_sync")


def _is_unique_violation(exc: Exception) -> bool:
    code = getattr(exc, "code", None)
    if str(code) == "23505":
        return True
    for value in (str(exc), getattr(exc, "message", None), getattr(exc, "details", None)):
        if not value:
            continue
        text = str(value).lower()
        if "23505" in text or "duplicate key" in text or "unique constraint" in text:
            return True
    return False


class MediaRepository:
    """Keep synchronous Supabase SDK calls off the asyncio event loop."""

    def __init__(
        self,
        database: SupabaseDatabase,
        *,
        to_thread: Callable[..., Awaitable[Any]] = asyncio.to_thread,
        retry_sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        discovery_batch_size: int = _DEFAULT_DISCOVERY_BATCH_SIZE,
        discovery_edge_batch_size: int = _DEFAULT_DISCOVERY_EDGE_BATCH_SIZE,
        runtime_state_cache_ttl_seconds: float = _DEFAULT_RUNTIME_STATE_CACHE_TTL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._database = database
        self._to_thread = to_thread
        self._retry_sleep = retry_sleep
        self._jitter = jitter
        self._discovery_batch_size = max(1, min(_MAX_BATCH_SIZE, int(discovery_batch_size)))
        self._discovery_edge_batch_size = max(
            1, min(_MAX_BATCH_SIZE, int(discovery_edge_batch_size))
        )
        self._runtime_state_cache_ttl_seconds = max(
            0.0, min(_MAX_RUNTIME_STATE_CACHE_TTL_SECONDS, float(runtime_state_cache_ttl_seconds))
        )
        self._monotonic = monotonic
        self._runtime_state_cache: dict[str, Any] | None = None
        self._runtime_state_cache_expires_at = 0.0
        self._runtime_state_generation = 0
        self._metrics: Counter[str] = Counter()
        self._metrics_lock = threading.Lock()

    def _bump_metrics(self, **increments: int) -> None:
        with self._metrics_lock:
            for name, value in increments.items():
                if value:
                    self._metrics[name] += value

    def metrics_snapshot(self) -> dict[str, int]:
        """Return a copy of lightweight database-operation counters."""
        with self._metrics_lock:
            return dict(self._metrics)

    async def _run(self, function: Callable[..., Any], *args: Any, retry: bool = False) -> Any:
        """Dispatch one database operation under the shared concurrency limit.

        Retries are opt-in per repository operation. The caller must only enable
        them when repeating the whole operation is idempotent or already guarded
        by a unique constraint and conflict-recovery path.
        """
        operation = _operation_name(function)
        attempts = _MAX_DB_ATTEMPTS if retry else 1
        for attempt in range(1, attempts + 1):
            try:
                result = await self._database.run_sync(function, *args, to_thread=self._to_thread)
            except _TRANSIENT_DB_EXCEPTIONS as exc:
                if attempt >= attempts:
                    log.error(
                        "Database operation failed operation=%s attempt=%s/%s "
                        "exception=%s retry=false",
                        operation,
                        attempt,
                        attempts,
                        type(exc).__name__,
                    )
                    raise
                delay = _RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
                delay += self._jitter(0.0, delay * 0.1)
                log.warning(
                    "Transient database transport failure operation=%s attempt=%s/%s "
                    "exception=%s retry=true retry_in_seconds=%.2f",
                    operation,
                    attempt,
                    attempts,
                    type(exc).__name__,
                    delay,
                )
                await self._retry_sleep(delay)
            else:
                if attempt > 1:
                    log.info(
                        "Database operation succeeded after retry operation=%s attempt=%s/%s",
                        operation,
                        attempt,
                        attempts,
                    )
                return result
        raise AssertionError("unreachable")  # pragma: no cover

    @property
    def configured(self) -> bool:
        return self._database.configured

    def _client(self) -> Any:
        if not self._database.configured:
            raise RepositoryError("Supabase is not configured")
        try:
            self._database.initialize()
            return self._database.client
        except Exception as exc:
            raise RepositoryError(f"Supabase client unavailable ({type(exc).__name__})") from None

    async def get_runtime_state(self) -> dict[str, Any]:
        cached = self._cached_runtime_state()
        if cached is not None:
            self._bump_metrics(runtime_state_cache_hits=1)
            return dict(cached)

        self._bump_metrics(runtime_state_cache_misses=1, runtime_state_reads=1)
        generation = self._runtime_state_generation
        state = await self._run(self._runtime_state_sync, retry=True)
        snapshot = dict(state)
        # Do not let a read that raced with a local /start or /stop overwrite
        # the fresher state persisted by that command.
        if generation == self._runtime_state_generation:
            self._cache_runtime_state(snapshot)
        return dict(snapshot)

    def _cached_runtime_state(self) -> dict[str, Any] | None:
        if self._runtime_state_cache is None:
            return None
        if self._monotonic() >= self._runtime_state_cache_expires_at:
            return None
        return self._runtime_state_cache

    def _cache_runtime_state(self, state: dict[str, Any]) -> None:
        self._runtime_state_cache = dict(state)
        self._runtime_state_cache_expires_at = (
            self._monotonic() + self._runtime_state_cache_ttl_seconds
        )

    def _invalidate_runtime_state_cache(self) -> None:
        self._runtime_state_cache = None
        self._runtime_state_cache_expires_at = 0.0
        self._runtime_state_generation += 1
        self._bump_metrics(runtime_state_cache_invalidations=1)

    def _runtime_state_sync(self) -> dict[str, Any]:
        rows = self._rows(
            self._client().table("runtime_state").select("*").eq("id", 1).limit(1).execute()
        )
        return rows[0] if rows else {"id": 1, "collection_enabled": False}

    async def set_collection_enabled(self, enabled: bool) -> dict[str, Any]:
        state = await self._run(self._set_collection_enabled_sync, enabled, retry=True)
        # The write already returned the persisted row. Invalidate any older
        # snapshot and seed the cache immediately so the next crawler check
        # observes the owner's command without another database read.
        self._invalidate_runtime_state_cache()
        self._bump_metrics(runtime_state_cache_refreshes=1)
        self._cache_runtime_state(state)
        return dict(state)

    def _set_collection_enabled_sync(self, enabled: bool) -> dict[str, Any]:
        rows = self._rows(
            self._client()
            .table("runtime_state")
            .upsert(
                {
                    "id": 1,
                    "collection_enabled": enabled,
                    "collection_changed_at": datetime.now(tz=UTC).isoformat(),
                },
                on_conflict="id",
            )
            .execute()
        )
        return rows[0] if rows else self._runtime_state_sync()

    async def create_job_run(
        self, config: dict[str, Any], job_type: str = "reddit_collection"
    ) -> str:
        if job_type not in _JOB_TYPES:
            raise ValueError(f"Unsupported job type: {job_type}")
        # A plain insert without a unique job identifier is not safe to repeat.
        return await self._run(self._create_job_run_sync, config, job_type, retry=False)

    def _create_job_run_sync(self, config: dict[str, Any], job_type: str) -> str:
        rows = self._rows(
            self._client()
            .table("job_runs")
            .insert({"job_type": job_type, "outcome": "running", "config_snapshot": config})
            .execute()
        )
        if not rows:
            raise RepositoryError("Job run insert returned no row")
        return str(rows[0]["id"])

    async def finish_job_run(
        self, job_id: str, counters: dict[str, Any], outcome: str, error: str | None = None
    ) -> None:
        await self._run(self._finish_job_run_sync, job_id, counters, outcome, error, retry=True)

    def _finish_job_run_sync(
        self, job_id: str, counters: dict[str, Any], outcome: str, error: str | None
    ) -> None:
        payload = {
            **counters,
            "outcome": outcome,
            "error_summary": error,
            "finished_at": datetime.now(tz=UTC).isoformat(),
        }
        self._client().table("job_runs").update(payload).eq("id", job_id).execute()

    async def mark_interrupted_jobs(self) -> None:
        await self._run(self._mark_interrupted_sync, retry=True)

    def _mark_interrupted_sync(self) -> None:
        client = self._client()
        for job_type in _JOB_TYPES:
            rows = self._rows(
                client.table("job_runs")
                .select("id")
                .eq("job_type", job_type)
                .eq("outcome", "running")
                .execute()
            )
            for row in rows:
                client.table("job_runs").update(
                    {
                        "outcome": "failed",
                        "error_summary": "interrupted_by_restart",
                        "finished_at": datetime.now(tz=UTC).isoformat(),
                    }
                ).eq("id", row["id"]).execute()

    async def recover_processing_discoveries(self) -> int:
        return await self._run(self._recover_processing_discoveries_sync, retry=True)

    def _recover_processing_discoveries_sync(self) -> int:
        result = (
            self._client().rpc("recover_expired_crawl_claims", {"p_lease_seconds": 900}).execute()
        )
        data = getattr(result, "data", None)
        if isinstance(data, int):
            return data
        if isinstance(data, dict):
            return int(data.get("recovered_count") or 0)
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return int(data[0].get("recovered_count") or 0)
        return 0

    async def backfill_frontier_domains(self, *, batch_size: int = 500) -> int:
        """Backfill PSL identities for legacy queued and processing discoveries."""
        return await self._run(self._backfill_frontier_domains_sync, batch_size, retry=True)

    def _backfill_frontier_domains_sync(self, batch_size: int) -> int:
        client = self._client()
        size = max(1, min(1000, batch_size))
        updated = 0
        while True:
            rows = self._rows(
                client.table("discoveries")
                .select("id,normalized_url,domain,discovered_at,status,fetched_at")
                .is_("registrable_domain", "null")
                .range(0, size - 1)
                .execute()
            )
            if not rows:
                break
            identities = []
            for row in rows:
                raw_url = row.get("normalized_url") or row.get("url")
                try:
                    if not isinstance(raw_url, str):
                        raise ValueError("discovery URL missing")
                    identity = domain_identity(raw_url)
                except ValueError:
                    identity = identity_from_row(row)
                if identity.hostname == "unknown":
                    continue
                identities.append(
                    {
                        "id": row.get("id"),
                        "hostname": identity.hostname,
                        "registrable_domain": identity.registrable_domain,
                    }
                )
            if not identities:
                break
            result = client.rpc(
                "backfill_crawl_domain_identities", {"p_items": identities}
            ).execute()
            data = getattr(result, "data", None)
            if isinstance(data, int):
                updated += data
            elif isinstance(data, dict):
                updated += int(data.get("updated_count") or len(identities))
            else:
                updated += len(identities)
            if len(rows) < size:
                break
        return updated

    async def latest_jobs(self, job_type: str, limit: int = 1) -> list[dict[str, Any]]:
        return await self._run(self._latest_jobs_sync, job_type, limit, retry=True)

    def _latest_jobs_sync(self, job_type: str, limit: int) -> list[dict[str, Any]]:
        return self._rows(
            self._client()
            .table("job_runs")
            .select("*")
            .eq("job_type", job_type)
            .order("started_at", desc=True)
            .limit(max(1, limit))
            .execute()
        )

    async def latest_bing_runs(self, limit: int = 1) -> list[dict[str, Any]]:
        return await self.latest_jobs("bing_discovery", limit)

    async def latest_crawl_runs(self, limit: int = 1) -> list[dict[str, Any]]:
        return await self.latest_jobs("crawl", limit)

    async def latest_reddit_runs(self, limit: int = 2) -> list[dict[str, Any]]:
        return await self._run(self._latest_reddit_runs_sync, limit, retry=True)

    async def operational_counts(self) -> dict[str, Any]:
        return await self._run(self._operational_counts_sync, retry=True)

    def _operational_counts_sync(self) -> dict[str, Any]:
        client = self._client()

        def count_status(status: str) -> int:
            response = (
                client.table("discoveries")
                .select("id", count="exact")
                .eq("status", status)
                .limit(1)
                .execute()
            )
            return int(getattr(response, "count", None) or 0)

        state = self._runtime_state_sync()
        return {
            "queued": count_status("queued"),
            "processing": count_status("processing"),
            "verified": count_status("verified"),
            "dumpster": count_status("dumpster"),
            "rejected": count_status("rejected"),
            "blocked": count_status("blocked"),
            "failed": count_status("failed"),
            "oversize": count_status("oversize"),
            "last_digest_success_at": state.get("last_digest_success_at"),
            "last_bing_success_at": state.get("last_bing_success_at"),
            "bing_paused": bool(state.get("bing_paused")),
            "bing_pause_reason": state.get("bing_pause_reason"),
        }

    async def crawl_frontier_summary(self, *, max_depth: int = 6) -> dict[str, int]:
        return await self._run(self._crawl_frontier_summary_sync, max_depth, retry=True)

    def _crawl_frontier_summary_sync(self, max_depth: int) -> dict[str, int]:
        response = (
            self._client()
            .rpc("crawl_frontier_summary", {"p_max_depth": max(0, min(6, int(max_depth)))})
            .execute()
        )
        data = getattr(response, "data", None)
        if isinstance(data, list) and data and isinstance(data[0], dict):
            data = data[0]
        if not isinstance(data, dict):
            return {}
        return {key: int(value or 0) for key, value in data.items()}

    async def get_robots_policy(self, origin: str) -> dict[str, Any] | None:
        return await self._run(self._get_robots_policy_sync, origin, retry=True)

    def _get_robots_policy_sync(self, origin: str) -> dict[str, Any] | None:
        rows = self._rows(
            self._client()
            .table("crawl_origin_state")
            .select("robots_body,robots_status,robots_category,robots_expires_at")
            .eq("origin", origin)
            .limit(1)
            .execute()
        )
        return rows[0] if rows else None

    async def store_robots_policy(
        self, origin: str, hostname: str, registrable_domain: str, policy: dict[str, Any]
    ) -> None:
        await self._run(
            self._store_robots_policy_sync,
            origin,
            hostname,
            registrable_domain,
            dict(policy),
            retry=True,
        )

    def _store_robots_policy_sync(
        self, origin: str, hostname: str, registrable_domain: str, policy: dict[str, Any]
    ) -> None:
        client = self._client()
        # Redirect targets can introduce an origin before its registrable domain
        # has a frontier row. The origin policy table references that row, so
        # register the minimum identity idempotently before persisting robots.
        client.table("crawl_domain_state").upsert(
            {
                "registrable_domain": registrable_domain,
                "hostnames": [hostname],
            },
            on_conflict="registrable_domain",
            ignore_duplicates=True,
        ).execute()
        payload = {
            "origin": origin,
            "hostname": hostname,
            "registrable_domain": registrable_domain,
            **policy,
        }
        client.table("crawl_origin_state").upsert(payload, on_conflict="origin").execute()

    async def pause_crawl_domain(self, registrable_domain: str, *, until: str, reason: str) -> None:
        await self._run(
            self._pause_crawl_domain_sync, registrable_domain, until, reason, retry=True
        )

    def _pause_crawl_domain_sync(self, registrable_domain: str, until: str, reason: str) -> None:
        self._client().rpc(
            "pause_crawl_domain",
            {
                "p_registrable_domain": registrable_domain,
                "p_next_eligible_at": until,
                "p_reason": reason[:120],
            },
        ).execute()

    async def sitemap_scan_is_due(self, registrable_domain: str, *, max_age_days: int = 30) -> bool:
        return await self._run(
            self._sitemap_scan_is_due_sync, registrable_domain, max_age_days, retry=True
        )

    def _sitemap_scan_is_due_sync(self, registrable_domain: str, max_age_days: int) -> bool:
        rows = self._rows(
            self._client()
            .table("crawl_domain_state")
            .select("sitemap_checked_at")
            .eq("registrable_domain", registrable_domain)
            .limit(1)
            .execute()
        )
        value = rows[0].get("sitemap_checked_at") if rows else None
        if not value:
            return True
        try:
            checked = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return True
        return (datetime.now(tz=UTC) - checked).total_seconds() >= max_age_days * 86400

    async def mark_sitemap_scan(self, registrable_domain: str) -> None:
        await self._run(self._mark_sitemap_scan_sync, registrable_domain, retry=True)

    def _mark_sitemap_scan_sync(self, registrable_domain: str) -> None:
        self._client().table("crawl_domain_state").update(
            {"sitemap_checked_at": datetime.now(tz=UTC).isoformat()}
        ).eq("registrable_domain", registrable_domain).execute()

    async def mark_reddit_success(self) -> None:
        await self._run(self._mark_reddit_success_sync, retry=True)
        self._invalidate_runtime_state_cache()

    def _mark_reddit_success_sync(self) -> None:
        self._client().table("runtime_state").update(
            {"last_reddit_success_at": datetime.now(tz=UTC).isoformat()}
        ).eq("id", 1).execute()

    async def mark_bing_success(self) -> None:
        await self._run(self._mark_bing_success_sync, retry=True)
        self._invalidate_runtime_state_cache()

    def _mark_bing_success_sync(self) -> None:
        self._client().table("runtime_state").update(
            {"last_bing_success_at": datetime.now(tz=UTC).isoformat()}
        ).eq("id", 1).execute()

    async def pause_bing(self, reason: str) -> None:
        await self._run(self._pause_bing_sync, reason, retry=True)
        self._invalidate_runtime_state_cache()

    def _pause_bing_sync(self, reason: str) -> None:
        self._client().table("runtime_state").update(
            {
                "bing_paused": True,
                "bing_pause_reason": reason[:500],
                "bing_paused_at": datetime.now(tz=UTC).isoformat(),
            }
        ).eq("id", 1).execute()

    async def clear_bing_pause(self) -> None:
        """Clear the persisted pause after an owner reviews the restriction.

        No Discord command calls this. It exists for a deliberate operator
        action and never runs automatically on startup or from ``/start``.
        """
        await self._run(self._clear_bing_pause_sync, retry=True)
        self._invalidate_runtime_state_cache()

    def _clear_bing_pause_sync(self) -> None:
        self._client().table("runtime_state").update(
            {
                "bing_paused": False,
                "bing_pause_reason": None,
                "bing_paused_at": None,
            }
        ).eq("id", 1).execute()

    def _latest_reddit_runs_sync(self, limit: int) -> list[dict[str, Any]]:
        return self._rows(
            self._client()
            .table("job_runs")
            .select("*")
            .eq("job_type", "reddit_collection")
            .order("started_at", desc=True)
            .limit(limit)
            .execute()
        )

    async def random_asset(self) -> dict[str, Any] | None:
        return await self._run(self._random_asset_sync, retry=True)

    def _random_asset_sync(self) -> dict[str, Any] | None:
        rows = self._rows(
            self._client()
            .table("media_items")
            .select("*")
            .eq("verification_status", "verified")
            .eq("download_status", "stored")
            .limit(500)
            .execute()
        )
        import random

        return random.choice(rows) if rows else None

    async def source_for_media(self, media_item_id: str) -> dict[str, Any] | None:
        return await self._run(self._source_for_media_sync, media_item_id, retry=True)

    def _source_for_media_sync(self, media_item_id: str) -> dict[str, Any] | None:
        rows = self._rows(
            self._client()
            .table("media_sources")
            .select("*")
            .eq("media_item_id", media_item_id)
            .order("discovered_at")
            .limit(1)
            .execute()
        )
        return rows[0] if rows else None

    async def sources_for_media(self, media_item_id: str) -> list[dict[str, Any]]:
        """Return every source association for one canonical media item."""
        return await self._run(self._sources_for_media_sync, media_item_id, retry=True)

    def _sources_for_media_sync(self, media_item_id: str) -> list[dict[str, Any]]:
        size = 1000
        offset = 0
        results: list[dict[str, Any]] = []
        while True:
            page = self._rows(
                self._client()
                .table("media_sources")
                .select("*")
                .eq("media_item_id", media_item_id)
                .order("discovered_at")
                .order("id")
                .range(offset, offset + size - 1)
                .execute()
            )
            results.extend(page)
            if len(page) < size:
                return results
            offset += len(page)

    async def asset_stats(self, start: str | None = None) -> dict[str, int]:
        return await self._run(self._asset_stats_sync, start, retry=True)

    def _asset_stats_sync(self, start: str | None) -> dict[str, int]:
        client = self._client()

        def count_assets(media_type: str | None = None) -> int:
            query = (
                client.table("media_items")
                .select("id", count="exact")
                .eq("verification_status", "verified")
                .eq("download_status", "stored")
            )
            if start:
                query = query.gte("created_at", start)
            if media_type:
                query = query.eq("media_type", media_type)
            response = query.limit(1).execute()
            return int(getattr(response, "count", None) or 0)

        pubs = client.table("publications").select("id", count="exact").eq("status", "published")
        if start:
            pubs = pubs.gte("published_at", start)
        publication_response = pubs.limit(1).execute()
        return {
            "total": count_assets(),
            "image": count_assets("image"),
            "animation": count_assets("animation"),
            "video": count_assets("video"),
            "delivered": int(getattr(publication_response, "count", None) or 0),
        }

    @staticmethod
    def _rows(response: Any) -> list[dict[str, Any]]:
        data = getattr(response, "data", None)
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        return []

    async def get_media_by_sha256(self, sha256: str) -> dict[str, Any] | None:
        return await self._run(self._get_media_by_sha256_sync, sha256, retry=True)

    def _get_media_by_sha256_sync(self, sha256: str) -> dict[str, Any] | None:
        response = (
            self._client().table("media_items").select("*").eq("sha256", sha256).limit(1).execute()
        )
        rows = self._rows(response)
        return rows[0] if rows else None

    async def upsert_discovery_record(self, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Insert a queue record by normalized URL without overwriting existing state.

        Returns ``(row, created)``. Existing rows keep their status, depth, and
        provenance because the same destination may already be verified,
        rejected, or have a shallower discovery path.
        """
        results = await self.upsert_discovery_records([payload])
        return results[0]

    async def upsert_discovery_records(
        self, payloads: list[dict[str, Any]]
    ) -> list[tuple[dict[str, Any], bool]]:
        """Atomically upsert discoveries in bounded chunks.

        Returns one ``(row, created)`` pair per input in input order. Duplicate
        normalized URLs within the same call share the first occurrence's row;
        only the first occurrence can report ``created=True``.
        """
        if not payloads:
            return []
        return await self._run(
            self._upsert_discovery_records_sync,
            [dict(payload) for payload in payloads],
            retry=True,
        )

    def _upsert_discovery_records_sync(
        self, payloads: list[dict[str, Any]]
    ) -> list[tuple[dict[str, Any], bool]]:
        prepared: list[tuple[str, dict[str, Any]]] = []
        for payload in payloads:
            enriched = self._with_domain_identity(dict(payload))
            normalized_url = enriched.get("normalized_url")
            if not isinstance(normalized_url, str) or not normalized_url:
                raise RepositoryError("Discovery payload requires normalized_url")
            prepared.append((normalized_url, enriched))

        unique: dict[str, dict[str, Any]] = {}
        for normalized_url, payload in prepared:
            unique.setdefault(normalized_url, payload)

        resolved: dict[str, dict[str, Any]] = {}
        created_urls: set[str] = set()
        items = list(unique.items())
        for offset in range(0, len(items), self._discovery_batch_size):
            chunk = items[offset : offset + self._discovery_batch_size]
            chunk_rows, chunk_created = self._upsert_discovery_chunk_sync(chunk)
            resolved.update(chunk_rows)
            created_urls.update(chunk_created)

        results: list[tuple[dict[str, Any], bool]] = []
        seen: set[str] = set()
        for normalized_url, _payload in prepared:
            row = resolved[normalized_url]
            created = normalized_url in created_urls and normalized_url not in seen
            results.append((row, created))
            seen.add(normalized_url)

        self._bump_metrics(
            discovery_batch_calls=1,
            discovery_batch_rows=len(payloads),
            discovery_batch_unique_rows=len(unique),
            discovery_batch_deduped=len(payloads) - len(unique),
            discovery_batch_created=sum(1 for _row, created in results if created),
            discovery_batch_existing=sum(1 for _row, created in results if not created),
        )
        return results

    def _upsert_discovery_chunk_sync(
        self, items: list[tuple[str, dict[str, Any]]]
    ) -> tuple[dict[str, dict[str, Any]], set[str]]:
        client = self._client()
        response = (
            client.table("discoveries")
            .upsert(
                [payload for _normalized_url, payload in items],
                on_conflict="normalized_url",
                ignore_duplicates=True,
            )
            .execute()
        )
        inserted: dict[str, dict[str, Any]] = {}
        for row in self._rows(response):
            normalized_url = row.get("normalized_url")
            if isinstance(normalized_url, str):
                inserted[normalized_url] = row

        # ``ignore_duplicates`` returns only newly inserted rows. Resolve every
        # other ID with one bounded lookup for this chunk instead of one GET per
        # URL.
        missing = [
            normalized_url for normalized_url, _payload in items if normalized_url not in inserted
        ]
        existing: dict[str, dict[str, Any]] = {}
        if missing:
            rows = self._rows(
                client.table("discoveries").select("*").in_("normalized_url", missing).execute()
            )
            for row in rows:
                normalized_url = row.get("normalized_url")
                if isinstance(normalized_url, str):
                    existing[normalized_url] = row

        resolved = {**existing, **inserted}
        unresolved = [
            normalized_url for normalized_url, _payload in items if normalized_url not in resolved
        ]
        if unresolved:
            raise RepositoryError(
                "Discovery upsert did not resolve all normalized URLs in the batch"
            )

        self._bump_metrics(
            discovery_batch_chunks=1,
            discovery_upsert_rows=len(items),
            discovery_inserted=len(inserted),
            discovery_reused=len(existing),
            discovery_batch_lookup_queries=1 if missing else 0,
        )
        return resolved, set(inserted)

    async def get_discovery(self, discovery_id: str) -> dict[str, Any] | None:
        return await self._run(self._get_discovery_sync, discovery_id, retry=True)

    def _get_discovery_sync(self, discovery_id: str) -> dict[str, Any] | None:
        rows = self._rows(
            self._client()
            .table("discoveries")
            .select("*")
            .eq("id", discovery_id)
            .limit(1)
            .execute()
        )
        return rows[0] if rows else None

    async def claim_discoveries(
        self,
        *,
        limit: int,
        max_depth: int,
        per_domain_limit: int = 2,
        new_domain_share: float = 0.8,
    ) -> list[dict[str, Any]]:
        """Atomically claim up to ``limit`` due queued discoveries."""
        # Repeating the whole select-and-claim could exceed the requested batch
        # after a partially committed attempt, so leave this operation unretried.
        return await self._run(
            self._claim_discoveries_sync,
            limit,
            max_depth,
            per_domain_limit,
            new_domain_share,
            retry=False,
        )

    def _claim_discoveries_sync(
        self, limit: int, max_depth: int, per_domain_limit: int, new_domain_share: float
    ) -> list[dict[str, Any]]:
        if limit < 1:
            return []
        result = (
            self._client()
            .rpc(
                "claim_crawl_discoveries",
                {
                    "p_limit": limit,
                    "p_max_depth": max_depth,
                    "p_per_domain_limit": max(1, min(10, per_domain_limit)),
                    "p_new_domain_share": max(0.5, min(0.95, float(new_domain_share))),
                },
            )
            .execute()
        )
        return self._rows(result)

    @staticmethod
    def _with_domain_identity(payload: dict[str, Any]) -> dict[str, Any]:
        value = payload.get("normalized_url") or payload.get("url")
        if not isinstance(value, str):
            return payload
        try:
            identity = domain_identity(value)
        except ValueError:
            return payload
        metadata = payload.get("metadata_json")
        metadata = metadata if isinstance(metadata, dict) else {}
        try:
            priority = int(
                metadata.get("crawl_priority")
                or metadata.get("admission_score")
                or payload.get("crawl_priority")
                or 0
            )
        except (TypeError, ValueError):
            priority = 0
        payload["hostname"] = identity.hostname
        payload["registrable_domain"] = identity.registrable_domain
        payload["domain"] = payload.get("domain") or identity.hostname
        payload["crawl_priority"] = max(0, min(100, priority))
        return payload

    async def update_discovery(self, discovery_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        return await self._run(self._update_discovery_sync, discovery_id, dict(fields), retry=True)

    def _update_discovery_sync(self, discovery_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        payload = {key: value for key, value in fields.items() if key in _DISCOVERY_UPDATE_FIELDS}
        if "status" in payload and payload["status"] not in _DISCOVERY_STATUSES:
            raise RepositoryError("Unsupported discovery status")
        if payload.get("status") != "processing" and "status" in payload:
            payload["claimed_at"] = None
        if not payload:
            return {"id": discovery_id}
        response = (
            self._client().table("discoveries").update(payload).eq("id", discovery_id).execute()
        )
        rows = self._rows(response)
        return rows[0] if rows else {"id": discovery_id, **payload}

    async def update_discovery_metadata(
        self, discovery_id: str, metadata: dict[str, Any]
    ) -> dict[str, Any]:
        return await self.update_discovery(discovery_id, {"metadata_json": metadata})

    async def requeue_discovery(
        self,
        discovery_id: str,
        *,
        reason: str | None = None,
        next_attempt_at: str | None = None,
    ) -> dict[str, Any]:
        return await self.update_discovery(
            discovery_id,
            {
                "status": "queued",
                "last_error": reason,
                "next_attempt_at": next_attempt_at,
            },
        )

    async def record_discovery_edge(
        self,
        *,
        parent_discovery_id: str | None,
        child_discovery_id: str | None,
        child_normalized_url: str,
        discovered_from_url: str,
        depth: int,
        original_url: str | None = None,
        link_text: str | None = None,
        source_domain: str | None = None,
        destination_domain: str | None = None,
        admission_score: int | None = None,
        admission_reason: str | None = None,
        discovery_mechanism: str | None = None,
    ) -> dict[str, Any]:
        rows = await self.record_discovery_edges(
            [
                {
                    "parent_discovery_id": parent_discovery_id,
                    "child_discovery_id": child_discovery_id,
                    "child_normalized_url": child_normalized_url,
                    "discovered_from_url": discovered_from_url,
                    "depth": depth,
                    "original_url": original_url,
                    "link_text": link_text,
                    "source_domain": source_domain,
                    "destination_domain": destination_domain,
                    "admission_score": admission_score,
                    "admission_reason": admission_reason,
                    "discovery_mechanism": discovery_mechanism,
                }
            ]
        )
        return rows[0]

    async def record_discovery_edges(self, edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Record edges atomically through PostgreSQL in bounded chunks.

        Returns one row per input in input order. Duplicate relationships within
        the same call share the first occurrence's row.
        """
        if not edges:
            return []
        return await self._run(
            self._record_discovery_edges_sync,
            [dict(edge) for edge in edges],
            retry=True,
        )

    @staticmethod
    def _edge_key(edge: dict[str, Any]) -> tuple[str | None, str]:
        parent = edge.get("parent_discovery_id")
        return (
            str(parent) if parent not in (None, "") else None,
            str(edge["child_normalized_url"]),
        )

    def _prepare_discovery_edge(self, edge: dict[str, Any]) -> dict[str, Any]:
        child_normalized_url = edge.get("child_normalized_url")
        if not isinstance(child_normalized_url, str) or not child_normalized_url:
            raise RepositoryError("Discovery edge requires child_normalized_url")
        discovered_from_url = edge.get("discovered_from_url")
        if not isinstance(discovered_from_url, str) or not discovered_from_url:
            raise RepositoryError("Discovery edge requires discovered_from_url")
        parent = edge.get("parent_discovery_id")
        child = edge.get("child_discovery_id")
        try:
            depth = int(edge.get("depth") or 0)
        except (TypeError, ValueError):
            raise RepositoryError("Discovery edge depth must be an integer") from None
        if not 0 <= depth <= 6:
            raise RepositoryError("Discovery edge depth must be between 0 and 6")
        admission_score = edge.get("admission_score")
        if admission_score is not None:
            try:
                admission_score = max(0, min(100, int(admission_score)))
            except (TypeError, ValueError):
                raise RepositoryError("Discovery edge admission_score must be an integer") from None
        return {
            "parent_discovery_id": str(parent) if parent not in (None, "") else None,
            "child_discovery_id": str(child) if child not in (None, "") else None,
            "child_normalized_url": child_normalized_url,
            "discovered_from_url": discovered_from_url,
            "depth": depth,
            "original_url": edge.get("original_url") or None,
            "link_text": (str(edge.get("link_text") or "")[:300]) or None,
            "source_domain": edge.get("source_domain") or None,
            "destination_domain": edge.get("destination_domain") or None,
            "admission_score": admission_score,
            "admission_reason": (str(edge.get("admission_reason") or "")[:500]) or None,
            "discovery_mechanism": edge.get("discovery_mechanism") or None,
        }

    def _record_discovery_edges_sync(self, edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
        prepared = [self._prepare_discovery_edge(edge) for edge in edges]
        unique: dict[tuple[str | None, str], dict[str, Any]] = {}
        for edge in prepared:
            unique.setdefault(self._edge_key(edge), edge)

        resolved: dict[tuple[str | None, str], dict[str, Any]] = {}
        created = 0
        items = list(unique.items())
        for offset in range(0, len(items), self._discovery_edge_batch_size):
            chunk = items[offset : offset + self._discovery_edge_batch_size]
            chunk_rows, chunk_created = self._record_discovery_edges_chunk_sync(
                [edge for _key, edge in chunk]
            )
            for (key, _edge), row in zip(chunk, chunk_rows, strict=True):
                resolved[key] = row
            created += chunk_created

        results = [dict(resolved[self._edge_key(edge)]) for edge in prepared]
        self._bump_metrics(
            edge_batch_calls=1,
            edge_batch_rows=len(edges),
            edge_batch_unique_rows=len(unique),
            edge_batch_deduped=len(edges) - len(unique),
            edge_batch_created=created,
            edge_batch_existing=len(edges) - created,
        )
        return results

    def _record_discovery_edges_chunk_sync(
        self, edges: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], int]:
        response = self._client().rpc(_DISCOVERY_EDGE_RPC, {"p_edges": edges}).execute()
        data = getattr(response, "data", None)
        if isinstance(data, list):
            data = data[0] if len(data) == 1 else None
        if not isinstance(data, dict):
            raise RepositoryError("record_discovery_edges returned an invalid response")
        rows = data.get("edges")
        if not isinstance(rows, list):
            raise RepositoryError("record_discovery_edges response is missing edges")
        inserted_count = max(0, min(len(edges), int(data.get("inserted_count") or 0)))

        by_key: dict[tuple[str | None, str], dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            child_normalized_url = row.get("child_normalized_url")
            if not isinstance(child_normalized_url, str):
                continue
            parent = row.get("parent_discovery_id")
            key = (
                str(parent) if parent not in (None, "") else None,
                child_normalized_url,
            )
            by_key[key] = row

        expected = [self._edge_key(edge) for edge in edges]
        missing = [key for key in expected if key not in by_key]
        if missing:
            raise RepositoryError(
                "record_discovery_edges did not return every requested relationship"
            )

        self._bump_metrics(
            edge_batch_chunks=1,
            edge_rpc_rows=len(edges),
            edge_rpc_inserted=inserted_count,
            edge_rpc_duplicates=len(edges) - inserted_count,
            edge_rpc_lookup_queries=0,
        )
        return [dict(by_key[key]) for key in expected], inserted_count

    async def upsert_discovery(self, candidate: RedditCandidate) -> dict[str, Any]:
        return await self._run(self._upsert_discovery_sync, candidate, retry=True)

    def _upsert_discovery_sync(self, candidate: RedditCandidate) -> dict[str, Any]:
        discovery_id = getattr(candidate, "discovery_id", None)
        if discovery_id:
            existing = self._get_discovery_sync(str(discovery_id))
            if existing is not None:
                return existing

        now = datetime.now(tz=UTC).isoformat()
        metadata = dict(candidate.media_metadata)
        metadata["media_url"] = candidate.media_url
        metadata["extraction_reason"] = candidate.extraction_reason
        source_type = getattr(candidate, "source_type", "reddit") or "reddit"
        source_url = candidate.source_url
        source_host = urlparse(source_url).hostname or (
            "www.reddit.com" if source_type == "reddit" else "unknown"
        )

        payload = {
            "url": source_url,
            "normalized_url": candidate.normalized_source_url,
            "source_type": source_type,
            "source_platform_id": candidate.submission_id,
            "title": candidate.title or None,
            "description": candidate.body,
            "creator_name": candidate.author_name,
            "creator_profile_url": candidate.author_profile_url,
            "domain": source_host,
            "depth": int(getattr(candidate, "crawl_depth", 0) or 0),
            "status": "verified",
            "verified_at": now,
            "metadata_json": metadata,
        }
        payload = self._with_domain_identity(payload)
        response = (
            self._client()
            .table("discoveries")
            .upsert(payload, on_conflict="normalized_url")
            .execute()
        )
        rows = self._rows(response)
        if rows:
            return rows[0]

        fallback = (
            self._client()
            .table("discoveries")
            .select("*")
            .eq("normalized_url", candidate.normalized_source_url)
            .limit(1)
            .execute()
        )
        fallback_rows = self._rows(fallback)
        if fallback_rows:
            return fallback_rows[0]
        raise RepositoryError("Discovery upsert returned no row")

    async def create_media_item(self, payload: dict[str, Any]) -> dict[str, Any]:
        # A unique SHA-256 key and duplicate recovery make this insert repeatable.
        # Without that key, an ambiguous response must not trigger another insert.
        sha256 = payload.get("sha256")
        retry = isinstance(sha256, str) and len(sha256) == 64
        return await self._run(self._create_media_item_sync, payload, retry=retry)

    def _create_media_item_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._client().table("media_items").insert(payload).execute()
        except Exception as exc:
            sha256 = payload.get("sha256")
            if isinstance(sha256, str) and _is_unique_violation(exc):
                try:
                    existing = self._get_media_by_sha256_sync(sha256)
                except Exception:
                    existing = None
                if existing is not None:
                    return {**existing, "_recovered": True}
            raise
        rows = self._rows(response)
        if rows:
            return rows[0]
        raise RepositoryError("Media item insert returned no row")

    async def update_media_item_metadata(
        self,
        media_item_id: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """Fill non-null technical metadata without erasing existing values."""
        return await self._run(
            self._update_media_item_metadata_sync,
            media_item_id,
            metadata,
            retry=True,
        )

    def _update_media_item_metadata_sync(
        self,
        media_item_id: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        allowed = {
            "width",
            "height",
            "duration_seconds",
            "mime_type",
            "extension",
            "media_type",
            "size_bytes",
            "perceptual_hash",
        }
        payload = {
            key: value for key, value in metadata.items() if key in allowed and value is not None
        }
        if not payload:
            return {"id": media_item_id}
        response = (
            self._client().table("media_items").update(payload).eq("id", media_item_id).execute()
        )
        rows = self._rows(response)
        return rows[0] if rows else {"id": media_item_id, **payload}

    async def list_media_with_perceptual_hash(
        self,
        *,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Return recent rows with perceptual hashes for similarity review."""
        return await self._run(self._list_perceptual_hashes_sync, limit, retry=True)

    def _list_perceptual_hashes_sync(self, limit: int) -> list[dict[str, Any]]:
        response = (
            self._client()
            .table("media_items")
            .select("id, perceptual_hash")
            .limit(max(1, limit))
            .execute()
        )
        return [row for row in self._rows(response) if isinstance(row.get("perceptual_hash"), str)]

    async def record_source(
        self,
        media_item_id: str,
        candidate: RedditCandidate,
        *,
        discovery_id: str | None = None,
        media_url: str | None = None,
        source_media_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._run(
            self._record_source_sync,
            media_item_id,
            candidate,
            discovery_id,
            media_url,
            source_media_id,
            retry=True,
        )

    def _record_source_sync(
        self,
        media_item_id: str,
        candidate: RedditCandidate,
        discovery_id: str | None,
        media_url: str | None,
        source_media_id: str | None,
    ) -> dict[str, Any]:
        source_platform = getattr(candidate, "source_platform", "reddit") or "reddit"
        source_post_id = getattr(candidate, "source_post_id", None) or candidate.submission_id
        payload = {
            "media_item_id": media_item_id,
            "discovery_id": discovery_id,
            "source_platform": source_platform,
            "source_post_id": source_post_id,
            "source_media_id": source_media_id or "",
            "source_url": candidate.source_url,
            "normalized_source_url": candidate.normalized_source_url,
            "media_url": media_url or candidate.media_url,
            "creator_name": candidate.author_name,
            "creator_profile_url": candidate.author_profile_url,
            "caption": candidate.body,
            "title": candidate.title or None,
            "discovered_at": datetime.now(tz=UTC).isoformat(),
        }
        response = (
            self._client()
            .table("media_sources")
            .upsert(
                payload,
                on_conflict="media_item_id,normalized_source_url,source_media_id",
            )
            .execute()
        )
        rows = self._rows(response)
        if rows:
            return rows[0]
        raise RepositoryError("Media source upsert returned no row")

    async def create_publication(
        self,
        media_item_id: str,
        channel_id: int,
        purpose: str,
        *,
        attempt_count: int = 1,
    ) -> dict[str, Any]:
        return await self._run(
            self._create_publication_sync,
            media_item_id,
            channel_id,
            purpose,
            attempt_count,
            retry=False,
        )

    def _create_publication_sync(
        self,
        media_item_id: str,
        channel_id: int,
        purpose: str,
        attempt_count: int,
    ) -> dict[str, Any]:
        payload = {
            "media_item_id": media_item_id,
            "channel_id": channel_id,
            "purpose": purpose,
            "status": "pending",
            "attempt_count": attempt_count,
        }
        response = self._client().table("publications").insert(payload).execute()
        rows = self._rows(response)
        if rows:
            return rows[0]
        raise RepositoryError("Publication insert returned no row")

    async def update_publication(
        self,
        publication_id: str,
        *,
        status: str,
        message_id: int | None = None,
        last_error: str | None = None,
    ) -> dict[str, Any]:
        return await self._run(
            self._update_publication_sync,
            publication_id,
            status,
            message_id,
            last_error,
            retry=True,
        )

    def _update_publication_sync(
        self,
        publication_id: str,
        status: str,
        message_id: int | None,
        last_error: str | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"status": status}
        if message_id is not None:
            payload["message_id"] = message_id
        if status == "published":
            payload["published_at"] = datetime.now(tz=UTC).isoformat()
        if last_error is not None:
            payload["last_error"] = last_error

        response = (
            self._client().table("publications").update(payload).eq("id", publication_id).execute()
        )
        rows = self._rows(response)
        if rows:
            return rows[0]
        return {"id": publication_id, **payload}

    async def get_digest_publication(
        self, media_item_id: str, channel_id: int
    ) -> dict[str, Any] | None:
        """Return the durable daily-digest delivery row for one item and channel."""
        return await self._run(
            self._get_digest_publication_sync, media_item_id, channel_id, retry=True
        )

    def _get_digest_publication_sync(
        self, media_item_id: str, channel_id: int
    ) -> dict[str, Any] | None:
        rows = self._rows(
            self._client()
            .table("publications")
            .select("*")
            .eq("media_item_id", media_item_id)
            .eq("channel_id", channel_id)
            .eq("purpose", "daily_digest")
            .limit(1)
            .execute()
        )
        return rows[0] if rows else None

    async def ensure_digest_publication(
        self,
        media_item_id: str,
        channel_id: int,
    ) -> dict[str, Any]:
        """Create a digest row only when none exists, recovering unique conflicts.

        The partial unique index ``uq_publications_digest_item_channel`` remains
        the final safety boundary. Attempts are counted separately with
        :meth:`record_publication_attempt`, so a freshly created row starts at
        zero.
        """
        return await self._run(
            self._ensure_digest_publication_sync, media_item_id, channel_id, retry=True
        )

    async def reserve_digest_publication(
        self,
        media_item_id: str,
        channel_id: int,
        digest_cycle: str,
        *,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Atomically reserve one global digest slot through PostgreSQL."""
        return await self._run(
            self._reserve_digest_publication_sync,
            media_item_id,
            channel_id,
            digest_cycle,
            limit,
            retry=False,
        )

    async def retarget_digest_publication(
        self, publication_id: str, claim_token: str, channel_id: int
    ) -> dict[str, Any]:
        return await self._digest_claim_rpc(
            "retarget_daily_digest_publication",
            {
                "p_publication_id": publication_id,
                "p_claim_token": claim_token,
                "p_channel_id": channel_id,
            },
        )

    def _reserve_digest_publication_sync(
        self,
        media_item_id: str,
        channel_id: int,
        digest_cycle: str,
        limit: int,
    ) -> dict[str, Any]:
        response = (
            self._client()
            .rpc(
                "reserve_daily_digest_publication",
                {
                    "p_media_item_id": media_item_id,
                    "p_channel_id": channel_id,
                    "p_digest_cycle": digest_cycle,
                    "p_limit": max(0, limit),
                },
            )
            .execute()
        )
        data = getattr(response, "data", None)
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, dict):
            raise RepositoryError("Digest reservation returned an invalid response")
        return data

    def _ensure_digest_publication_sync(
        self, media_item_id: str, channel_id: int
    ) -> dict[str, Any]:
        existing = self._get_digest_publication_sync(media_item_id, channel_id)
        if existing is not None:
            return existing
        payload = {
            "media_item_id": media_item_id,
            "channel_id": channel_id,
            "purpose": "daily_digest",
            "status": "pending",
            "attempt_count": 0,
        }
        try:
            response = self._client().table("publications").insert(payload).execute()
        except Exception as exc:
            if _is_unique_violation(exc):
                existing = self._get_digest_publication_sync(media_item_id, channel_id)
                if existing is not None:
                    return existing
            raise
        rows = self._rows(response)
        if rows:
            return rows[0]
        fallback = self._get_digest_publication_sync(media_item_id, channel_id)
        if fallback is not None:
            return fallback
        raise RepositoryError("Digest publication insert returned no row")

    async def record_publication_attempt(
        self,
        publication_id: str,
        *,
        expected_attempt_count: int,
        claim_token: str,
        digest_cycle: str,
        attempt_limit: int = 3,
    ) -> dict[str, Any] | None:
        """Advance attempts only through a live, fenced delivery claim."""
        result = await self.advance_digest_attempt(
            publication_id,
            claim_token,
            digest_cycle,
            expected_attempt_count=expected_attempt_count,
            attempt_limit=attempt_limit,
        )
        return result if result.get("attempt_advanced") else None

    async def claim_digest_publication(
        self, publication_id: str, claim_token: str, *, lease_seconds: int = 900
    ) -> dict[str, Any]:
        return await self._digest_claim_rpc(
            "claim_daily_digest_publication",
            {
                "p_publication_id": publication_id,
                "p_claim_token": claim_token,
                "p_lease_seconds": lease_seconds,
            },
        )

    async def renew_digest_claim(
        self, publication_id: str, claim_token: str, *, lease_seconds: int = 900
    ) -> bool:
        return bool(
            await self._digest_claim_rpc(
                "renew_daily_digest_claim",
                {
                    "p_publication_id": publication_id,
                    "p_claim_token": claim_token,
                    "p_lease_seconds": lease_seconds,
                },
            )
        )

    async def advance_digest_attempt(
        self,
        publication_id: str,
        claim_token: str,
        digest_cycle: str,
        *,
        expected_attempt_count: int,
        attempt_limit: int,
        lease_seconds: int = 900,
    ) -> dict[str, Any]:
        return await self._digest_claim_rpc(
            "advance_daily_digest_attempt_for_cycle",
            {
                "p_publication_id": publication_id,
                "p_claim_token": claim_token,
                "p_expected_attempt_count": expected_attempt_count,
                "p_digest_cycle": digest_cycle,
                "p_attempt_limit": attempt_limit,
                "p_lease_seconds": lease_seconds,
            },
        )

    async def terminalize_digest_publication(
        self,
        publication_id: str,
        claim_token: str,
        error: str,
        *,
        attempt_limit: int,
        conclusive_no_delivery: bool,
    ) -> bool:
        return bool(
            await self._digest_claim_rpc(
                "terminalize_daily_digest_publication",
                {
                    "p_publication_id": publication_id,
                    "p_claim_token": claim_token,
                    "p_error": error,
                    "p_attempt_limit": attempt_limit,
                    "p_conclusive_no_delivery": conclusive_no_delivery,
                },
            )
        )

    async def finish_digest_failure(
        self, publication_id: str, claim_token: str, error: str
    ) -> bool:
        return bool(
            await self._digest_claim_rpc(
                "finish_daily_digest_failure",
                {
                    "p_publication_id": publication_id,
                    "p_claim_token": claim_token,
                    "p_error": error,
                },
            )
        )

    async def finish_digest_reconciliation(
        self, publication_id: str, claim_token: str, error: str
    ) -> bool:
        return bool(
            await self._digest_claim_rpc(
                "finish_daily_digest_reconciliation",
                {
                    "p_publication_id": publication_id,
                    "p_claim_token": claim_token,
                    "p_error": error,
                },
            )
        )

    async def publish_digest_claimed(
        self, publication_id: str, claim_token: str, message_id: int
    ) -> bool:
        return bool(
            await self._digest_claim_rpc(
                "publish_daily_digest_publication",
                {
                    "p_publication_id": publication_id,
                    "p_claim_token": claim_token,
                    "p_message_id": message_id,
                },
            )
        )

    async def rollover_reconciled_digest_publication(
        self,
        publication_id: str,
        claim_token: str,
        digest_cycle: str,
        *,
        limit: int = 50,
    ) -> dict[str, Any]:
        return await self._digest_claim_rpc(
            "rollover_reconciled_digest_publication",
            {
                "p_publication_id": publication_id,
                "p_claim_token": claim_token,
                "p_digest_cycle": digest_cycle,
                "p_limit": limit,
            },
        )

    async def _digest_claim_rpc(self, function: str, params: dict[str, Any]) -> Any:
        # Digest claim RPCs are stateful (leases and attempt counters), so they
        # are deliberately excluded from automatic transport retries.
        return await self._run(self._digest_claim_rpc_sync, function, params, retry=False)

    def _digest_claim_rpc_sync(self, function: str, params: dict[str, Any]) -> Any:
        response = self._client().rpc(function, params).execute()
        data = getattr(response, "data", None)
        if isinstance(data, list):
            data = data[0] if data else None
        if data is None:
            raise RepositoryError(f"{function} returned an empty response")
        return data

    async def mark_publication_published(
        self,
        publication_id: str,
        message_id: int,
    ) -> dict[str, Any]:
        return await self._run(
            self._mark_publication_published_sync, publication_id, message_id, retry=True
        )

    def _mark_publication_published_sync(
        self, publication_id: str, message_id: int
    ) -> dict[str, Any]:
        payload = {
            "status": "published",
            "message_id": message_id,
            "published_at": datetime.now(tz=UTC).isoformat(),
            "last_error": None,
        }
        response = (
            self._client().table("publications").update(payload).eq("id", publication_id).execute()
        )
        rows = self._rows(response)
        if rows:
            return rows[0]
        return {"id": publication_id, **payload}

    async def mark_publication_failed(
        self,
        publication_id: str,
        error: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._mark_publication_failed_sync, publication_id, error, retry=True
        )

    def _mark_publication_failed_sync(self, publication_id: str, error: str) -> dict[str, Any]:
        payload = {"status": "failed", "last_error": error[:500]}
        if not error.startswith(("discord_delivery_ambiguous:", "discord_delivery_failed")):
            payload["digest_slot_reserved"] = False
        response = (
            self._client().table("publications").update(payload).eq("id", publication_id).execute()
        )
        rows = self._rows(response)
        if rows:
            return rows[0]
        return {"id": publication_id, **payload}

    async def record_publication_reconciliation_error(
        self,
        publication_id: str,
        error: str,
        *,
        preserve_error: str | None = None,
    ) -> dict[str, Any]:
        """Record a reconciliation diagnostic without losing prior uncertainty."""
        return await self._run(
            self._record_publication_reconciliation_error_sync,
            publication_id,
            error,
            preserve_error,
            retry=True,
        )

    def _record_publication_reconciliation_error_sync(
        self, publication_id: str, error: str, preserve_error: str | None = None
    ) -> dict[str, Any]:
        last_error = f"{preserve_error};{error}" if preserve_error else error
        payload = {"last_error": last_error[:500]}
        response = (
            self._client().table("publications").update(payload).eq("id", publication_id).execute()
        )
        rows = self._rows(response)
        if rows:
            return rows[0]
        return {"id": publication_id, **payload}

    async def list_retryable_digest_publications(
        self,
        *,
        attempt_limit: int,
        limit: int = 100,
        now: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return retryable rows and exhausted rows that still need reconciliation."""
        return await self._run(
            self._list_retryable_digest_publications_sync,
            attempt_limit,
            limit,
            now or datetime.now(tz=UTC).isoformat(),
            retry=True,
        )

    def _list_retryable_digest_publications_sync(
        self, attempt_limit: int, limit: int, now: str
    ) -> list[dict[str, Any]]:
        return self._rows(
            self._client()
            .table("publications")
            .select("*")
            .eq("purpose", "daily_digest")
            .in_("status", ["pending", "failed"])
            .lt("attempt_count", max(1, attempt_limit))
            .eq("digest_terminal", False)
            .or_(f"next_attempt_at.is.null,next_attempt_at.lte.{now}")
            .or_(
                "delivery_claim_token.is.null,delivery_claim_expires_at.is.null,"
                f"delivery_claim_expires_at.lte.{now}"
            )
            .order("next_attempt_at", nullsfirst=True)
            .order("created_at")
            .order("id")
            .limit(max(1, limit))
            .execute()
        )

    async def list_exhausted_ambiguous_digest_publications(
        self,
        *,
        attempt_limit: int,
        page_size: int = 100,
        now: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return every exhausted ambiguous row, reading the backlog in stable pages."""
        return await self._run(
            self._list_exhausted_ambiguous_digest_publications_sync,
            attempt_limit,
            page_size,
            now or datetime.now(tz=UTC).isoformat(),
            retry=True,
        )

    def _list_exhausted_ambiguous_digest_publications_sync(
        self, attempt_limit: int, page_size: int, now: str
    ) -> list[dict[str, Any]]:
        size = max(1, page_size)
        results: list[dict[str, Any]] = []
        cursor_created_at: str | None = None
        cursor_id: str | None = None
        while True:
            query = (
                self._client()
                .table("publications")
                .select("*")
                .eq("purpose", "daily_digest")
                .in_("status", ["pending", "failed"])
                .gte("attempt_count", max(1, attempt_limit))
                .eq("digest_terminal", False)
                .or_(
                    ",".join(
                        (
                            "status.eq.pending",
                            "last_error.like.discord_delivery_ambiguous:*",
                            "last_error.like.discord_delivery_failed*",
                        )
                    )
                )
                .or_(f"next_attempt_at.is.null,next_attempt_at.lte.{now}")
                .order("created_at")
                .order("id")
            )
            if cursor_created_at is not None and cursor_id is not None:
                query = query.or_(
                    f"created_at.gt.{cursor_created_at},"
                    f"and(created_at.eq.{cursor_created_at},id.gt.{cursor_id})"
                )
            page = self._rows(query.range(0, size - 1).execute())
            results.extend(page)
            if len(page) < size:
                return results
            cursor_created_at = str(page[-1].get("created_at") or "")
            cursor_id = str(page[-1].get("id") or "")

    async def get_media_item(self, media_item_id: str) -> dict[str, Any] | None:
        return await self._run(self._get_media_item_sync, media_item_id, retry=True)

    def _get_media_item_sync(self, media_item_id: str) -> dict[str, Any] | None:
        rows = self._rows(
            self._client()
            .table("media_items")
            .select("*")
            .eq("id", media_item_id)
            .limit(1)
            .execute()
        )
        return rows[0] if rows else None

    async def list_digest_candidates(
        self,
        *,
        window_start: str,
        window_end: str,
        cursor: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return eligible media in deterministic window order.

        The cursor is inclusive. Re-reading rows at the last persisted timestamp
        is safe because publication records decide whether delivery already
        happened, and it prevents ties from being skipped permanently.
        """
        return await self._run(
            self._list_digest_candidates_sync,
            window_start,
            window_end,
            cursor,
            limit,
            offset,
            retry=True,
        )

    def _list_digest_candidates_sync(
        self,
        window_start: str,
        window_end: str,
        cursor: str | None,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        page_size = max(1, limit)
        start_offset = max(0, offset)
        query = (
            self._client()
            .table("media_items")
            .select("*")
            .eq("verification_status", "verified")
            .eq("download_status", "stored")
            .gte("created_at", cursor or window_start)
            .lte("created_at", window_end)
            .order("created_at")
            .order("id")
            .range(start_offset, start_offset + page_size - 1)
        )
        return self._rows(query.execute())

    async def digest_publication_counts(self, *, attempt_limit: int = 3) -> dict[str, int]:
        return await self._run(self._digest_publication_counts_sync, attempt_limit, retry=True)

    def _digest_publication_counts_sync(self, attempt_limit: int) -> dict[str, int]:
        client = self._client()

        def count(status: str, attempt_filter: str | None = None) -> int:
            query = (
                client.table("publications")
                .select("id", count="exact")
                .eq("purpose", "daily_digest")
                .eq("status", status)
            )
            threshold = max(1, attempt_limit)
            if attempt_filter == "retryable":
                query = query.lt("attempt_count", threshold)
            elif attempt_filter == "exhausted":
                query = query.gte("attempt_count", threshold)
            response = query.limit(1).execute()
            return int(getattr(response, "count", None) or 0)

        return {
            "pending": count("pending"),
            "failed_retryable": count("failed", "retryable"),
            "failed_exhausted": count("failed", "exhausted"),
        }

    async def get_digest_state(self) -> dict[str, Any]:
        return await self._run(self._get_digest_state_sync, retry=True)

    def _get_digest_state_sync(self) -> dict[str, Any]:
        rows = self._rows(
            self._client().table("digest_state").select("*").eq("id", 1).limit(1).execute()
        )
        return rows[0] if rows else {"id": 1}

    async def update_digest_state(self, fields: dict[str, Any]) -> dict[str, Any]:
        return await self._run(self._update_digest_state_sync, dict(fields), retry=True)

    def _update_digest_state_sync(self, fields: dict[str, Any]) -> dict[str, Any]:
        payload = {
            key: value
            for key, value in fields.items()
            if key in _DIGEST_STATE_FIELDS and value is not None
        }
        if not payload:
            return {"id": 1}
        response = self._client().table("digest_state").update(payload).eq("id", 1).execute()
        rows = self._rows(response)
        return rows[0] if rows else {"id": 1, **payload}

    async def mark_digest_success(self) -> None:
        await self._run(self._mark_digest_success_sync, retry=True)
        self._invalidate_runtime_state_cache()

    def _mark_digest_success_sync(self) -> None:
        self._client().table("runtime_state").update(
            {"last_digest_success_at": datetime.now(tz=UTC).isoformat()}
        ).eq("id", 1).execute()

    async def latest_digest_runs(self, limit: int = 1) -> list[dict[str, Any]]:
        return await self.latest_jobs("daily_digest", limit)
