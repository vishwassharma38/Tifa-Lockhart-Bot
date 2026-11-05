from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from app.media.metadata import extract_media_metadata
from app.media.validation import MediaInfo

PNG = MediaInfo("png", "image/png", "image")
GIF = MediaInfo("gif", "image/gif", "animation")
MP4 = MediaInfo("mp4", "video/mp4", "video")


def write_png(path: Path, size: tuple[int, int]) -> None:
    Image.new("RGB", size, (12, 34, 56)).save(path, format="PNG")


def write_gif(path: Path) -> None:
    frames = [
        Image.new("RGB", (3, 4), (255, 0, 0)),
        Image.new("RGB", (3, 4), (0, 255, 0)),
    ]
    frames[0].save(path, format="GIF", save_all=True, append_images=frames[1:], duration=100)


def test_image_metadata_reports_dimensions_without_exif(tmp_path) -> None:
    path = tmp_path / "image.png"
    write_png(path, (6, 4))

    metadata = extract_media_metadata(path, PNG, size_bytes=path.stat().st_size)

    assert metadata.width == 6
    assert metadata.height == 4
    assert metadata.mime_type == "image/png"
    assert metadata.extension == "png"
    assert metadata.media_type == "image"
    assert metadata.frame_count == 1
    assert metadata.is_animated is False
    fields = metadata.media_item_fields()
    assert fields == {
        "mime_type": "image/png",
        "extension": "png",
        "media_type": "image",
        "size_bytes": path.stat().st_size,
        "width": 6,
        "height": 4,
    }
    assert "exif" not in fields


def test_gif_metadata_reports_animation_frames(tmp_path) -> None:
    path = tmp_path / "image.gif"
    write_gif(path)

    metadata = extract_media_metadata(path, GIF, size_bytes=path.stat().st_size)

    assert metadata.media_type == "animation"
    assert metadata.width == 3
    assert metadata.height == 4
    assert metadata.frame_count == 2
    assert metadata.is_animated is True


def test_video_metadata_uses_injected_probe(tmp_path) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42")

    def probe(_path: Path) -> dict[str, object]:
        return {
            "streams": [
                {"codec_type": "audio", "codec_name": "aac"},
                {"codec_type": "video", "width": "1920", "height": "1080", "codec_name": "h264"},
            ],
            "format": {"format_name": "mov,mp4,m4a", "duration": "9.75"},
        }

    metadata = extract_media_metadata(
        path,
        MP4,
        size_bytes=path.stat().st_size,
        ffprobe_runner=probe,
    )

    assert metadata.width == 1920
    assert metadata.height == 1080
    assert metadata.duration_seconds == 9.75
    assert metadata.codec == "h264"
    assert metadata.container_format == "mov,mp4,m4a"


def test_missing_video_probe_keeps_media_without_optional_fields(tmp_path) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42")

    metadata = extract_media_metadata(
        path,
        MP4,
        size_bytes=path.stat().st_size,
        ffprobe_runner=lambda _path: None,
    )

    assert metadata.media_type == "video"
    assert metadata.width is None
    assert metadata.height is None
    assert metadata.duration_seconds is None
    assert metadata.media_item_fields()["size_bytes"] == path.stat().st_size


def test_invalid_image_bytes_do_not_reject_media(tmp_path) -> None:
    path = tmp_path / "bad.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\nnot-a-real-image")

    metadata = extract_media_metadata(path, PNG, size_bytes=path.stat().st_size)

    assert metadata.media_type == "image"
    assert metadata.width is None
    assert metadata.height is None


def test_image_metadata_does_not_include_exif_location(tmp_path) -> None:
    path = tmp_path / "image.jpg"
    image = Image.new("RGB", (2, 2), (1, 2, 3))
    exif = Image.Exif()
    exif[0x010F] = "TestCamera"
    exif[0x0110] = "TestModel"
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    path.write_bytes(buffer.getvalue())

    metadata = extract_media_metadata(
        path,
        MediaInfo("jpg", "image/jpeg", "image"),
        size_bytes=path.stat().st_size,
    )

    assert metadata.width == 2
    assert metadata.height == 2
    assert "TestCamera" not in str(metadata.media_item_fields())
    assert "gps" not in str(metadata.extra).lower()
