"""Supabase repository methods used by the Phase 2 media pipeline."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from app.db.client import SupabaseDatabase
from app.reddit.models import RedditCandidate

_JOB_TYPES = ("reddit_collection", "brave_discovery", "crawl", "daily_digest")
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
    }
)


class RepositoryError(RuntimeError):
    """Raised when a repository operation cannot be completed."""


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
    ) -> None:
        self._database = database
        self._to_thread = to_thread

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
        return await self._to_thread(self._runtime_state_sync)

    def _runtime_state_sync(self) -> dict[str, Any]:
        rows = self._rows(
            self._client().table("runtime_state").select("*").eq("id", 1).limit(1).execute()
        )
        return rows[0] if rows else {"id": 1, "collection_enabled": False}

    async def set_collection_enabled(self, enabled: bool) -> dict[str, Any]:
        return await self._to_thread(self._set_collection_enabled_sync, enabled)

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
        return await self._to_thread(self._create_job_run_sync, config, job_type)

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
        await self._to_thread(self._finish_job_run_sync, job_id, counters, outcome, error)

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
        await self._to_thread(self._mark_interrupted_sync)

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
        return await self._to_thread(self._recover_processing_discoveries_sync)

    def _recover_processing_discoveries_sync(self) -> int:
        client = self._client()
        rows = self._rows(
            client.table("discoveries").select("id").eq("status", "processing").execute()
        )
        now = datetime.now(tz=UTC).isoformat()
        for row in rows:
            client.table("discoveries").update(
                {
                    "status": "queued",
                    "last_error": "interrupted_by_restart",
                    "next_attempt_at": now,
                }
            ).eq("id", row["id"]).execute()
        return len(rows)

    async def latest_jobs(self, job_type: str, limit: int = 1) -> list[dict[str, Any]]:
        return await self._to_thread(self._latest_jobs_sync, job_type, limit)

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

    async def latest_brave_runs(self, limit: int = 1) -> list[dict[str, Any]]:
        return await self.latest_jobs("brave_discovery", limit)

    async def latest_crawl_runs(self, limit: int = 1) -> list[dict[str, Any]]:
        return await self.latest_jobs("crawl", limit)

    async def latest_reddit_runs(self, limit: int = 2) -> list[dict[str, Any]]:
        return await self._to_thread(self._latest_reddit_runs_sync, limit)

    async def operational_counts(self) -> dict[str, Any]:
        return await self._to_thread(self._operational_counts_sync)

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
            "last_brave_success_at": state.get("last_brave_success_at"),
            "brave_paused": bool(state.get("brave_paused")),
            "brave_pause_reason": state.get("brave_pause_reason"),
        }

    async def mark_reddit_success(self) -> None:
        await self._to_thread(self._mark_reddit_success_sync)

    def _mark_reddit_success_sync(self) -> None:
        self._client().table("runtime_state").update(
            {"last_reddit_success_at": datetime.now(tz=UTC).isoformat()}
        ).eq("id", 1).execute()

    async def mark_brave_success(self) -> None:
        await self._to_thread(self._mark_brave_success_sync)

    def _mark_brave_success_sync(self) -> None:
        self._client().table("runtime_state").update(
            {"last_brave_success_at": datetime.now(tz=UTC).isoformat()}
        ).eq("id", 1).execute()

    async def pause_brave(self, reason: str) -> None:
        await self._to_thread(self._pause_brave_sync, reason)

    def _pause_brave_sync(self, reason: str) -> None:
        self._client().table("runtime_state").update(
            {
                "brave_paused": True,
                "brave_pause_reason": reason[:500],
                "brave_paused_at": datetime.now(tz=UTC).isoformat(),
            }
        ).eq("id", 1).execute()

    async def clear_brave_pause(self) -> None:
        """Clear the persisted pause after an owner reviews the restriction.

        No Discord command calls this. It exists for a deliberate operator
        action and never runs automatically on startup or from ``/start``.
        """
        await self._to_thread(self._clear_brave_pause_sync)

    def _clear_brave_pause_sync(self) -> None:
        self._client().table("runtime_state").update(
            {
                "brave_paused": False,
                "brave_pause_reason": None,
                "brave_paused_at": None,
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
        return await self._to_thread(self._random_asset_sync)

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
        return await self._to_thread(self._source_for_media_sync, media_item_id)

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
        return await self._to_thread(self._sources_for_media_sync, media_item_id)

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
        return await self._to_thread(self._asset_stats_sync, start)

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
        return await self._to_thread(self._get_media_by_sha256_sync, sha256)

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
        normalized_url = payload.get("normalized_url")
        if not isinstance(normalized_url, str) or not normalized_url:
            raise RepositoryError("Discovery payload requires normalized_url")
        return await self._to_thread(self._upsert_discovery_record_sync, dict(payload))

    def _upsert_discovery_record_sync(self, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        normalized_url = str(payload["normalized_url"])
        existing = self._get_discovery_by_url_sync(normalized_url)
        if existing is not None:
            return existing, False
        try:
            response = self._client().table("discoveries").insert(payload).execute()
        except Exception as exc:
            if _is_unique_violation(exc):
                existing = self._get_discovery_by_url_sync(normalized_url)
                if existing is not None:
                    return existing, False
            raise
        rows = self._rows(response)
        if rows:
            return rows[0], True
        fallback = self._get_discovery_by_url_sync(normalized_url)
        if fallback is not None:
            return fallback, False
        raise RepositoryError("Discovery insert returned no row")

    def _get_discovery_by_url_sync(self, normalized_url: str) -> dict[str, Any] | None:
        rows = self._rows(
            self._client()
            .table("discoveries")
            .select("*")
            .eq("normalized_url", normalized_url)
            .limit(1)
            .execute()
        )
        return rows[0] if rows else None

    async def get_discovery(self, discovery_id: str) -> dict[str, Any] | None:
        return await self._to_thread(self._get_discovery_sync, discovery_id)

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
    ) -> list[dict[str, Any]]:
        """Atomically claim up to ``limit`` due queued discoveries."""
        return await self._to_thread(self._claim_discoveries_sync, limit, max_depth)

    def _claim_discoveries_sync(self, limit: int, max_depth: int) -> list[dict[str, Any]]:
        if limit < 1:
            return []
        now = datetime.now(tz=UTC).isoformat()
        rows = self._rows(
            self._client()
            .table("discoveries")
            .select("*")
            .eq("status", "queued")
            .lte("depth", max_depth)
            .or_(f"next_attempt_at.is.null,next_attempt_at.lte.{now}")
            .order("discovered_at")
            .limit(limit)
            .execute()
        )
        claimed: list[dict[str, Any]] = []
        for row in rows:
            discovery_id = row.get("id")
            if discovery_id is None:
                continue
            response = (
                self._client()
                .table("discoveries")
                .update({"status": "processing", "next_attempt_at": None})
                .eq("id", discovery_id)
                .eq("status", "queued")
                .execute()
            )
            updated = self._rows(response)
            if updated:
                claimed.append({**row, **updated[0]})
        return claimed

    async def update_discovery(self, discovery_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        return await self._to_thread(self._update_discovery_sync, discovery_id, dict(fields))

    def _update_discovery_sync(self, discovery_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        payload = {key: value for key, value in fields.items() if key in _DISCOVERY_UPDATE_FIELDS}
        if "status" in payload and payload["status"] not in _DISCOVERY_STATUSES:
            raise RepositoryError("Unsupported discovery status")
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
    ) -> dict[str, Any]:
        return await self._to_thread(
            self._record_discovery_edge_sync,
            parent_discovery_id,
            child_discovery_id,
            child_normalized_url,
            discovered_from_url,
            depth,
        )

    def _edge_query(self, parent_discovery_id: str | None, child_normalized_url: str) -> Any:
        query = (
            self._client()
            .table("discovery_edges")
            .select("id")
            .eq("child_normalized_url", child_normalized_url)
        )
        if parent_discovery_id is None:
            query = query.is_("parent_discovery_id", "null")
        else:
            query = query.eq("parent_discovery_id", parent_discovery_id)
        return query.limit(1)

    def _record_discovery_edge_sync(
        self,
        parent_discovery_id: str | None,
        child_discovery_id: str | None,
        child_normalized_url: str,
        discovered_from_url: str,
        depth: int,
    ) -> dict[str, Any]:
        existing = self._rows(self._edge_query(parent_discovery_id, child_normalized_url).execute())
        if existing:
            return existing[0]
        payload = {
            "parent_discovery_id": parent_discovery_id,
            "child_discovery_id": child_discovery_id,
            "child_normalized_url": child_normalized_url,
            "discovered_from_url": discovered_from_url,
            "depth": depth,
        }
        try:
            response = self._client().table("discovery_edges").insert(payload).execute()
        except Exception as exc:
            if _is_unique_violation(exc):
                existing = self._rows(
                    self._edge_query(parent_discovery_id, child_normalized_url).execute()
                )
                if existing:
                    return existing[0]
            raise
        rows = self._rows(response)
        return rows[0] if rows else payload

    async def upsert_discovery(self, candidate: RedditCandidate) -> dict[str, Any]:
        return await self._to_thread(self._upsert_discovery_sync, candidate)

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
        return await self._to_thread(self._create_media_item_sync, payload)

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
        return await self._to_thread(
            self._update_media_item_metadata_sync,
            media_item_id,
            metadata,
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
        return await self._to_thread(self._list_perceptual_hashes_sync, limit)

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
        return await self._to_thread(
            self._record_source_sync,
            media_item_id,
            candidate,
            discovery_id,
            media_url,
            source_media_id,
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
        return await self._to_thread(
            self._create_publication_sync,
            media_item_id,
            channel_id,
            purpose,
            attempt_count,
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
        return await self._to_thread(
            self._update_publication_sync,
            publication_id,
            status,
            message_id,
            last_error,
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
        return await self._to_thread(self._get_digest_publication_sync, media_item_id, channel_id)

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
        return await self._to_thread(
            self._ensure_digest_publication_sync, media_item_id, channel_id
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
        return await self._to_thread(
            self._reserve_digest_publication_sync,
            media_item_id,
            channel_id,
            digest_cycle,
            limit,
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
    ) -> dict[str, Any]:
        """Advance the real delivery attempt count before any send is attempted."""
        return await self._to_thread(
            self._record_publication_attempt_sync,
            publication_id,
            expected_attempt_count,
        )

    def _record_publication_attempt_sync(
        self, publication_id: str, expected_attempt_count: int
    ) -> dict[str, Any]:
        payload = {
            "status": "pending",
            "attempt_count": max(0, expected_attempt_count) + 1,
            "last_error": None,
        }
        response = (
            self._client()
            .table("publications")
            .update(payload)
            .eq("id", publication_id)
            .eq("attempt_count", max(0, expected_attempt_count))
            .execute()
        )
        rows = self._rows(response)
        if rows:
            return rows[0]
        current = self._rows(
            self._client()
            .table("publications")
            .select("*")
            .eq("id", publication_id)
            .limit(1)
            .execute()
        )
        if current:
            return current[0]
        raise RepositoryError("Publication attempt could not be recorded")

    async def mark_publication_published(
        self,
        publication_id: str,
        message_id: int,
    ) -> dict[str, Any]:
        return await self._to_thread(
            self._mark_publication_published_sync, publication_id, message_id
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
        return await self._to_thread(self._mark_publication_failed_sync, publication_id, error)

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
        return await self._to_thread(
            self._record_publication_reconciliation_error_sync,
            publication_id,
            error,
            preserve_error,
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
    ) -> list[dict[str, Any]]:
        """Return retryable rows and exhausted rows that still need reconciliation."""
        return await self._to_thread(
            self._list_retryable_digest_publications_sync,
            attempt_limit,
            limit,
        )

    def _list_retryable_digest_publications_sync(
        self, attempt_limit: int, limit: int
    ) -> list[dict[str, Any]]:
        return self._rows(
            self._client()
            .table("publications")
            .select("*")
            .eq("purpose", "daily_digest")
            .in_("status", ["pending", "failed"])
            .lt("attempt_count", max(1, attempt_limit))
            .order("created_at")
            .limit(max(1, limit))
            .execute()
        )

    async def list_exhausted_ambiguous_digest_publications(
        self,
        *,
        attempt_limit: int,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """Return every exhausted ambiguous row, reading the backlog in stable pages."""
        return await self._to_thread(
            self._list_exhausted_ambiguous_digest_publications_sync,
            attempt_limit,
            page_size,
        )

    def _list_exhausted_ambiguous_digest_publications_sync(
        self, attempt_limit: int, page_size: int
    ) -> list[dict[str, Any]]:
        size = max(1, page_size)
        offset = 0
        results: list[dict[str, Any]] = []
        while True:
            page = self._rows(
                self._client()
                .table("publications")
                .select("*")
                .eq("purpose", "daily_digest")
                .in_("status", ["pending", "failed"])
                .gte("attempt_count", max(1, attempt_limit))
                .or_(
                    ",".join(
                        (
                            "status.eq.pending",
                            "last_error.like.discord_delivery_ambiguous:*",
                            "last_error.like.discord_delivery_failed*",
                        )
                    )
                )
                .order("created_at")
                .order("id")
                .range(offset, offset + size - 1)
                .execute()
            )
            results.extend(page)
            if len(page) < size:
                return results
            offset += len(page)

    async def get_media_item(self, media_item_id: str) -> dict[str, Any] | None:
        return await self._to_thread(self._get_media_item_sync, media_item_id)

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
        return await self._to_thread(
            self._list_digest_candidates_sync,
            window_start,
            window_end,
            cursor,
            limit,
            offset,
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
        return await self._to_thread(self._digest_publication_counts_sync, attempt_limit)

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
        return await self._to_thread(self._get_digest_state_sync)

    def _get_digest_state_sync(self) -> dict[str, Any]:
        rows = self._rows(
            self._client().table("digest_state").select("*").eq("id", 1).limit(1).execute()
        )
        return rows[0] if rows else {"id": 1}

    async def update_digest_state(self, fields: dict[str, Any]) -> dict[str, Any]:
        return await self._to_thread(self._update_digest_state_sync, dict(fields))

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
        await self._to_thread(self._mark_digest_success_sync)

    def _mark_digest_success_sync(self) -> None:
        self._client().table("runtime_state").update(
            {"last_digest_success_at": datetime.now(tz=UTC).isoformat()}
        ).eq("id", 1).execute()

    async def latest_digest_runs(self, limit: int = 1) -> list[dict[str, Any]]:
        return await self.latest_jobs("daily_digest", limit)
