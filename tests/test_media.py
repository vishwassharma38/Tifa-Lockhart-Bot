from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import aiohttp
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


async def no_sleep(_delay: float) -> None:
    return None


class FakeContent:
    def __init__(self, response, chunks: list[bytes], error: BaseException | None = None) -> None:
        self._response = response
        self._chunks = chunks
        self._error = error

    async def iter_chunked(self, _size: int):
        assert self._response.active, "response body read after response context was released"
        for chunk in self._chunks:
            yield chunk
        if self._error is not None:
            raise self._error


class FakeResponse:
    def __init__(
        self,
        chunks: list[bytes],
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self.active = False
        self.closed = False
        self.enter_count = 0
        self.content = FakeContent(self, chunks, error)

    async def __aenter__(self) -> FakeResponse:
        assert not self.closed, "released response context reused"
        self.enter_count += 1
        self.active = True
        return self

    async def __aexit__(self, *_exc_info: object) -> bool:
        self.active = False
        self.closed = True
        return False


class FakeSession:
    def __init__(self, response: FakeResponse | list[FakeResponse]) -> None:
        self.responses = response if isinstance(response, list) else [response]
        self.response_index = 0
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.closed = False

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append((url, kwargs))
        response = self.responses[min(self.response_index, len(self.responses) - 1)]
        self.response_index += 1
        return response

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
    assert session.responses[0].closed
    assert session.responses[0].enter_count == 1


async def test_404_is_permanent_and_not_retried(tmp_path: Path) -> None:
    session = FakeSession(FakeResponse([], status=404))
    fetcher = MediaFetcher(session=session, max_attempts=4, sleep=no_sleep)

    with pytest.raises(MediaDownloadError) as raised:
        await fetcher.download("https://i.redd.it/missing.png", destination_dir=tmp_path)

    assert raised.value.category == "http_error"
    assert raised.value.status_code == 404
    assert len(session.calls) == 1
    assert session.responses[0].closed


async def test_429_numeric_retry_after_is_bounded(tmp_path: Path) -> None:
    session = FakeSession(
        [
            FakeResponse([], status=429, headers={"Retry-After": "99"}),
            FakeResponse([PNG_BYTES]),
        ]
    )
    delays = []

    async def record_delay(delay):
        delays.append(delay)

    fetcher = MediaFetcher(
        session=session,
        max_attempts=2,
        max_retry_delay=2,
        sleep=record_delay,
        jitter=lambda *_args: 0,
    )

    result = await fetcher.download("https://i.redd.it/retry.png", destination_dir=tmp_path)

    assert result.path.read_bytes() == PNG_BYTES
    assert delays == [2]
    assert len(session.calls) == 2
    assert all(response.closed for response in session.responses)


async def test_429_http_date_retry_after_is_parsed(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    retry_at = datetime.now(UTC) + timedelta(seconds=4)
    session = FakeSession(
        [
            FakeResponse([], status=429, headers={"Retry-After": format_datetime(retry_at)}),
            FakeResponse([PNG_BYTES]),
        ]
    )
    delays = []

    async def record_delay(delay):
        delays.append(delay)

    fetcher = MediaFetcher(
        session=session,
        max_attempts=2,
        max_retry_delay=10,
        sleep=record_delay,
        jitter=lambda *_args: 0,
    )

    await fetcher.download("https://i.redd.it/retry.png", destination_dir=tmp_path)

    assert len(delays) == 1
    assert 0 <= delays[0] <= 4


async def test_server_error_retries_with_a_fresh_response(tmp_path: Path) -> None:
    session = FakeSession([FakeResponse([], status=500), FakeResponse([PNG_BYTES])])
    fetcher = MediaFetcher(session=session, max_attempts=2, sleep=no_sleep, jitter=lambda *_args: 0)

    result = await fetcher.download("https://i.redd.it/retry.png", destination_dir=tmp_path)

    assert result.path.read_bytes() == PNG_BYTES
    assert len(session.calls) == 2
    assert all(response.closed for response in session.responses)


async def test_connection_reset_retries_and_preserves_exhausted_cause(tmp_path: Path) -> None:
    reset = aiohttp.ClientConnectionResetError("connection reset")
    session = FakeSession(
        [
            FakeResponse([], error=reset),
            FakeResponse([PNG_BYTES]),
        ]
    )
    fetcher = MediaFetcher(session=session, max_attempts=2, sleep=no_sleep, jitter=lambda *_args: 0)

    result = await fetcher.download("https://i.redd.it/retry.png", destination_dir=tmp_path)
    assert result.path.read_bytes() == PNG_BYTES
    assert len(session.calls) == 2

    exhausted = MediaFetcher(
        session=FakeSession(FakeResponse([], error=reset)), max_attempts=1, sleep=no_sleep
    )
    with pytest.raises(MediaDownloadError) as raised:
        await exhausted.download("https://i.redd.it/reset.png", destination_dir=tmp_path)
    assert raised.value.category == "connection_reset"
    assert raised.value.__cause__ is reset
    assert raised.value.original_exception is reset


async def test_timeout_retries_and_cancellation_propagates(tmp_path: Path) -> None:
    timed_out = TimeoutError("read timed out")
    session = FakeSession([FakeResponse([], error=timed_out), FakeResponse([PNG_BYTES])])
    fetcher = MediaFetcher(session=session, max_attempts=2, sleep=no_sleep, jitter=lambda *_args: 0)
    result = await fetcher.download("https://i.redd.it/retry.png", destination_dir=tmp_path)
    assert result.path.read_bytes() == PNG_BYTES

    async def cancel(_delay):
        raise asyncio.CancelledError

    cancel_session = FakeSession(FakeResponse([], status=500))
    canceled = MediaFetcher(session=cancel_session, max_attempts=2, sleep=cancel)
    with pytest.raises(asyncio.CancelledError):
        await canceled.download("https://i.redd.it/retry.png", destination_dir=tmp_path)
    assert cancel_session.responses[0].closed


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


async def test_reddit_auto_webp_preview_uses_detected_format(tmp_path: Path) -> None:
    session = FakeSession(FakeResponse([WEBP_BYTES], headers={"Content-Type": "image/webp"}))
    fetcher = MediaFetcher(session=session)

    result = await fetcher.download(
        "https://preview.redd.it/example.jpg?width=640&auto=webp&s=test",
        destination_dir=tmp_path,
    )

    assert result.media.extension == "webp"
    assert result.path.suffix == ".webp"
    assert result.sha256 == hashlib.sha256(WEBP_BYTES).hexdigest()
    assert result.path.read_bytes() == WEBP_BYTES


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/example.jpg?auto=webp",
        "https://preview.redd.it/example.jpg?width=640",
        "https://preview.redd.it/example.jpg?width=640&auto=png",
    ],
)
async def test_format_mismatch_without_reddit_webp_negotiation_is_rejected(url, tmp_path: Path) -> None:
    session = FakeSession(FakeResponse([WEBP_BYTES], headers={"Content-Type": "image/webp"}))
    fetcher = MediaFetcher(session=session)

    with pytest.raises(UnsupportedMediaError):
        await fetcher.download(url, destination_dir=tmp_path)

    assert list(tmp_path.iterdir()) == []


