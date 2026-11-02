"""Backblaze B2 adapter using the private S3-compatible API."""

from __future__ import annotations

import asyncio
import hmac
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.config import Settings
from app.health.status import DependencyStatus
from app.storage.base import (
    StorageAccessDeniedError,
    StorageError,
    StorageInitializationError,
    StorageNotConfiguredError,
    StorageObjectNotFoundError,
    StorageObjectTooLargeError,
    StorageOperationError,
    StorageTimeoutError,
    StorageVerificationError,
    StorageVerificationResult,
)

log = logging.getLogger(__name__)

_VERIFICATION_PREFIX = "healthchecks"
_VERIFICATION_PAYLOAD = b"tifa-lockhart-bot b2 verification\n"
_MISSING_ERROR_CODES = frozenset({"404", "NoSuchKey", "NotFound"})
_ACCESS_ERROR_CODES = frozenset(
    {
        "401",
        "403",
        "AccessDenied",
        "InvalidAccessKeyId",
        "SignatureDoesNotMatch",
        "Unauthorized",
    }
)


class B2Storage:
    """Small S3-compatible adapter for the private Backblaze B2 bucket."""

    def __init__(
        self,
        endpoint_url: str,
        key_id: str,
        application_key: str,
        bucket_name: str,
        region: str,
        *,
        client_factory: Callable[..., Any] = boto3.client,
        to_thread: Callable[..., Awaitable[Any]] = asyncio.to_thread,
        connect_timeout: float = 5.0,
        read_timeout: float = 15.0,
        operation_timeout: float = 15.0,
    ) -> None:
        self._endpoint_url = endpoint_url
        self._key_id = key_id
        self._application_key = application_key
        self._bucket_name = bucket_name
        self._region = region
        self._client_factory = client_factory
        self._to_thread = to_thread
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._operation_timeout = operation_timeout
        self._client: Any | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> B2Storage:
        return cls(
            endpoint_url=settings.b2_endpoint_url,
            key_id=settings.b2_key_id,
            application_key=settings.b2_application_key,
            bucket_name=settings.b2_bucket_name,
            region=settings.b2_region,
        )

    @property
    def configured(self) -> bool:
        return all(
            (
                self._endpoint_url,
                self._key_id,
                self._application_key,
                self._bucket_name,
                self._region,
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
            s3={"addressing_style": "path"},
        )
        try:
            self._client = self._client_factory(
                "s3",
                endpoint_url=self._endpoint_url,
                aws_access_key_id=self._key_id,
                aws_secret_access_key=self._application_key,
                region_name=self._region,
                config=config,
            )
        except Exception:
            raise StorageInitializationError("B2 client initialization failed") from None
        return True

    async def verify(self, timeout: float | None = None) -> DependencyStatus:
        """Check the configured bucket without returning or logging secrets."""
        if not self.configured:
            return DependencyStatus.not_configured()
        if self._client is None:
            try:
                self.initialize()
            except StorageInitializationError:
                return DependencyStatus.unavailable()

        client = self._client
        assert client is not None

        try:
            await self._call(
                "bucket check",
                client.head_bucket,
                Bucket=self._bucket_name,
                timeout=timeout,
            )
        except StorageTimeoutError:
            log.warning("B2 health check timed out")
            return DependencyStatus.unavailable()
        except StorageError as exc:
            log.warning("B2 health check failed (%s)", type(exc).__name__)
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
            raise ValueError("B2 upload payload must not be empty")
        client = self._ensure_client()

        await self._call(
            "upload",
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
                        raise StorageObjectTooLargeError(
                            "B2 object exceeds the requested size limit"
                        )
                return data
            finally:
                body.close()

        return await self._call(
            "download",
            _download,
            timeout=timeout,
            missing_is_not_found=True,
        )

    async def delete_object(self, object_key: str, *, timeout: float | None = None) -> None:
        """Delete one explicitly named object, treating an absent object as deleted."""
        self._validate_key(object_key)
        client = self._ensure_client()
        try:
            await self._call(
                "delete",
                client.delete_object,
                Bucket=self._bucket_name,
                Key=object_key,
                timeout=timeout,
                missing_is_not_found=True,
            )
        except StorageObjectNotFoundError:
            return

    async def verify_upload_download(
        self,
        *,
        cleanup: bool = True,
        timeout: float | None = None,
    ) -> StorageVerificationResult:
        """Upload, retrieve, compare, and optionally delete a temporary object.

        B2 keeps historical object versions. Deleting the temporary object only
        removes the current version (and may create a hide marker); it does not
        permanently purge earlier versions. Cleanup is restricted to the exact
        uniquely named health-check key created by this call.
        """
        self._ensure_client()
        object_key = f"{_VERIFICATION_PREFIX}/{uuid.uuid4().hex}.txt"
        payload = _VERIFICATION_PAYLOAD

        try:
            await self.upload_bytes(
                object_key,
                payload,
                content_type="text/plain",
                timeout=timeout,
            )
            downloaded = await self.download_bytes(
                object_key,
                max_bytes=len(payload) + 1,
                timeout=timeout,
            )
            if not hmac.compare_digest(downloaded, payload):
                raise StorageVerificationError("B2 verification payload did not round-trip")
        except Exception as verification_error:
            if cleanup:
                cleanup_error = await self._cleanup_verification_object(
                    object_key,
                    timeout=timeout,
                )
                if cleanup_error is not None:
                    raise StorageVerificationError(
                        "B2 verification failed and cleanup failed "
                        f"({type(verification_error).__name__}, {cleanup_error})"
                    ) from verification_error
            raise

        cleanup_error = None
        if cleanup:
            cleanup_error = await self._cleanup_verification_object(
                object_key,
                timeout=timeout,
            )
            if cleanup_error is not None:
                log.warning(
                    "Failed to delete the B2 verification object (%s)",
                    cleanup_error,
                )

        return StorageVerificationResult(
            object_key=object_key,
            size_bytes=len(payload),
            cleaned_up=cleanup and cleanup_error is None,
            cleanup_requested=cleanup,
            cleanup_error=cleanup_error,
        )

    async def _cleanup_verification_object(
        self,
        object_key: str,
        *,
        timeout: float | None,
    ) -> str | None:
        """Delete one verification object and return an error type name on failure."""
        try:
            await self.delete_object(object_key, timeout=timeout)
        except Exception as exc:
            return type(exc).__name__
        return None

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
                log.debug("Ignoring B2 close error (%s)", type(exc).__name__)

    def _ensure_client(self) -> Any:
        if not self.configured:
            raise StorageNotConfiguredError("B2 storage is not configured")
        if self._client is None:
            self.initialize()
        client = self._client
        if client is None:
            raise StorageInitializationError("B2 client is not initialized")
        return client

    async def _call(
        self,
        operation: str,
        function: Callable[..., Any],
        *args: Any,
        timeout: float | None = None,
        missing_is_not_found: bool = False,
        **kwargs: Any,
    ) -> Any:
        operation_coro = self._to_thread(function, *args, **kwargs)
        try:
            return await asyncio.wait_for(
                operation_coro,
                timeout=timeout if timeout is not None else self._operation_timeout,
            )
        except TimeoutError:
            raise StorageTimeoutError(f"B2 {operation} timed out") from None
        except ClientError as exc:
            raise self._translate_client_error(
                exc,
                operation,
                missing_is_not_found=missing_is_not_found,
            ) from None
        except BotoCoreError:
            raise StorageOperationError(f"B2 {operation} failed") from None

    @staticmethod
    def _translate_client_error(
        exc: ClientError,
        operation: str,
        *,
        missing_is_not_found: bool,
    ) -> StorageError:
        response = exc.response if isinstance(exc.response, dict) else {}
        error = response.get("Error", {}) if isinstance(response, dict) else {}
        code = str(error.get("Code", "")) if isinstance(error, dict) else ""
        if missing_is_not_found and code in _MISSING_ERROR_CODES:
            return StorageObjectNotFoundError(f"B2 object was not found during {operation}")
        if code in _ACCESS_ERROR_CODES:
            return StorageAccessDeniedError(f"B2 {operation} was denied")
        return StorageOperationError(f"B2 {operation} failed")

    @staticmethod
    def _validate_key(object_key: str) -> None:
        if not object_key or len(object_key) > 1024:
            raise ValueError("Invalid B2 object key")
        if object_key.startswith("/") or ".." in object_key.split("/"):
            raise ValueError("Invalid B2 object key")
