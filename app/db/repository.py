"""Supabase repository methods used by the Phase 2 media pipeline."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from app.db.client import SupabaseDatabase
from app.reddit.models import RedditCandidate


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

    async def create_job_run(self, config: dict[str, Any]) -> str:
        return await self._to_thread(self._create_job_run_sync, config)

    def _create_job_run_sync(self, config: dict[str, Any]) -> str:
        rows = self._rows(
            self._client()
            .table("job_runs")
            .insert(
                {"job_type": "reddit_collection", "outcome": "running", "config_snapshot": config}
            )
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
        rows = self._rows(
            self._client()
            .table("job_runs")
            .select("id")
            .eq("job_type", "reddit_collection")
            .eq("outcome", "running")
            .execute()
        )
        for row in rows:
            self._client().table("job_runs").update(
                {
                    "outcome": "failed",
                    "error_summary": "interrupted_by_restart",
                    "finished_at": datetime.now(tz=UTC).isoformat(),
                }
            ).eq("id", row["id"]).execute()

    async def latest_reddit_runs(self, limit: int = 2) -> list[dict[str, Any]]:
        return await self._to_thread(self._latest_reddit_runs_sync, limit)

    async def operational_counts(self) -> dict[str, Any]:
        return await self._to_thread(self._operational_counts_sync)

    def _operational_counts_sync(self) -> dict[str, Any]:
        client = self._client()
        queued = (
            client.table("discoveries")
            .select("id", count="exact")
            .eq("status", "queued")
            .limit(1)
            .execute()
        )
        dumpster = (
            client.table("discoveries")
            .select("id", count="exact")
            .eq("status", "dumpster")
            .limit(1)
            .execute()
        )
        state = self._runtime_state_sync()
        return {
            "queued": int(getattr(queued, "count", None) or 0),
            "dumpster": int(getattr(dumpster, "count", None) or 0),
            "last_digest_success_at": state.get("last_digest_success_at"),
        }

    async def mark_reddit_success(self) -> None:
        await self._to_thread(self._mark_reddit_success_sync)

    def _mark_reddit_success_sync(self) -> None:
        self._client().table("runtime_state").update(
            {"last_reddit_success_at": datetime.now(tz=UTC).isoformat()}
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

    async def upsert_discovery(self, candidate: RedditCandidate) -> dict[str, Any]:
        return await self._to_thread(self._upsert_discovery_sync, candidate)

    def _upsert_discovery_sync(self, candidate: RedditCandidate) -> dict[str, Any]:
        now = datetime.now(tz=UTC).isoformat()
        metadata = dict(candidate.media_metadata)
        metadata["media_url"] = candidate.media_url
        metadata["extraction_reason"] = candidate.extraction_reason

        payload = {
            "url": candidate.source_url,
            "normalized_url": candidate.normalized_source_url,
            "source_type": "reddit",
            "source_platform_id": candidate.submission_id,
            "title": candidate.title or None,
            "description": candidate.body,
            "creator_name": candidate.author_name,
            "creator_profile_url": candidate.author_profile_url,
            "domain": urlparse(candidate.source_url).hostname or "www.reddit.com",
            "depth": 0,
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
        payload = {
            "media_item_id": media_item_id,
            "discovery_id": discovery_id,
            "source_platform": "reddit",
            "source_post_id": candidate.submission_id,
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
