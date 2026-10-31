"""Cloudflare R2 adapter using the private S3-compatible API."""

from __future__ import annotations

import asyncio
import hmac
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.config import Config

from app.config import Settings
from app.health.status import DependencyStatus

log = logging.getLogger(__name__)

_VERIFICATION_PREFIX = "healthchecks"


class R2Error(RuntimeError):
    """Base class for R2 adapter errors with secret-free messages."""


class R2NotConfiguredError(R2Error):
    """Raised when an operation requires R2 but credentials are absent."""


class R2InitializationError(R2Error):
    """Raised when a configured R2 client cannot be created."""


class R2ObjectTooLargeError(R2Error):
    """Raised when a downloaded object exceeds the requested bound."""


class R2VerificationError(R2Error):
    """Raised when the upload/download verification does not round-trip."""


@dataclass(frozen=True, slots=True)
class R2VerificationResult:
    object_key: str
    size_bytes: int
    cleaned_up: bool


class R2Storage:
    """Small S3-compatible adapter for the private media bucket."""

    def __init__(
        self,
        endpoint_url: str,
        access_key_id: str,
        secret_access_key: str,
        bucket_name: str,
        region: str = "auto",
        *,
        client_factory: Callable[..., Any] = boto3.client,
        to_thread: Callable[..., Awaitable[Any]] = asyncio.to_thread,
        connect_timeout: float = 5.0,
        read_timeout: float = 15.0,
        operation_timeout: float = 15.0,
    ) -> None:
        self._endpoint_url = endpoint_url
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._bucket_name = bucket_name
        self._region = region or "auto"
        self._client_factory = client_factory
        self._to_thread = to_thread
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._operation_timeout = operation_timeout
        self._client: Any | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> R2Storage:
        return cls(
            endpoint_url=settings.r2_endpoint_url,
            access_key_id=settings.r2_access_key_id,
            secret_access_key=settings.r2_secret_access_key,
            bucket_name=settings.r2_bucket_name,
            region=settings.r2_region,
        )

    @property
    def configured(self) -> bool:
        return all(
            (
                self._endpoint_url,
                self._access_key_id,
                self._secret_access_key,
                self._bucket_name,
            )
        )

    @property
    def bucket_name(self) -> str:
        return self._bucket_name

    def initialize(self) -> bool:
        """Create the S3-compatible client if configured."""
        if not self.configured:
            return False
        if self._client is not None:
            return True

        config = Config(
            signature_version="s3v4",
            connect_timeout=self._connect_timeout,
            read_timeout=self._read_timeout,
            retries={"max_attempts": 2, "mode": "standard"},
            max_pool_connections=10,
        )
        try:
            self._client = self._client_factory(
                "s3",
                endpoint_url=self._endpoint_url,
                aws_access_key_id=self._access_key_id,
                aws_secret_access_key=self._secret_access_key,
                region_name=self._region,
                config=config,
            )
        except Exception as exc:
            raise R2InitializationError("R2 client initialization failed") from exc
        return True

    async def verify(self, timeout: float | None = None) -> DependencyStatus:
        """Check the configured bucket without returning or logging secrets."""
        if not self.configured:
            return DependencyStatus.not_configured()
        if self._client is None:
            try:
                self.initialize()
            except R2InitializationError:
                return DependencyStatus.unavailable()

        client = self._client
        assert client is not None

        try:
            await self._call(
                client.head_bucket,
                Bucket=self._bucket_name,
                timeout=timeout,
            )
        except TimeoutError:
            log.warning("R2 health check timed out")
            return DependencyStatus.unavailable()
        except Exception as exc:
            log.warning("R2 health check failed (%s)", type(exc).__name__)
            return DependencyStatus.unavailable()
        return DependencyStatus.verified()

    async def upload_bytes(
        self,
        object_key: str,
        payload: bytes,
        content_type: str,
        *,
        timeout: float | None = None,
    ) -> None:
        """Upload bytes to the private bucket. No public ACL is applied."""
        self._validate_key(object_key)
        if not payload:
            raise ValueError("R2 upload payload must not be empty")
        client = self._ensure_client()

        await self._call(
            client.put_object,
            Bucket=self._bucket_name,
            Key=object_key,
            Body=payload,
            ContentType=content_type,
            timeout=timeout,
        )

    async def download_bytes(
        self,
        object_key: str,
        *,
        max_bytes: int | None = None,
        timeout: float | None = None,
    ) -> bytes:
        """Download an object, optionally enforcing a hard byte ceiling."""
        self._validate_key(object_key)
        if max_bytes is not None and max_bytes < 1:
            raise ValueError("max_bytes must be positive when provided")
        client = self._ensure_client()

        def _download() -> bytes:
            response = client.get_object(Bucket=self._bucket_name, Key=object_key)
            body = response["Body"]
            try:
                if max_bytes is None:
                    data = body.read()
                else:
                    data = body.read(max_bytes + 1)
                    if len(data) > max_bytes:
                        raise R2ObjectTooLargeError("R2 object exceeds the requested size limit")
                return data
            finally:
                body.close()

        try:
            return await self._call(_download, timeout=timeout)
        except TimeoutError:
            raise R2Error("R2 download timed out") from None

    async def delete_object(self, object_key: str, *, timeout: float | None = None) -> None:
        """Delete one explicitly named object."""
        self._validate_key(object_key)
        client = self._ensure_client()
        await self._call(
            client.delete_object,
            Bucket=self._bucket_name,
            Key=object_key,
            timeout=timeout,
        )

    async def verify_upload_download(
        self,
        *,
        cleanup: bool = True,
        timeout: float | None = None,
    ) -> R2VerificationResult:
        """Upload, retrieve, compare, and optionally delete a temporary object."""
        self._ensure_client()
        object_key = f"{_VERIFICATION_PREFIX}/{uuid.uuid4().hex}.txt"
        payload = b"tifa-lockhart-bot r2 verification\n"
        uploaded = False
        cleaned_up = False

        try:
            await self.upload_bytes(
                object_key,
                payload,
                content_type="text/plain",
                timeout=timeout,
            )
            uploaded = True
            downloaded = await self.download_bytes(
                object_key,
                max_bytes=len(payload) + 1,
                timeout=timeout,
            )
            if not hmac.compare_digest(downloaded, payload):
                raise R2VerificationError("R2 verification payload did not round-trip")
        finally:
            if cleanup and uploaded:
                try:
                    await self.delete_object(object_key, timeout=timeout)
                    cleaned_up = True
                except Exception as exc:
                    log.warning(
                        "Failed to delete the R2 verification object (%s)",
                        type(exc).__name__,
                    )

        return R2VerificationResult(
            object_key=object_key,
            size_bytes=len(payload),
            cleaned_up=cleaned_up,
        )

    async def close(self) -> None:
        """Close the underlying S3 client if it exposes a close method."""
        client = self._client
        self._client = None
        if client is None:
            return
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:
                log.debug("Ignoring R2 close error (%s)", type(exc).__name__)

    def _ensure_client(self) -> Any:
        if not self.configured:
            raise R2NotConfiguredError("R2 storage is not configured")
        if self._client is None:
            self.initialize()
        client = self._client
        if client is None:
            raise R2InitializationError("R2 client is not initialized")
        return client

    async def _call(
        self,
        function: Callable[..., Any],
        *args: Any,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> Any:
        operation = self._to_thread(function, *args, **kwargs)
        return await asyncio.wait_for(
            operation,
            timeout=timeout if timeout is not None else self._operation_timeout,
        )

    @staticmethod
    def _validate_key(object_key: str) -> None:
        if not object_key or len(object_key) > 1024:
            raise ValueError("Invalid R2 object key")
        if object_key.startswith("/") or ".." in object_key.split("/"):
            raise ValueError("Invalid R2 object key")
