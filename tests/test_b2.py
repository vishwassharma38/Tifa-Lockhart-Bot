from __future__ import annotations

import asyncio
from typing import Any

import pytest
from botocore.config import Config
from botocore.exceptions import ClientError, EndpointConnectionError

from app.health.status import ServiceState
from app.storage.b2 import B2Storage
from app.storage.base import (
    StorageAccessDeniedError,
    StorageInitializationError,
    StorageNotConfiguredError,
    StorageObjectNotFoundError,
    StorageObjectTooLargeError,
    StorageOperationError,
    StorageTimeoutError,
    StorageVerificationError,
)

VERIFICATION_KEY_PREFIX = "healthchecks/"
ENDPOINT_URL = "https://s3.us-west-004.backblazeb2.com"
REGION = "us-west-004"
KEY_ID = "test-key-id"
APPLICATION_KEY = "test-application-key"


def client_error(code: str, message: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, operation)


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
        self.head_error: Exception | None = None
        self.put_error: Exception | None = None
        self.get_error: Exception | None = None
        self.delete_error: Exception | None = None

    def head_bucket(self, Bucket: str) -> None:
        self.head_calls.append(Bucket)
        if self.head_error is not None:
            raise self.head_error

    def put_object(self, **kwargs: Any) -> None:
        if self.put_error is not None:
            raise self.put_error
        self.puts.append(kwargs)
        self.objects[kwargs["Key"]] = kwargs["Body"]

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        del Bucket
        if self.get_error is not None:
            raise self.get_error
        if Key not in self.objects:
            raise client_error("NoSuchKey", "The specified key does not exist", "GetObject")
        return {"Body": FakeBody(self.objects[Key])}

    def delete_object(self, Bucket: str, Key: str) -> None:
        del Bucket
        if self.delete_error is not None:
            raise self.delete_error
        self.deletes.append(Key)
        self.objects.pop(Key, None)

    def close(self) -> None:
        self.closed = True


class FakeFactory:
    def __init__(self, client: FakeS3) -> None:
        self.client = client
        self.args: tuple[Any, ...] = ()
        self.kwargs: dict[str, Any] = {}
        self.error: Exception | None = None

    def __call__(self, *args: Any, **kwargs: Any) -> FakeS3:
        self.args = args
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        return self.client


async def immediate_thread(function, *args, **kwargs):
    return function(*args, **kwargs)


async def slow_thread(function, *args, **kwargs):
    await asyncio.sleep(0.05)
    return function(*args, **kwargs)


def make_storage(client: FakeS3, **kwargs: Any) -> tuple[B2Storage, FakeFactory]:
    factory = FakeFactory(client)
    storage = B2Storage(
        ENDPOINT_URL,
        KEY_ID,
        APPLICATION_KEY,
        "private-bucket",
        REGION,
        client_factory=factory,
        to_thread=kwargs.pop("to_thread", immediate_thread),
        **kwargs,
    )
    return storage, factory


