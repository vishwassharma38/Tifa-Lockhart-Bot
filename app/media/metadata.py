"""Technical media metadata extraction for still images, GIFs, and video."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.media.validation import MediaInfo

log = logging.getLogger(__name__)

_FFPROBE_TIMEOUT_SECONDS = 15


@dataclass(frozen=True, slots=True)
class MediaMetadata:
    """Non-sensitive technical metadata extracted from a validated file."""

    mime_type: str
    extension: str
    media_type: str
    size_bytes: int
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    frame_count: int | None = None
    is_animated: bool | None = None
    codec: str | None = None
    container_format: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def media_item_fields(self) -> dict[str, Any]:
        """Return only the columns that are safe and available to persist."""
        fields: dict[str, Any] = {
            "mime_type": self.mime_type,
            "extension": self.extension,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
        }
        if self.width is not None:
            fields["width"] = self.width
        if self.height is not None:
            fields["height"] = self.height
        if self.duration_seconds is not None:
            fields["duration_seconds"] = self.duration_seconds
        return fields


def _image_metadata(path: Path, media: MediaInfo, size_bytes: int) -> MediaMetadata:
    metadata = MediaMetadata(
        mime_type=media.mime_type,
        extension=media.extension,
        media_type=media.media_type,
        size_bytes=size_bytes,
    )
    try:
        from PIL import Image
    except Exception:  # pragma: no cover - dependency is declared, guard is defensive
        return metadata

    try:
        with Image.open(path) as image:
            width, height = image.size
            frame_count = int(getattr(image, "n_frames", 1) or 1)
            is_animated = bool(getattr(image, "is_animated", False))
            image_format = getattr(image, "format", None)
            detected_mime = None
            if isinstance(image_format, str):
                detected_mime = Image.MIME.get(image_format)
            return MediaMetadata(
                mime_type=detected_mime or media.mime_type,
                extension=media.extension,
                media_type=media.media_type,
                size_bytes=size_bytes,
                width=width if width > 0 else None,
                height=height if height > 0 else None,
                frame_count=frame_count,
                is_animated=is_animated,
                container_format=image_format if isinstance(image_format, str) else None,
            )
    except Exception as exc:
        log.warning("Image metadata extraction failed (%s)", type(exc).__name__)
        return metadata


def _run_ffprobe(path: Path) -> dict[str, Any] | None:
    executable = shutil.which("ffprobe")
    if executable is None:
        return None
    command = [
        executable,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            timeout=_FFPROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    try:
        parsed = json.loads(completed.stdout.decode("utf-8", errors="replace"))
    except (ValueError, AttributeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _video_metadata(
    path: Path,
    media: MediaInfo,
    size_bytes: int,
    probe: Callable[[Path], dict[str, Any] | None],
) -> MediaMetadata:
    metadata = MediaMetadata(
        mime_type=media.mime_type,
        extension=media.extension,
        media_type=media.media_type,
        size_bytes=size_bytes,
    )
    try:
        probed = probe(path)
    except Exception as exc:
        log.warning("Video metadata probe failed (%s)", type(exc).__name__)
        return metadata
    if not isinstance(probed, dict):
        return metadata

    streams = probed.get("streams")
    stream = None
    if isinstance(streams, list):
        for candidate in streams:
            if isinstance(candidate, dict) and candidate.get("codec_type") == "video":
                stream = candidate
                break
    container = probed.get("format")
    container_name = container.get("format_name") if isinstance(container, dict) else None

    width = _positive_int(stream.get("width")) if isinstance(stream, dict) else None
    height = _positive_int(stream.get("height")) if isinstance(stream, dict) else None
    duration = _positive_float(stream.get("duration")) if isinstance(stream, dict) else None
    if duration is None and isinstance(container, dict):
        duration = _positive_float(container.get("duration"))
    codec = stream.get("codec_name") if isinstance(stream, dict) else None

    return MediaMetadata(
        mime_type=media.mime_type,
        extension=media.extension,
        media_type=media.media_type,
        size_bytes=size_bytes,
        width=width,
        height=height,
        duration_seconds=duration,
        codec=codec if isinstance(codec, str) else None,
        container_format=container_name if isinstance(container_name, str) else None,
    )


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def extract_media_metadata(
    path: Path,
    media: MediaInfo,
    *,
    size_bytes: int,
    ffprobe_runner: Callable[[Path], dict[str, Any] | None] | None = None,
) -> MediaMetadata:
    """Extract technical metadata without modifying the original file."""
    if media.media_type in {"image", "animation"}:
        return _image_metadata(path, media, size_bytes)
    return _video_metadata(path, media, size_bytes, ffprobe_runner or _run_ffprobe)
