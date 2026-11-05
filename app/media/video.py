"""Isolated yt-dlp-based retrieval for supported public Reddit video."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.media.fetcher import DownloadedMedia
from app.media.validation import (
    MAX_MEDIA_BYTES,
    detect_media_type,
    extension_from_url,
    ftyp_signature_size,
)

log = logging.getLogger(__name__)

_VIDEO_EXTENSIONS = frozenset({"mp4", "webm"})
_READ_CHUNK_SIZE = 64 * 1024
_COMPLETE_FORMAT = (
    "best[ext=mp4][acodec!=none][vcodec!=none]"
    "/best[ext=webm][acodec!=none][vcodec!=none]"
    "/bestvideo[ext=mp4]+bestaudio[ext=m4a]"
    "/bestvideo+bestaudio"
)


class VideoDownloadUnavailableError(RuntimeError):
    """Raised when yt-dlp is not installed or cannot be initialized."""


class VideoDownloadError(RuntimeError):
    """Raised when a supported public video cannot be acquired completely."""


class OversizeVideoError(VideoDownloadError):
    """Raised when a video exceeds the hard byte ceiling."""


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_READ_CHUNK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


class VideoDownloader:
    """Download one public video through yt-dlp without login or cookies."""

    def __init__(
        self,
        *,
        yt_dlp_module: Any | None = None,
        to_thread: Callable[..., Awaitable[Any]] = asyncio.to_thread,
    ) -> None:
        self._yt_dlp_module = yt_dlp_module
        self._to_thread = to_thread

    def _module(self) -> Any:
        if self._yt_dlp_module is not None:
            return self._yt_dlp_module
        try:
            import yt_dlp
        except Exception:
            raise VideoDownloadUnavailableError("yt-dlp is not available") from None
        self._yt_dlp_module = yt_dlp
        return yt_dlp

    async def download(
        self,
        url: str,
        *,
        destination_dir: Path,
        max_bytes: int = MAX_MEDIA_BYTES,
    ) -> DownloadedMedia:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        destination_dir.mkdir(parents=True, exist_ok=True)
        return await self._to_thread(
            self._download_sync,
            url,
            destination_dir,
            max_bytes,
        )

    def _download_sync(
        self,
        url: str,
        destination_dir: Path,
        max_bytes: int,
    ) -> DownloadedMedia:
        module = self._module()
        options = {
            "outtmpl": str(destination_dir / "video.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "max_filesize": max_bytes,
            "format": _COMPLETE_FORMAT,
            "merge_output_format": "mp4",
            "restrictfilenames": True,
            "noprogress": True,
        }

        try:
            with module.YoutubeDL(options) as downloader:
                info = downloader.extract_info(url, download=True)
        except VideoDownloadUnavailableError:
            raise
        except Exception as exc:
            raise VideoDownloadError(f"Video retrieval failed ({type(exc).__name__})") from None

        if not isinstance(info, dict):
            raise VideoDownloadError("Video retrieval returned no metadata")

        requested = info.get("requested_downloads")
        candidate_paths: list[Path] = []
        if isinstance(requested, list):
            for entry in requested:
                if not isinstance(entry, dict):
                    continue
                filepath = entry.get("filepath")
                if isinstance(filepath, str):
                    path = Path(filepath)
                    if path.is_file():
                        candidate_paths.append(path)
        if not candidate_paths:
            candidate_paths = [
                path
                for path in destination_dir.iterdir()
                if path.is_file() and not path.name.endswith((".part", ".ytdl"))
            ]
        if not candidate_paths:
            raise VideoDownloadError("Video retrieval produced no file")

        path = max(candidate_paths, key=lambda item: item.stat().st_mtime)
        size_bytes = path.stat().st_size
        if size_bytes <= 0:
            raise VideoDownloadError("Video retrieval produced an empty file")
        if size_bytes > max_bytes:
            raise OversizeVideoError("Video exceeds the configured size limit")

        with path.open("rb") as handle:
            signature = handle.read(64)
            needed = ftyp_signature_size(signature)
            if needed is not None and needed > len(signature):
                signature += handle.read(needed - len(signature))
        media = detect_media_type(signature)
        if media is None or media.extension not in _VIDEO_EXTENSIONS:
            raise VideoDownloadError("Downloaded video is not a supported mp4 or webm file")

        expected = extension_from_url(url)
        if expected is not None and expected in _VIDEO_EXTENSIONS and expected != media.extension:
            # yt-dlp may remux a public fallback into the other supported container.
            log.info(
                "Video container differs from URL hint (%s -> %s)",
                expected,
                media.extension,
            )

        return DownloadedMedia(
            path=path,
            sha256=_sha256_file(path),
            size_bytes=size_bytes,
            media=media,
            source_url=url,
            declared_content_type=media.mime_type,
        )