async def test_unconfigured_storage_reports_not_configured() -> None:
    factory = FakeFactory(FakeS3())
    storage = B2Storage(
        "",
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
    with pytest.raises(StorageNotConfiguredError):
        await storage.upload_bytes("healthchecks/test.txt", b"data", "text/plain")


async def test_verify_initializes_client_and_propagates_b2_settings() -> None:
    client = FakeS3()
    storage, factory = make_storage(client)

    status = await storage.verify()

    assert status.state is ServiceState.VERIFIED
    assert client.head_calls == ["private-bucket"]
    assert storage.bucket_name == "private-bucket"
    assert factory.args == ("s3",)
    assert factory.kwargs["endpoint_url"] == ENDPOINT_URL
    assert factory.kwargs["region_name"] == REGION
    assert factory.kwargs["aws_access_key_id"] == KEY_ID
    assert factory.kwargs["aws_secret_access_key"] == APPLICATION_KEY
    assert isinstance(factory.kwargs["config"], Config)
    assert factory.kwargs["config"].signature_version == "s3v4"
    assert factory.kwargs["config"].s3 == {"addressing_style": "path"}


async def test_verify_reports_unavailable_for_auth_permission_failure() -> None:
    client = FakeS3()
    client.head_error = client_error("403", "Forbidden", "HeadBucket")
    storage, _factory = make_storage(client)

    status = await storage.verify()

    assert status.state is ServiceState.UNAVAILABLE


async def test_verify_reports_unavailable_for_connectivity_failure() -> None:
    client = FakeS3()
    client.head_error = EndpointConnectionError(endpoint_url=ENDPOINT_URL)
    storage, _factory = make_storage(client)

    status = await storage.verify()

    assert status.state is ServiceState.UNAVAILABLE


async def test_initialization_failure_is_secret_free_and_unavailable() -> None:
    factory = FakeFactory(FakeS3())
    factory.error = RuntimeError(f"bad credentials for {APPLICATION_KEY}")
    storage = B2Storage(
        ENDPOINT_URL,
        KEY_ID,
        APPLICATION_KEY,
        "private-bucket",
        REGION,
        client_factory=factory,
        to_thread=immediate_thread,
    )

    with pytest.raises(StorageInitializationError) as exc_info:
        storage.initialize()
    assert APPLICATION_KEY not in str(exc_info.value)

    status = await storage.verify()
    assert status.state is ServiceState.UNAVAILABLE


async def test_upload_download_and_delete_use_private_operations_without_acl() -> None:
    client = FakeS3()
    storage, _factory = make_storage(client)
    key = "assets/ab/" + ("a" * 64) + ".png"

    await storage.upload_bytes(key, b"media-data", "image/png")
    downloaded = await storage.download_bytes(key)

    assert downloaded == b"media-data"
    assert len(client.puts) == 1
    assert client.puts[0]["Key"] == key
    assert client.puts[0]["ContentType"] == "image/png"
    assert client.puts[0]["Body"] == b"media-data"
    assert "ACL" not in client.puts[0]

    await storage.delete_object(key)

    assert client.deletes == [key]
    assert key not in client.objects


async def test_download_enforces_maximum_size() -> None:
    client = FakeS3()
    storage, _factory = make_storage(client)
    key = "healthchecks/large.txt"
    await storage.upload_bytes(key, b"abcdef", "text/plain")

    with pytest.raises(StorageObjectTooLargeError):
        await storage.download_bytes(key, max_bytes=3)


async def test_missing_object_download_raises_and_delete_is_idempotent() -> None:
    client = FakeS3()
    storage, _factory = make_storage(client)

    with pytest.raises(StorageObjectNotFoundError):
        await storage.download_bytes("healthchecks/missing.txt")

    await storage.delete_object("healthchecks/missing.txt")

    assert client.deletes == ["healthchecks/missing.txt"]


async def test_upload_failure_is_translated_without_leaking_credentials() -> None:
    client = FakeS3()
    client.put_error = client_error(
        "AccessDenied",
        f"denied for application_key={APPLICATION_KEY}",
        "PutObject",
    )
    storage, _factory = make_storage(client)

    with pytest.raises(StorageAccessDeniedError) as exc_info:
        await storage.upload_bytes("healthchecks/test.txt", b"data", "text/plain")

    assert APPLICATION_KEY not in str(exc_info.value)


async def test_download_failure_is_translated() -> None:
    client = FakeS3()
    client.get_error = client_error("InternalError", "backend failed", "GetObject")
    storage, _factory = make_storage(client)

    with pytest.raises(StorageOperationError):
        await storage.download_bytes("healthchecks/test.txt")


async def test_delete_failure_is_translated() -> None:
    client = FakeS3()
    client.delete_error = client_error("InternalError", "backend failed", "DeleteObject")
    storage, _factory = make_storage(client)

    with pytest.raises(StorageOperationError):
        await storage.delete_object("healthchecks/test.txt")


async def test_operation_timeout_is_bounded_and_translated() -> None:
    client = FakeS3()
    storage, _factory = make_storage(
        client,
        to_thread=slow_thread,
        operation_timeout=0.001,
    )

    with pytest.raises(StorageTimeoutError):
        await storage.upload_bytes("healthchecks/slow.txt", b"data", "text/plain")

    status = await storage.verify()
    assert status.state is ServiceState.UNAVAILABLE


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
    client.get_error = client_error("NoSuchKey", "not found", "GetObject")
    storage, _factory = make_storage(client)

    with pytest.raises(StorageObjectNotFoundError):
        await storage.verify_upload_download(cleanup=True)

    assert len(client.deletes) == 1
    assert client.deletes[0].startswith(VERIFICATION_KEY_PREFIX)


async def test_verification_cleans_up_after_upload_failure() -> None:
    client = FakeS3()
    client.put_error = client_error("AccessDenied", "denied", "PutObject")
    storage, _factory = make_storage(client)

    with pytest.raises(StorageAccessDeniedError):
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

    with pytest.raises(StorageVerificationError):
        await storage.verify_upload_download(cleanup=True)

    assert len(client.deletes) == 1


async def test_verification_never_deletes_unrelated_objects() -> None:
    client = FakeS3()
    unrelated_key = "assets/aa/" + ("a" * 64) + ".png"
    client.objects[unrelated_key] = b"unrelated"
    storage, _factory = make_storage(client)

    result = await storage.verify_upload_download(cleanup=True)

    assert client.deletes == [result.object_key]
    assert client.objects[unrelated_key] == b"unrelated"


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
