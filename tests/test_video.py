from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.media.video import (
    OversizeVideoError,
    VideoDownloader,
    VideoDownloadError,
)

MP4_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"video-data"


class FakeYoutubeDL:
    def __init__(self, options: dict[str, object], data: bytes, *, fail: bool = False) -> None:
        self.options = options
        self.data = data
        self.fail = fail
        self.urls: list[str] = []

    def __enter__(self) -> FakeYoutubeDL:
        return self

    def __exit__(self, *_exc_info: object) -> bool:
        return False

    def extract_info(self, url: str, *, download: bool) -> dict[str, object]:
        assert download is True
        self.urls.append(url)
        if self.fail:
            raise RuntimeError("extractor failed")
        path = Path(str(self.options["outtmpl"]).replace("%(ext)s", "mp4"))
        path.write_bytes(self.data)
        return {"requested_downloads": [{"filepath": str(path)}]}


class FakeYtDlpModule:
    def __init__(self, data: bytes, *, fail: bool = False) -> None:
        self.data = data
        self.fail = fail
        self.downloaders: list[FakeYoutubeDL] = []

    def YoutubeDL(self, options: dict[str, object]) -> FakeYoutubeDL:
        downloader = FakeYoutubeDL(options, self.data, fail=self.fail)
        self.downloaders.append(downloader)
        return downloader


async def test_downloader_returns_validated_mp4(tmp_path) -> None:
    module = FakeYtDlpModule(MP4_BYTES)
    downloader = VideoDownloader(yt_dlp_module=module)

    media = await downloader.download(
        "https://v.redd.it/abc123/CMAF_720.mp4",
        destination_dir=tmp_path,
        max_bytes=1024,
    )

    assert media.media.extension == "mp4"
    assert media.media.mime_type == "video/mp4"
    assert media.size_bytes == len(MP4_BYTES)
    assert media.sha256 == hashlib.sha256(MP4_BYTES).hexdigest()
    assert media.path.read_bytes() == MP4_BYTES
    assert module.downloaders[0].urls == ["https://v.redd.it/abc123/CMAF_720.mp4"]
    options = module.downloaders[0].options
    assert options["noplaylist"] is True
    assert options["merge_output_format"] == "mp4"
    assert "acodec!=none" in str(options["format"])


async def test_downloader_rejects_oversized_output(tmp_path) -> None:
    module = FakeYtDlpModule(MP4_BYTES)
    downloader = VideoDownloader(yt_dlp_module=module)

    with pytest.raises(OversizeVideoError):
        await downloader.download(
            "https://v.redd.it/abc123/CMAF_720.mp4",
            destination_dir=tmp_path,
            max_bytes=4,
        )


async def test_downloader_rejects_unsupported_bytes(tmp_path) -> None:
    module = FakeYtDlpModule(b"not-a-video")
    downloader = VideoDownloader(yt_dlp_module=module)

    with pytest.raises(VideoDownloadError):
        await downloader.download(
            "https://v.redd.it/abc123/CMAF_720.mp4",
            destination_dir=tmp_path,
            max_bytes=1024,
        )


async def test_downloader_translates_extractor_failure(tmp_path) -> None:
    module = FakeYtDlpModule(MP4_BYTES, fail=True)
    downloader = VideoDownloader(yt_dlp_module=module)

    with pytest.raises(VideoDownloadError, match="Video retrieval failed"):
        await downloader.download(
            "https://v.redd.it/abc123/CMAF_720.mp4",
            destination_dir=tmp_path,
            max_bytes=1024,
        )


async def test_downloader_rejects_non_positive_limit(tmp_path) -> None:
    downloader = VideoDownloader(yt_dlp_module=FakeYtDlpModule(MP4_BYTES))

    with pytest.raises(ValueError):
        await downloader.download(
            "https://v.redd.it/abc123/CMAF_720.mp4",
            destination_dir=tmp_path,
            max_bytes=0,
        )