async def test_reddit_negotiated_format_still_checks_content_type(tmp_path: Path) -> None:
    session = FakeSession(FakeResponse([WEBP_BYTES], headers={"Content-Type": "image/jpeg"}))
    fetcher = MediaFetcher(session=session)

    with pytest.raises(UnsupportedMediaError):
        await fetcher.download(
            "https://preview.redd.it/example.jpg?width=640&auto=webp",
            destination_dir=tmp_path,
        )

    assert list(tmp_path.iterdir()) == []


async def test_avif_remains_unsupported_even_with_reddit_webp_parameter(tmp_path: Path) -> None:
    avif_bytes = b"\x00\x00\x00\x18ftypavif" + b"media"
    assert detect_media_type(avif_bytes) is None
    session = FakeSession(FakeResponse([avif_bytes], headers={"Content-Type": "image/avif"}))
    fetcher = MediaFetcher(session=session)

    with pytest.raises(UnsupportedMediaError):
        await fetcher.download(
            "https://preview.redd.it/example.jpg?auto=webp",
            destination_dir=tmp_path,
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

    assert session.calls == []


async def test_fetcher_closes_only_sessions_it_owns() -> None:
    injected = FakeSession(FakeResponse([PNG_BYTES]))
    async with MediaFetcher(session=injected) as fetcher:
        assert fetcher is not None
    assert injected.closed is False
