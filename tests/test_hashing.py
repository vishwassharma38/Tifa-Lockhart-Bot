from __future__ import annotations

from pathlib import Path

from PIL import Image

from app.media.hashing import (
    DEFAULT_SIMILARITY_THRESHOLD,
    PerceptualHasher,
    find_similar_hashes,
    hamming_distance,
)
from app.media.validation import MediaInfo

PNG = MediaInfo("png", "image/png", "image")
MP4 = MediaInfo("mp4", "video/mp4", "video")


def write_png(path: Path) -> None:
    Image.new("RGB", (8, 8), (20, 30, 40)).save(path, format="PNG")


def test_disabled_hasher_returns_none(tmp_path) -> None:
    path = tmp_path / "image.png"
    write_png(path)

    assert PerceptualHasher(enabled=False).compute(path, PNG) is None


def test_enabled_hasher_computes_still_image_hash(tmp_path) -> None:
    path = tmp_path / "image.png"
    write_png(path)

    hasher = PerceptualHasher(enabled=True)
    first = hasher.compute(path, PNG)
    second = hasher.compute(path, PNG)

    assert isinstance(first, str)
    assert first == second
    assert len(first) == 16


def test_hasher_never_processes_video(tmp_path) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42")

    assert PerceptualHasher(enabled=True).compute(path, MP4) is None


def test_hamming_distance_handles_valid_and_invalid_values() -> None:
    assert hamming_distance("0000000000000000", "0000000000000000") == 0
    assert hamming_distance("0000000000000000", "000000000000000f") == 4
    assert hamming_distance("not-hex", "0000000000000000") is None


def test_find_similar_hashes_is_review_only_and_thresholded() -> None:
    candidates = [
        {"id": "close", "perceptual_hash": "000000000000000f"},
        {"id": "far", "perceptual_hash": "ffffffffffffffff"},
        {"id": "missing"},
    ]

    signals = find_similar_hashes("0000000000000000", candidates)

    assert DEFAULT_SIMILARITY_THRESHOLD == 8
    assert signals == [
        {
            "media_item_id": "close",
            "perceptual_hash": "000000000000000f",
            "distance": 4,
            "signal": "perceptual_similarity_review",
        }
    ]
