from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.media.fetcher import (
    MediaDownloadError,
    MediaFetcher,
    OversizeMediaError,
    UnsupportedMediaError,
)
from app.media.validation import (
    build_object_key,
    detect_media_type,
    sanitize_filename,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"phase-two-media"
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"phase-two-media"
WEBP_BYTES = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBPVP8 " + b"media"
GIF_BYTES = b"GIF89a" + b"media"
MP4_BYTES = b"\x00\x00\x00\x18ftypisom" + b"media"
WEBM_BYTES = b"\x1a\x45\xdf\xa3" + b"media"


class FakeContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def iter_chunked(self, _size: int):
        for chunk in self._chunks:
            yield chunk


class FakeResponse:
    def __init__(
        self,
        chunks: list[bytes],
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self.content = FakeContent(chunks)

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *_exc_info: object) -> bool:
        return False


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.closed = False

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append((url, kwargs))
        return self.response

    async def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("header", "extension", "mime_type", "media_type"),
    [
        (JPEG_BYTES, "jpg", "image/jpeg", "image"),
        (PNG_BYTES, "png", "image/png", "image"),
        (GIF_BYTES, "gif", "image/gif", "animation"),
        (WEBP_BYTES, "webp", "image/webp", "image"),
        (MP4_BYTES, "mp4", "video/mp4", "video"),
        (WEBM_BYTES, "webm", "video/webm", "video"),
    ],
)
def test_detect_supported_media_signatures(header, extension, mime_type, media_type) -> None:
    info = detect_media_type(header)

    assert info is not None
    assert info.extension == extension
    assert info.mime_type == mime_type
    assert info.media_type == media_type


def test_detect_rejects_unknown_content() -> None:
    assert detect_media_type(b"<html>not media</html>") is None


def test_object_key_is_content_addressed() -> None:
    sha256 = "ab" + ("c" * 62)

    assert build_object_key(sha256, "webp") == f"assets/ab/{sha256}.webp"


def test_filename_sanitization_removes_paths_and_unsafe_characters() -> None:
    assert sanitize_filename("../../evil name?.png") == "evil_name_.png"
    assert sanitize_filename("...") == "media"


async def test_download_streams_bytes_computes_sha_and_validates(tmp_path: Path) -> None:
    data = PNG_BYTES
    session = FakeSession(
        FakeResponse(
            [data[:8], data[8:]],
            headers={"Content-Type": "image/png"},
        )
    )
    fetcher = MediaFetcher(session=session)

    result = await fetcher.download(
        "https://i.redd.it/example.png",
        destination_dir=tmp_path,
        max_bytes=100,
    )

    assert result.path.read_bytes() == data
    assert result.sha256 == hashlib.sha256(data).hexdigest()
    assert result.size_bytes == len(data)
    assert result.media.extension == "png"
    assert result.declared_content_type == "image/png"
    assert session.calls[0][1]["max_redirects"] == 5


async def test_jpeg_alias_url_is_accepted(tmp_path: Path) -> None:
    session = FakeSession(FakeResponse([JPEG_BYTES]))
    fetcher = MediaFetcher(session=session)

    result = await fetcher.download(
        "https://i.redd.it/example.jpeg",
        destination_dir=tmp_path,
        max_bytes=100,
    )

    assert result.media.extension == "jpg"
    assert result.path.suffix == ".jpg"


async def test_oversized_download_stops_at_limit_and_cleans_partial_file(
    tmp_path: Path,
) -> None:
    session = FakeSession(FakeResponse([b"abc", b"def"]))
    fetcher = MediaFetcher(session=session)

    with pytest.raises(OversizeMediaError):
        await fetcher.download(
            "https://i.redd.it/example.png",
            destination_dir=tmp_path,
            max_bytes=5,
        )

    assert list(tmp_path.iterdir()) == []


async def test_oversized_content_length_is_rejected_before_body_read(tmp_path: Path) -> None:
    session = FakeSession(FakeResponse([b"unused"], headers={"Content-Length": "999999"}))
    fetcher = MediaFetcher(session=session)

    with pytest.raises(OversizeMediaError):
        await fetcher.download(
            "https://i.redd.it/example.png",
            destination_dir=tmp_path,
            max_bytes=100,
        )

    assert list(tmp_path.iterdir()) == []


async def test_invalid_content_is_rejected_and_cleaned_up(tmp_path: Path) -> None:
    session = FakeSession(FakeResponse([b"<html>not media</html>"]))
    fetcher = MediaFetcher(session=session)

    with pytest.raises(UnsupportedMediaError):
        await fetcher.download(
            "https://i.redd.it/example.png",
            destination_dir=tmp_path,
            max_bytes=100,
        )

    assert list(tmp_path.iterdir()) == []


async def test_content_must_match_url_extension(tmp_path: Path) -> None:
    session = FakeSession(FakeResponse([JPEG_BYTES]))
    fetcher = MediaFetcher(session=session)

    with pytest.raises(UnsupportedMediaError):
        await fetcher.download(
            "https://i.redd.it/example.png",
            destination_dir=tmp_path,
            max_bytes=100,
        )

    assert list(tmp_path.iterdir()) == []


async def test_empty_response_is_rejected(tmp_path: Path) -> None:
    session = FakeSession(FakeResponse([]))
    fetcher = MediaFetcher(session=session)

    with pytest.raises(MediaDownloadError):
        await fetcher.download(
            "https://i.redd.it/example.png",
            destination_dir=tmp_path,
        )

    assert list(tmp_path.iterdir()) == []


async def test_non_http_media_url_is_rejected(tmp_path: Path) -> None:
    session = FakeSession(FakeResponse([PNG_BYTES]))
    fetcher = MediaFetcher(session=session)

    with pytest.raises(MediaDownloadError):
        await fetcher.download("file:///etc/passwd", destination_dir=tmp_path)


async def test_fetcher_closes_only_sessions_it_owns() -> None:
    injected = FakeSession(FakeResponse([PNG_BYTES]))
    async with MediaFetcher(session=injected) as fetcher:
        assert fetcher is not None
    assert injected.closed is False
