"""Private object storage integration."""

from __future__ import annotations

from app.config import Settings
from app.storage.b2 import B2Storage
from app.storage.base import (
    ObjectStorage,
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

__all__ = [
    "B2Storage",
    "ObjectStorage",
    "StorageAccessDeniedError",
    "StorageError",
    "StorageInitializationError",
    "StorageNotConfiguredError",
    "StorageObjectNotFoundError",
    "StorageObjectTooLargeError",
    "StorageOperationError",
    "StorageTimeoutError",
    "StorageVerificationError",
    "StorageVerificationResult",
    "create_storage",
]


def create_storage(settings: Settings) -> ObjectStorage:
    """Build the configured storage provider behind the neutral interface."""
    return B2Storage.from_settings(settings)
