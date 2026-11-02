"""Provider-independent object storage interface and safe error types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.health.status import DependencyStatus


class StorageError(RuntimeError):
    """Base class for storage errors with secret-free messages."""


class StorageNotConfiguredError(StorageError):
    """Raised when an operation requires storage but it is not configured."""


class StorageInitializationError(StorageError):
    """Raised when a configured storage client cannot be created."""


class StorageObjectTooLargeError(StorageError):
    """Raised when a downloaded object exceeds the requested bound."""


class StorageVerificationError(StorageError):
    """Raised when an upload/download verification does not round-trip."""


class StorageObjectNotFoundError(StorageError):
    """Raised when a requested object does not exist."""


class StorageAccessDeniedError(StorageError):
    """Raised when the provider rejects authentication or permissions."""


class StorageOperationError(StorageError):
    """Raised when a storage operation fails for another reason."""


class StorageTimeoutError(StorageOperationError):
    """Raised when a storage operation exceeds its bounded timeout."""


@dataclass(frozen=True, slots=True)
class StorageVerificationResult:
    object_key: str
    size_bytes: int
    cleaned_up: bool
    cleanup_requested: bool = True
    cleanup_error: str | None = None

    @property
    def complete(self) -> bool:
        """Whether the round-trip succeeded and requested cleanup finished."""
        return not self.cleanup_requested or self.cleaned_up


class ObjectStorage(Protocol):
    """Operations the application requires from a private object store."""

    @property
    def configured(self) -> bool: ...

    @property
    def bucket_name(self) -> str: ...

    def initialize(self) -> bool: ...

    async def verify(self, timeout: float | None = None) -> DependencyStatus: ...

    async def upload_bytes(
        self,
        object_key: str,
        payload: bytes,
        content_type: str,
        *,
        timeout: float | None = None,
    ) -> None: ...

    async def download_bytes(
        self,
        object_key: str,
        *,
        max_bytes: int | None = None,
        timeout: float | None = None,
    ) -> bytes: ...

    async def delete_object(self, object_key: str, *, timeout: float | None = None) -> None: ...

    async def verify_upload_download(
        self,
        *,
        cleanup: bool = True,
        timeout: float | None = None,
    ) -> StorageVerificationResult: ...

    async def close(self) -> None: ...
