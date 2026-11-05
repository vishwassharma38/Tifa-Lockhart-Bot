"""Bounded streaming media download with signature and size validation."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp

from app.media.validation import (
    MAX_MEDIA_BYTES,
    MediaInfo,
    detect_media_type,
    extension_from_url,
    media_info_for_mime_type,
)

__all__ = [
    "DownloadedMedia",
    "MAX_MEDIA_BYTES",
    "MediaDownloadError",
    "MediaFetcher",
    "OversizeMediaError",
    "UnsupportedMediaError",
]

_DEFAULT_CHUNK_SIZE = 64 * 1024
_MAX_SIGNATURE_BYTES = 64


class MediaDownloadError(RuntimeError):
    """Raised when a media file cannot be acquired safely."""


class OversizeMediaError(MediaDownloadError):
    """Raised when a download exceeds the hard byte ceiling."""


class UnsupportedMediaError(MediaDownloadError):
    """Raised when downloaded bytes are not a supported, expected media type."""


@dataclass(frozen=True, slots=True)
class DownloadedMedia:
    """A validated media file stored in the caller's temporary directory."""

    path: Path
    sha256: str
    size_bytes: int
    media: MediaInfo
    source_url: str
    declared_content_type: str | None = None

    def read_bytes(self) -> bytes:
        """Read the validated temporary file for in-memory consumers."""
        return self.path.read_bytes()


def _parse_content_length(headers: Any) -> int | None:
    value = headers.get("Content-Length") if headers is not None else None
    if value is None and headers is not None:
        value = headers.get("content-length")
    if value is None:
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _safe_unlink(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return


class MediaFetcher:
    """Stream a public HTTP media response to disk while hashing it."""

    def __init__(
        self,
        *,
        session: Any | None = None,
        chunk_size: int = _DEFAULT_CHUNK_SIZE,
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
        max_redirects: int = 5,
    ) -> None:
        if chunk_size < 1:
            raise ValueError("chunk_size must be positive")
        self._session = session
        self._owns_session = session is None
        self._chunk_size = chunk_size
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._max_redirects = max_redirects
        self._closed = False

    async def __aenter__(self) -> MediaFetcher:
        self._get_session()
        return self

    async def __aexit__(self, *_exc_info: object) -> bool:
        await self.close()
        return False

    def _get_session(self) -> Any:
        if self._closed:
            raise MediaDownloadError("Media fetcher is closed")
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    def _timeout(self) -> Any:
        return aiohttp.ClientTimeout(
            total=None,
            connect=self._connect_timeout,
            sock_read=self._read_timeout,
        )

    async def close(self) -> None:
        session = self._session
        owns = self._owns_session
        self._session = None
        self._owns_session = False
        self._closed = True
        if owns and session is not None:
            await session.close()

    async def download(
        self,
        url: str,
        *,
        destination_dir: Path,
        max_bytes: int = MAX_MEDIA_BYTES,
    ) -> DownloadedMedia:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")

        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise MediaDownloadError("Media URL must be a public http(s) URL")

        destination_dir.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        part_path = destination_dir / f".{token}.part"
        final_path: Path | None = None
        completed = False

        try:
            session = self._get_session()
            async with session.get(
                url,
                timeout=self._timeout(),
                max_redirects=self._max_redirects,
            ) as response:
                status = int(getattr(response, "status", 0) or 0)
                if status < 200 or status >= 300:
                    raise MediaDownloadError(f"Media request failed with HTTP {status}")

                headers = getattr(response, "headers", None) or {}
                declared_content_type = headers.get("Content-Type") or headers.get("content-type")

                content_length = _parse_content_length(headers)
                if content_length is not None and content_length > max_bytes:
                    raise OversizeMediaError("Media exceeds the configured size limit")

                hasher = hashlib.sha256()
                signature = bytearray()
                total = 0
                with part_path.open("wb") as handle:
                    async for chunk in response.content.iter_chunked(self._chunk_size):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > max_bytes:
                            raise OversizeMediaError("Media exceeds the configured size limit")
                        if len(signature) < _MAX_SIGNATURE_BYTES:
                            remaining = _MAX_SIGNATURE_BYTES - len(signature)
                            signature.extend(chunk[:remaining])
                        hasher.update(chunk)
                        handle.write(chunk)

            if total == 0:
                raise MediaDownloadError("Media response was empty")

            media = detect_media_type(bytes(signature))
            if media is None:
                raise UnsupportedMediaError("Downloaded bytes are not supported media")

            expected_extension = extension_from_url(url)
            if expected_extension is not None and expected_extension != media.extension:
                raise UnsupportedMediaError(
                    "Downloaded media type does not match the source URL extension"
                )

            declared_info = media_info_for_mime_type(declared_content_type)
            if declared_info is not None and declared_info.media_type != media.media_type:
                raise UnsupportedMediaError(
                    "Downloaded media type does not match the declared content type"
                )

            final_path = destination_dir / f"{token}.{media.extension}"
            part_path.replace(final_path)
            completed = True

        except MediaDownloadError:
            _safe_unlink(part_path)
            if final_path is not None and not completed:
                _safe_unlink(final_path)
            raise
        except Exception as exc:
            _safe_unlink(part_path)
            if final_path is not None and not completed:
                _safe_unlink(final_path)
            raise MediaDownloadError(f"Media download failed ({type(exc).__name__})") from exc

        return DownloadedMedia(
            path=final_path,
            sha256=hasher.hexdigest(),
            size_bytes=total,
            media=media,
            source_url=url,
            declared_content_type=str(declared_content_type) if declared_content_type else None,
        )
