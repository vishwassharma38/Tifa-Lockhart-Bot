"""Optional perceptual hashing for still images.

SHA-256 remains the authoritative exact-duplicate identifier. Perceptual
similarity is only a review signal and never triggers automatic merging,
deletion, or suppression.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from app.media.validation import MediaInfo

log = logging.getLogger(__name__)

DEFAULT_SIMILARITY_THRESHOLD = 8


class PerceptualHasher:
    """Compute a pHash for supported still images when explicitly enabled."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        hash_function: Any | None = None,
    ) -> None:
        self._enabled = enabled
        self._hash_function = hash_function

    @property
    def enabled(self) -> bool:
        return self._enabled

    def compute(self, path: Path, media: MediaInfo) -> str | None:
        if not self._enabled or media.media_type != "image":
            return None
        try:
            return self._compute(path)
        except Exception as exc:
            log.warning("Perceptual hash failed (%s)", type(exc).__name__)
            return None

    def _compute(self, path: Path) -> str:
        if self._hash_function is not None:
            return str(self._hash_function(path))
        import imagehash
        from PIL import Image

        with Image.open(path) as image:
            return str(imagehash.phash(image))


def hamming_distance(left: str, right: str) -> int | None:
    """Return the bit distance between two hexadecimal perceptual hashes."""
    try:
        return bin(int(left, 16) ^ int(right, 16)).count("1")
    except (TypeError, ValueError):
        return None


def find_similar_hashes(
    perceptual_hash: str,
    candidates: Iterable[dict[str, Any]],
    *,
    threshold: int = DEFAULT_SIMILARITY_THRESHOLD,
) -> list[dict[str, Any]]:
    """Return non-destructive similarity review signals for candidate rows."""
    signals: list[dict[str, Any]] = []
    for row in candidates:
        if not isinstance(row, dict):
            continue
        existing_hash = row.get("perceptual_hash")
        if not isinstance(existing_hash, str):
            continue
        distance = hamming_distance(perceptual_hash, existing_hash)
        if distance is None or distance > threshold:
            continue
        signals.append(
            {
                "media_item_id": row.get("id"),
                "perceptual_hash": existing_hash,
                "distance": distance,
                "signal": "perceptual_similarity_review",
            }
        )
    return signals
