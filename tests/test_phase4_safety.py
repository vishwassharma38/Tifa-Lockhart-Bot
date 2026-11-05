from __future__ import annotations

import hashlib

import pytest

from app.media.fetcher import MediaDownloadError, MediaFetcher, _PublicResolver
from app.storage.b2 import B2Storage


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/a.png",
        "http://10.0.0.1/a.png",
        "http://[::1]/a.png",
        "http://169.254.169.254/latest/meta-data",
    ],
)
async def test_fetcher_blocks_literal_non_public_destinations(url, tmp_path):
    fetcher = MediaFetcher()
    with pytest.raises(MediaDownloadError):
        await fetcher.download(url, destination_dir=tmp_path)


async def test_resolver_rejects_private_dns_result(monkeypatch):
    async def private_result(*_args, **_kwargs):
        return [(2, 1, 6, "", ("192.168.1.2", 443))]

    monkeypatch.setattr("asyncio.BaseEventLoop.getaddrinfo", private_result)
    with pytest.raises(OSError):
        await _PublicResolver().resolve("media.example", 443)


async def test_b2_existing_object_requires_exact_bytes(make_settings):
    settings = make_settings(
        B2_ENDPOINT_URL="https://s3.us-west-004.backblazeb2.com",
        B2_REGION="us-west-004",
        B2_BUCKET_NAME="bucket",
        B2_KEY_ID="id",
        B2_APPLICATION_KEY="key",
    )
    storage = B2Storage.from_settings(settings)
    data = b"valid object"
    storage.download_bytes = lambda *_args, **_kwargs: None

    async def retrieve(*_args, **_kwargs):
        return data

    storage.download_bytes = retrieve
    assert await storage.verify_object(
        "assets/x/file.png", sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data)
    )
    assert not await storage.verify_object(
        "assets/x/file.png", sha256="0" * 64, size_bytes=len(data)
    )
    assert not await storage.verify_object(
        "assets/x/file.png", sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data) + 1
    )
