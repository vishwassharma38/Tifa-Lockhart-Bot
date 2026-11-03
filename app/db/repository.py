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

    def _client(self) -> Any:
        if not self._database.configured:
            raise RepositoryError("Supabase is not configured")
        try:
            self._database.initialize()
            return self._database.client
        except Exception as exc:
            raise RepositoryError(f"Supabase client unavailable ({type(exc).__name__})") from None

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
        response = self._client().table("media_items").insert(payload).execute()
        rows = self._rows(response)
        if rows:
            return rows[0]
        raise RepositoryError("Media item insert returned no row")

    async def record_source(
        self,
        media_item_id: str,
        candidate: RedditCandidate,
        *,
        discovery_id: str | None = None,
        media_url: str | None = None,
    ) -> dict[str, Any]:
        return await self._to_thread(
            self._record_source_sync,
            media_item_id,
            candidate,
            discovery_id,
            media_url,
        )

    def _record_source_sync(
        self,
        media_item_id: str,
        candidate: RedditCandidate,
        discovery_id: str | None,
        media_url: str | None,
    ) -> dict[str, Any]:
        payload = {
            "media_item_id": media_item_id,
            "discovery_id": discovery_id,
            "source_platform": "reddit",
            "source_post_id": candidate.submission_id,
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
            .upsert(payload, on_conflict="media_item_id,normalized_source_url")
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
