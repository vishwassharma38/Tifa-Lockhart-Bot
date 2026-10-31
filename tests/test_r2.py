from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError

from app.health.status import ServiceState
from app.storage.r2 import (
    R2NotConfiguredError,
    R2ObjectTooLargeError,
    R2Storage,
    R2VerificationError,
)

VERIFICATION_KEY_PREFIX = "healthchecks/"


class FakeBody:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self.closed = False

    def read(self, amount: int | None = None) -> bytes:
        if amount is None:
            return self._data
        return self._data[:amount]

    def close(self) -> None:
        self.closed = True


class FakeS3:
    def __init__(self) -> None:
        self.head_calls: list[str] = []
        self.puts: list[dict[str, Any]] = []
        self.deletes: list[str] = []
        self.objects: dict[str, bytes] = {}
        self.closed = False
        self.fail_head = False
        self.fail_get = False

    def head_bucket(self, Bucket: str) -> None:
        self.head_calls.append(Bucket)
        if self.fail_head:
            raise ClientError(
                {"Error": {"Code": "403", "Message": "Forbidden"}},
                "HeadBucket",
            )

    def put_object(self, **kwargs: Any) -> None:
        self.puts.append(kwargs)
        self.objects[kwargs["Key"]] = kwargs["Body"]

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        del Bucket
        if self.fail_get:
            raise ClientError(
                {"Error": {"Code": "404", "Message": "Not Found"}},
                "GetObject",
            )
        return {"Body": FakeBody(self.objects[Key])}

    def delete_object(self, Bucket: str, Key: str) -> None:
        del Bucket
        self.deletes.append(Key)
        self.objects.pop(Key, None)

    def close(self) -> None:
        self.closed = True


class FakeFactory:
    def __init__(self, client: FakeS3) -> None:
        self.client = client
        self.args: tuple[Any, ...] = ()
        self.kwargs: dict[str, Any] = {}

    def __call__(self, *args: Any, **kwargs: Any) -> FakeS3:
        self.args = args
        self.kwargs = kwargs
        return self.client


async def immediate_thread(function, *args, **kwargs):
    return function(*args, **kwargs)


def make_storage(client: FakeS3, **kwargs: Any) -> tuple[R2Storage, FakeFactory]:
    factory = FakeFactory(client)
    storage = R2Storage(
        "https://example.r2.cloudflarestorage.com",
        "access-key",
        "secret-key",
        "private-bucket",
        "auto",
        client_factory=factory,
        to_thread=immediate_thread,
        **kwargs,
    )
    return storage, factory


async def test_unconfigured_storage_reports_not_configured() -> None:
    factory = FakeFactory(FakeS3())
    storage = R2Storage(
        "",
        "",
        "",
        "",
        client_factory=factory,
        to_thread=immediate_thread,
    )

    assert storage.configured is False
    assert storage.initialize() is False
    status = await storage.verify()

    assert status.state is ServiceState.NOT_CONFIGURED
    assert factory.args == ()
    with pytest.raises(R2NotConfiguredError):
        await storage.upload_bytes("healthchecks/test.txt", b"data", "text/plain")


async def test_verify_initializes_client_and_checks_bucket() -> None:
    client = FakeS3()
    storage, factory = make_storage(client)

    status = await storage.verify()

    assert status.state is ServiceState.VERIFIED
    assert client.head_calls == ["private-bucket"]
    assert factory.args == ("s3",)
    assert factory.kwargs["endpoint_url"] == "https://example.r2.cloudflarestorage.com"
    assert factory.kwargs["region_name"] == "auto"
    assert factory.kwargs["aws_access_key_id"] == "access-key"
    assert factory.kwargs["aws_secret_access_key"] == "secret-key"


async def test_verify_reports_unavailable_for_bucket_or_auth_failure() -> None:
    client = FakeS3()
    client.fail_head = True
    storage, _factory = make_storage(client)

    status = await storage.verify()

    assert status.state is ServiceState.UNAVAILABLE


async def test_upload_and_download_use_private_put_without_acl() -> None:
    client = FakeS3()
    storage, _factory = make_storage(client)
    key = "assets/ab/" + ("a" * 64) + ".png"

    await storage.upload_bytes(key, b"media-data", "image/png")
    downloaded = await storage.download_bytes(key)

    assert downloaded == b"media-data"
    assert len(client.puts) == 1
    assert client.puts[0]["Key"] == key
    assert client.puts[0]["ContentType"] == "image/png"
    assert "ACL" not in client.puts[0]


async def test_download_enforces_maximum_size() -> None:
    client = FakeS3()
    storage, _factory = make_storage(client)
    key = "healthchecks/large.txt"
    await storage.upload_bytes(key, b"abcdef", "text/plain")

    with pytest.raises(R2ObjectTooLargeError):
        await storage.download_bytes(key, max_bytes=3)


async def test_verification_uploads_downloads_and_cleans_up() -> None:
    client = FakeS3()
    storage, _factory = make_storage(client)

    result = await storage.verify_upload_download(cleanup=True)

    assert result.object_key.startswith(VERIFICATION_KEY_PREFIX)
    assert result.size_bytes > 0
    assert result.cleaned_up is True
    assert client.deletes == [result.object_key]
    assert result.object_key not in client.objects
    assert client.puts[0]["Key"] == result.object_key


async def test_verification_can_leave_temporary_object_when_cleanup_disabled() -> None:
    client = FakeS3()
    storage, _factory = make_storage(client)

    result = await storage.verify_upload_download(cleanup=False)

    assert result.cleaned_up is False
    assert client.deletes == []
    assert result.object_key in client.objects


async def test_verification_cleans_up_after_download_failure() -> None:
    client = FakeS3()
    client.fail_get = True
    storage, _factory = make_storage(client)

    with pytest.raises(ClientError):
        await storage.verify_upload_download(cleanup=True)

    assert len(client.deletes) == 1
    assert client.deletes[0].startswith(VERIFICATION_KEY_PREFIX)


async def test_verification_detects_mismatched_payload() -> None:
    class CorruptingS3(FakeS3):
        def put_object(self, **kwargs: Any) -> None:
            kwargs["Body"] = b"corrupted"
            super().put_object(**kwargs)

    client = CorruptingS3()
    storage, _factory = make_storage(client)

    with pytest.raises(R2VerificationError):
        await storage.verify_upload_download(cleanup=True)

    assert len(client.deletes) == 1


async def test_invalid_object_keys_are_rejected() -> None:
    client = FakeS3()
    storage, _factory = make_storage(client)

    with pytest.raises(ValueError):
        await storage.upload_bytes("../escape.txt", b"data", "text/plain")
    with pytest.raises(ValueError):
        await storage.download_bytes("/absolute.txt")


async def test_close_releases_the_client() -> None:
    client = FakeS3()
    storage, _factory = make_storage(client)
    storage.initialize()

    await storage.close()
    await storage.close()

    assert client.closed is True
