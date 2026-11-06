"""Media type detection, filename safety, and content-addressed object keys."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

MAX_MEDIA_BYTES = 10_000_000
# The ftyp box is normally small. This bounds signature buffering for unusual
# files with many compatible brands while still allowing complete parsing.
MAX_FTYP_BOX_BYTES = 4096

SUPPORTED_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "webp", "gif", "mp4", "webm"})
IMAGE_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "webp"})
VIDEO_EXTENSIONS = frozenset({"mp4", "webm"})

_EXTENSION_ALIASES = {"jpeg": "jpg"}

_UNSAFE_FILENAME_CHARACTERS = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_FILENAME_LENGTH = 150


@dataclass(frozen=True, slots=True)
class MediaInfo:
    """Detected media format with a normalized extension and MIME type."""

    extension: str
    mime_type: str
    media_type: str


_BY_EXTENSION = {
    "jpg": MediaInfo("jpg", "image/jpeg", "image"),
    "png": MediaInfo("png", "image/png", "image"),
    "webp": MediaInfo("webp", "image/webp", "image"),
    "gif": MediaInfo("gif", "image/gif", "animation"),
    "mp4": MediaInfo("mp4", "video/mp4", "video"),
    "webm": MediaInfo("webm", "video/webm", "video"),
}

_BY_MIME = {
    "image/jpeg": _BY_EXTENSION["jpg"],
    "image/jpg": _BY_EXTENSION["jpg"],
    "image/png": _BY_EXTENSION["png"],
    "image/webp": _BY_EXTENSION["webp"],
    "image/gif": _BY_EXTENSION["gif"],
    "video/mp4": _BY_EXTENSION["mp4"],
    "video/webm": _BY_EXTENSION["webm"],
}


def normalize_extension(extension: str | None) -> str | None:
    """Return a canonical supported extension or ``None`` when unsupported."""
    if not extension:
        return None
    cleaned = extension.strip().lower().lstrip(".")
    cleaned = _EXTENSION_ALIASES.get(cleaned, cleaned)
    return cleaned if cleaned in SUPPORTED_EXTENSIONS else None


def media_info_for_extension(extension: str | None) -> MediaInfo | None:
    normalized = normalize_extension(extension)
    return _BY_EXTENSION.get(normalized) if normalized else None


def media_info_for_mime_type(mime_type: str | None) -> MediaInfo | None:
    """Resolve a declared MIME type, ignoring parameters and casing."""
    if not mime_type:
        return None
    base = mime_type.split(";", 1)[0].strip().lower()
    return _BY_MIME.get(base)


def ftyp_signature_size(header: bytes) -> int | None:
    """Return a safe declared ftyp size to read, if a supported box is present."""
    if len(header) < 8 or header[4:8] != b"ftyp":
        return None

    box_size = int.from_bytes(header[:4], "big")
    box_header_size = 8
    if box_size == 1:
        if len(header) < 16:
            return 16
        box_size = int.from_bytes(header[8:16], "big")
        box_header_size = 16
    elif box_size == 0:
        # A box extending to EOF cannot be validated from a signature prefix.
        return None

    brand_data_start = box_header_size + 8  # major_brand and minor_version
    if (
        box_size < brand_data_start
        or box_size > MAX_FTYP_BOX_BYTES
        or (box_size - brand_data_start) % 4 != 0
    ):
        return None
    return box_size


def _ftyp_brands(header: bytes) -> frozenset[bytes] | None:
    """Parse a complete leading ISO BMFF File Type Box, if present."""
    box_size = ftyp_signature_size(header)
    if box_size is None or box_size > len(header):
        return None
    box_header_size = 16 if int.from_bytes(header[:4], "big") == 1 else 8
    brand_data_start = box_header_size + 8  # major_brand and minor_version

    brands = {header[box_header_size : box_header_size + 4]}
    brands.update(header[offset : offset + 4] for offset in range(brand_data_start, box_size, 4))
    return frozenset(brands)


_AVIF_BRANDS = frozenset({b"avif", b"avis", b"avio", b"MA1A", b"MA1B"})
_HEIF_BRANDS = frozenset(
    {
        # Structural and codec-specific HEIF brands.
        b"heic",
        b"heix",
        b"hevc",
        b"hevx",
        b"heim",
        b"heis",
        b"hevm",
        b"hevs",
        b"mif1",
        b"mif2",
        b"msf1",
        b"msf2",
        b"avci",
        b"avcs",
        b"vvic",
        b"vvis",
        b"jpeg",
        b"jpgs",
        b"evbi",
        b"evbs",
        b"evmi",
        b"evms",
        b"1pic",
        b"pred",
        b"j2ki",
        b"j2is",
        b"jxsi",
        b"jxss",
    }
)


def _iso_bmff_image_format(header: bytes) -> str | None:
    """Identify AVIF or HEIF brands, preferring codec-specific AVIF brands."""
    brands = _ftyp_brands(header)
    if brands is None:
        return None
    if brands & _AVIF_BRANDS:
        return "avif"
    if brands & _HEIF_BRANDS:
        return "heif"
    return None


def detect_media_type(header: bytes) -> MediaInfo | None:
    """Detect a supported media format from the leading bytes of a file.

    Remote extensions and ``Content-Type`` headers are never trusted; callers
    pass the actual downloaded signature bytes to this function.
    """
    if not header:
        return None

    if header.startswith(b"\xff\xd8\xff"):
        return _BY_EXTENSION["jpg"]
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return _BY_EXTENSION["png"]
    if header.startswith((b"GIF87a", b"GIF89a")):
        return _BY_EXTENSION["gif"]
    if len(header) >= 12 and header[0:4] == b"RIFF" and header[8:12] == b"WEBP":
        return _BY_EXTENSION["webp"]
    if header[4:8] == b"ftyp":
        brands = _ftyp_brands(header)
        if brands is None or brands & (_AVIF_BRANDS | _HEIF_BRANDS):
            return None
        return _BY_EXTENSION["mp4"]
    if header.startswith(b"\x1a\x45\xdf\xa3"):
        return _BY_EXTENSION["webm"]
    return None


def extension_from_url(url: str) -> str | None:
    """Return the normalized extension encoded in a public media URL."""
    try:
        path = urlparse(url).path
    except ValueError:
        return None
    raw = PurePosixPath(unquote(path)).suffix
    return normalize_extension(raw)


def build_object_key(sha256: str, extension: str) -> str:
    """Build the content-addressed private object key for a media file."""
    if not re.fullmatch("[0-9a-f]{64}", sha256):
        raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
    normalized = normalize_extension(extension)
    if normalized is None:
        raise ValueError("Unsupported media extension")
    return f"assets/{sha256[:2]}/{sha256}.{normalized}"


def sanitize_filename(name: str) -> str:
    """Strip directory components and unsafe characters from a filename."""
    candidate = str(name).replace("\\", "/")
    basename = PurePosixPath(candidate).name
    cleaned = _UNSAFE_FILENAME_CHARACTERS.sub("_", basename)
    cleaned = cleaned.strip(" .")
    if not cleaned:
        return "media"
    if len(cleaned) > _MAX_FILENAME_LENGTH:
        suffix = PurePosixPath(cleaned).suffix
        stem = cleaned[: _MAX_FILENAME_LENGTH - len(suffix)]
        cleaned = f"{stem}{suffix}"
    return cleaned
