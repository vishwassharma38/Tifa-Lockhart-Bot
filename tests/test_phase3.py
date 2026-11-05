from __future__ import annotations

import hashlib
import io
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image

from app.media.fetcher import DownloadedMedia, MediaDownloadError
from app.media.hashing import PerceptualHasher
from app.media.validation import MediaInfo, build_object_key, detect_media_type
from app.media.video import VideoDownloadUnavailableError
from app.pipeline.phase2 import Phase2Pipeline, Phase2Status
from app.reddit.models import MediaAsset, RedditCandidate

MP4_MEDIA = MediaInfo("mp4", "video/mp4", "video")


def png_bytes(size: tuple[int, int], color: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def make_candidate(assets: tuple[MediaAsset, ...]) -> RedditCandidate:
    return RedditCandidate(
        submission_id="abc123",
        permalink="https://www.reddit.com/r/Tifa/comments/abc123/a_post",
        source_url="https://www.reddit.com/r/Tifa/comments/abc123/a_post",
        title="Tifa gallery",
        body="Original artwork",
        subreddit="Tifa",
        author_name="artist_name",
        author_profile_url="https://www.reddit.com/user/artist_name",
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
        media_url=None,
        media_assets=assets,
    )


class MappingFetcher:
    def __init__(self, payloads: dict[str, bytes | Exception]) -> None:
        self.payloads = payloads
        self.calls: list[str] = []

    async def download(
        self,
        url: str,
        *,
        destination_dir: Path,
        max_bytes: int,
    ) -> DownloadedMedia:
        del max_bytes
        self.calls.append(url)
        value = self.payloads[url]
        if isinstance(value, Exception):
            raise value
        media = detect_media_type(value)
        assert media is not None
        path = destination_dir / f"{hashlib.sha256(value).hexdigest()[:8]}.{media.extension}"
        path.write_bytes(value)
        return DownloadedMedia(
            path=path,
            sha256=hashlib.sha256(value).hexdigest(),
            size_bytes=len(value),
            media=media,
            source_url=url,
        )


class FakeVideoDownloader:
    def __init__(self, *, data: bytes | None = None, error: Exception | None = None) -> None:
        self.data = data
        self.error = error
        self.calls: list[str] = []

    async def download(
        self,
        url: str,
        *,
        destination_dir: Path,
        max_bytes: int,
    ) -> DownloadedMedia:
        del max_bytes
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        assert self.data is not None
        path = destination_dir / "video.mp4"
        path.write_bytes(self.data)
        return DownloadedMedia(
            path=path,
            sha256=hashlib.sha256(self.data).hexdigest(),
            size_bytes=len(self.data),
            media=MP4_MEDIA,
            source_url=url,
        )


class FakeRepository:
    def __init__(
        self,
        *,
        existing_by_sha: dict[str, dict[str, object]] | None = None,
        hashes: list[dict[str, object]] | None = None,
        fail_discovery: bool = False,
        fail_source: bool = False,
        recover_on_create: bool = False,
    ) -> None:
        self.existing_by_sha = existing_by_sha or {}
        self.hashes = hashes or []
        self.fail_discovery = fail_discovery
        self.fail_source = fail_source
        self.recover_on_create = recover_on_create
        self.created_media: list[dict[str, object]] = []
        self.sources: list[dict[str, object]] = []
        self.metadata_updates: list[tuple[str, dict[str, object]]] = []
        self.publications: list[str] = []
        self.publication_updates: list[dict[str, object]] = []
        self._next_id = 0

    async def get_media_by_sha256(self, sha256: str) -> dict[str, object] | None:
        return self.existing_by_sha.get(sha256)

    async def upsert_discovery(self, candidate: RedditCandidate) -> dict[str, object]:
        del candidate
        if self.fail_discovery:
            raise RuntimeError("discovery insert failed")
        return {"id": "discovery-1"}

    async def create_media_item(self, payload: dict[str, object]) -> dict[str, object]:
        sha256 = str(payload["sha256"])
        if self.recover_on_create:
            return {"id": "concurrent-media", "sha256": sha256, "_recovered": True}
        self._next_id += 1
        row = {"id": f"media-{self._next_id}", **payload}
        self.created_media.append(payload)
        self.existing_by_sha[sha256] = row
        return row

    async def update_media_item_metadata(
        self,
        media_item_id: str,
        metadata: dict[str, object],
    ) -> dict[str, object]:
        self.metadata_updates.append((media_item_id, metadata))
        return {"id": media_item_id, **metadata}

    async def list_media_with_perceptual_hash(self) -> list[dict[str, object]]:
        return self.hashes

    async def record_source(
        self,
        media_item_id: str,
        candidate: RedditCandidate,
        *,
        discovery_id: str | None = None,
        media_url: str | None = None,
        source_media_id: str | None = None,
    ) -> dict[str, object]:
        if self.fail_source:
            raise RuntimeError("source insert failed")
        row = {
            "media_item_id": media_item_id,
            "submission_id": candidate.submission_id,
            "discovery_id": discovery_id,
            "media_url": media_url,
            "source_media_id": source_media_id,
        }
        self.sources.append(row)
        return row

    async def create_publication(
        self,
        media_item_id: str,
        channel_id: int,
        purpose: str,
    ) -> dict[str, object]:
        del channel_id, purpose
        self.publications.append(media_item_id)
        return {"id": f"publication-{len(self.publications)}"}

    async def update_publication(
        self,
        publication_id: str,
        *,
        status: str,
        message_id: int | None = None,
        last_error: str | None = None,
    ) -> dict[str, object]:
        row = {
            "id": publication_id,
            "status": status,
            "message_id": message_id,
            "last_error": last_error,
        }
        self.publication_updates.append(row)
        return row


class FakeStorage:
    bucket_name = "private-bucket"
    configured = True

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.uploads: list[str] = []
        self.downloads: list[str] = []

    async def object_exists(self, object_key: str, *, timeout: float | None = None) -> bool:
        del timeout
        return object_key in self.objects

    async def verify_object(
        self, object_key: str, *, sha256: str, size_bytes: int, timeout: float | None = None
    ) -> bool:
        del timeout
        import hashlib

        data = self.objects.get(object_key, b"")
        return len(data) == size_bytes and hashlib.sha256(data).hexdigest() == sha256

    async def upload_file(
        self,
        object_key: str,
        file_path: Path,
        content_type: str,
        *,
        timeout: float | None = None,
    ) -> None:
        del content_type, timeout
        self.uploads.append(object_key)
        self.objects[object_key] = file_path.read_bytes()

    async def download_bytes(
        self,
        object_key: str,
        *,
        max_bytes: int | None = None,
        timeout: float | None = None,
    ) -> bytes:
        del max_bytes, timeout
        self.downloads.append(object_key)
        return self.objects[object_key]


class FakePublisher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def publish(
        self,
        *,
        filename: str,
        data: bytes,
        candidate: RedditCandidate,
    ) -> int:
        del data, candidate
        self.calls.append(filename)
        return 500 + len(self.calls)


def build_pipeline(
    make_settings,
    tmp_path: Path,
    *,
    candidate: RedditCandidate,
    fetcher: MappingFetcher,
    repository: FakeRepository,
    storage: FakeStorage,
    publisher: FakePublisher,
    video_downloader=None,
    perceptual_hasher: PerceptualHasher | None = None,
    ffprobe_runner=None,
) -> Phase2Pipeline:
    class Connector:
        async def fetch_candidate(self, reference: str) -> RedditCandidate:
            del reference
            return candidate

    return Phase2Pipeline(
        settings=make_settings(DISCORD_DISCOVERIES_CHANNEL_ID="999"),
        connector=Connector(),
        fetcher=fetcher,
        repository=repository,
        storage=storage,
        publisher=publisher,
        temp_dir_factory=lambda: str(tmp_path / "work"),
        video_downloader=video_downloader,
        perceptual_hasher=perceptual_hasher,
        ffprobe_runner=ffprobe_runner,
    )


async def test_gallery_assets_are_stored_sourced_and_published_individually(
    make_settings,
    tmp_path,
) -> None:
    first = png_bytes((4, 3), (255, 0, 0))
    second = png_bytes((5, 6), (0, 255, 0))
    third = png_bytes((7, 8), (0, 0, 255))
    urls = [
        "https://i.redd.it/one.png",
        "https://i.redd.it/two.png",
        "https://i.redd.it/three.png",
    ]
    assets = tuple(
        MediaAsset(
            url=url,
            source_media_id=f"gallery-{index}",
            filename=f"reddit-abc123-{index + 1}.png",
            gallery_index=index,
        )
        for index, url in enumerate(urls)
    )
    fetcher = MappingFetcher(dict(zip(urls, [first, second, third], strict=True)))
    repository = FakeRepository()
    storage = FakeStorage()
    publisher = FakePublisher()
    pipeline = build_pipeline(
        make_settings,
        tmp_path,
        candidate=make_candidate(assets),
        fetcher=fetcher,
        repository=repository,
        storage=storage,
        publisher=publisher,
    )

    results = await pipeline.run_all("abc123")

    assert [result.status for result in results] == [Phase2Status.PUBLISHED] * 3
    assert len(storage.uploads) == 3
    assert len(set(storage.uploads)) == 3
    assert [row["source_media_id"] for row in repository.sources] == [
        "gallery-0",
        "gallery-1",
        "gallery-2",
    ]
    assert [row["media_url"] for row in repository.sources] == urls
    assert publisher.calls == [
        "reddit-abc123-1.png",
        "reddit-abc123-2.png",
        "reddit-abc123-3.png",
    ]
    assert len(repository.publications) == 3
    assert [row["width"] for row in repository.created_media] == [4, 5, 7]
    assert [row["height"] for row in repository.created_media] == [3, 6, 8]
    assert all(row["category"] == "other" for row in repository.created_media)
    assert not (tmp_path / "work").exists()


async def test_one_gallery_failure_does_not_block_other_assets(make_settings, tmp_path) -> None:
    first = png_bytes((2, 2), (255, 0, 0))
    third = png_bytes((3, 3), (0, 0, 255))
    urls = [
        "https://i.redd.it/one.png",
        "https://i.redd.it/two.png",
        "https://i.redd.it/three.png",
    ]
    assets = tuple(
        MediaAsset(
            url=url,
            source_media_id=f"gallery-{index}",
            gallery_index=index,
        )
        for index, url in enumerate(urls)
    )
    fetcher = MappingFetcher(
        {
            urls[0]: first,
            urls[1]: MediaDownloadError("network failed"),
            urls[2]: third,
        }
    )
    repository = FakeRepository()
    storage = FakeStorage()
    publisher = FakePublisher()
    pipeline = build_pipeline(
        make_settings,
        tmp_path,
        candidate=make_candidate(assets),
        fetcher=fetcher,
        repository=repository,
        storage=storage,
        publisher=publisher,
    )

    results = await pipeline.run_all("abc123")

    assert [result.status for result in results] == [
        Phase2Status.PUBLISHED,
        Phase2Status.FAILED,
        Phase2Status.PUBLISHED,
    ]
    assert results[1].reason == "media_download_failed"
    assert len(storage.uploads) == 2
    assert len(publisher.calls) == 2

    aggregate = await pipeline.run("abc123")

    assert aggregate.status is Phase2Status.FAILED
    assert aggregate.reason == "partial_failure"
    assert aggregate.details["asset_count"] == 3
    assert aggregate.details["duplicate_count"] == 2
    assert aggregate.details["failed_count"] == 1


async def test_duplicate_keeps_per_asset_source_and_review_signal(make_settings, tmp_path) -> None:
    data = png_bytes((4, 4), (10, 20, 30))
    sha256 = hashlib.sha256(data).hexdigest()
    asset = MediaAsset(
        url="https://i.redd.it/one.png",
        source_media_id="gallery-0",
        filename="reddit-abc123-1.png",
        gallery_index=0,
    )
    fetcher = MappingFetcher({asset.url: data})
    repository = FakeRepository(
        existing_by_sha={sha256: {"id": "existing-media", "sha256": sha256}},
        hashes=[{"id": "similar-media", "perceptual_hash": "ffffffffffffffff"}],
    )
    storage = FakeStorage()
    publisher = FakePublisher()
    hasher = PerceptualHasher(enabled=True, hash_function=lambda _path: "ffffffffffffffff")
    pipeline = build_pipeline(
        make_settings,
        tmp_path,
        candidate=make_candidate((asset,)),
        fetcher=fetcher,
        repository=repository,
        storage=storage,
        publisher=publisher,
        perceptual_hasher=hasher,
    )

    result = await pipeline.run("abc123")

    assert result.status is Phase2Status.DUPLICATE
    assert result.media_item_id == "existing-media"
    assert result.details["similar_media"] == [
        {
            "media_item_id": "similar-media",
            "perceptual_hash": "ffffffffffffffff",
            "distance": 0,
            "signal": "perceptual_similarity_review",
        }
    ]
    assert storage.uploads == []
    assert publisher.calls == []
    assert repository.sources[0]["source_media_id"] == "gallery-0"
    assert repository.metadata_updates[0][0] == "existing-media"
    assert repository.metadata_updates[0][1]["width"] == 4
    assert repository.metadata_updates[0][1]["perceptual_hash"] == "ffffffffffffffff"


async def test_video_asset_uses_video_downloader_and_probe_metadata(
    make_settings, tmp_path
) -> None:
    data = b"\x00\x00\x00\x18ftypmp42" + b"video-payload"
    asset = MediaAsset(
        url="https://v.redd.it/abc123/CMAF_720.mp4?source=fallback",
        source_media_id="https://v.redd.it/abc123/DASHPlaylist.mpd",
        filename="reddit-abc123.mp4",
        requires_video_download=True,
        download_url="https://www.reddit.com/r/Tifa/comments/abc123/a_post/",
    )
    fetcher = MappingFetcher({})
    repository = FakeRepository()
    storage = FakeStorage()
    publisher = FakePublisher()
    video_downloader = FakeVideoDownloader(data=data)

    def probe(_path: Path) -> dict[str, object]:
        return {
            "streams": [
                {"codec_type": "video", "width": 1280, "height": 720, "codec_name": "h264"}
            ],
            "format": {"format_name": "mov,mp4", "duration": "12.5"},
        }

    pipeline = build_pipeline(
        make_settings,
        tmp_path,
        candidate=make_candidate((asset,)),
        fetcher=fetcher,
        repository=repository,
        storage=storage,
        publisher=publisher,
        video_downloader=video_downloader,
        ffprobe_runner=probe,
    )

    result = await pipeline.run("abc123")

    assert result.status is Phase2Status.PUBLISHED
    assert video_downloader.calls == [asset.download_url]
    assert fetcher.calls == []
    assert repository.created_media[0]["category"] == "video"
    assert repository.created_media[0]["width"] == 1280
    assert repository.created_media[0]["height"] == 720
    assert repository.created_media[0]["duration_seconds"] == 12.5
    assert repository.sources[0]["source_media_id"] == asset.source_media_id
    assert publisher.calls == ["reddit-abc123.mp4"]


async def test_video_downloader_unavailable_is_skipped_without_database_writes(
    make_settings,
    tmp_path,
) -> None:
    asset = MediaAsset(
        url="https://v.redd.it/abc123/CMAF_720.mp4",
        requires_video_download=True,
    )
    repository = FakeRepository()
    pipeline = build_pipeline(
        make_settings,
        tmp_path,
        candidate=make_candidate((asset,)),
        fetcher=MappingFetcher({}),
        repository=repository,
        storage=FakeStorage(),
        publisher=FakePublisher(),
        video_downloader=FakeVideoDownloader(error=VideoDownloadUnavailableError("missing")),
    )

    result = await pipeline.run("abc123")

    assert result.status is Phase2Status.SKIPPED
    assert result.reason == "video_downloader_unavailable"
    assert repository.created_media == []
    assert repository.sources == []


async def test_existing_object_is_reused_without_duplicate_upload(make_settings, tmp_path) -> None:
    data = png_bytes((2, 3), (1, 2, 3))
    sha256 = hashlib.sha256(data).hexdigest()
    object_key = build_object_key(sha256, "png")
    asset = MediaAsset(url="https://i.redd.it/one.png")
    storage = FakeStorage()
    storage.objects[object_key] = data
    pipeline = build_pipeline(
        make_settings,
        tmp_path,
        candidate=make_candidate((asset,)),
        fetcher=MappingFetcher({asset.url: data}),
        repository=FakeRepository(),
        storage=storage,
        publisher=FakePublisher(),
    )

    result = await pipeline.run("abc123")

    assert result.status is Phase2Status.PUBLISHED
    assert result.object_key == object_key
    assert storage.uploads == []
    assert storage.downloads == [object_key]


async def test_discovery_failure_retains_object_and_is_recoverable(make_settings, tmp_path) -> None:
    data = png_bytes((2, 2), (9, 9, 9))
    asset = MediaAsset(url="https://i.redd.it/one.png")
    repository = FakeRepository(fail_discovery=True)
    storage = FakeStorage()
    publisher = FakePublisher()
    pipeline = build_pipeline(
        make_settings,
        tmp_path,
        candidate=make_candidate((asset,)),
        fetcher=MappingFetcher({asset.url: data}),
        repository=repository,
        storage=storage,
        publisher=publisher,
    )

    result = await pipeline.run("abc123")

    assert result.status is Phase2Status.RECOVERABLE
    assert result.reason == "discovery_write_failed_recoverable"
    assert len(storage.uploads) == 1
    assert repository.created_media == []
    assert publisher.calls == []

    repository.fail_discovery = False
    retried = await pipeline.run("abc123")

    assert retried.status is Phase2Status.PUBLISHED
    assert len(storage.uploads) == 1
    assert len(repository.created_media) == 1
    assert len(publisher.calls) == 1


async def test_source_failure_keeps_media_and_retry_reuses_it(make_settings, tmp_path) -> None:
    data = png_bytes((2, 2), (8, 8, 8))
    asset = MediaAsset(url="https://i.redd.it/one.png")
    repository = FakeRepository(fail_source=True)
    storage = FakeStorage()
    publisher = FakePublisher()
    pipeline = build_pipeline(
        make_settings,
        tmp_path,
        candidate=make_candidate((asset,)),
        fetcher=MappingFetcher({asset.url: data}),
        repository=repository,
        storage=storage,
        publisher=publisher,
    )

    result = await pipeline.run("abc123")

    assert result.status is Phase2Status.RECOVERABLE
    assert result.reason == "source_attribution_failed_recoverable"
    assert len(repository.created_media) == 1
    assert publisher.calls == []

    repository.fail_source = False
    retried = await pipeline.run("abc123")

    assert retried.status is Phase2Status.DUPLICATE
    assert len(storage.uploads) == 1
    assert len(repository.created_media) == 1
    assert len(repository.sources) == 1
    assert publisher.calls == []


async def test_concurrent_duplicate_reuses_media_without_publication(
    make_settings, tmp_path
) -> None:
    data = png_bytes((2, 2), (7, 7, 7))
    asset = MediaAsset(url="https://i.redd.it/one.png")
    repository = FakeRepository(recover_on_create=True)
    storage = FakeStorage()
    publisher = FakePublisher()
    pipeline = build_pipeline(
        make_settings,
        tmp_path,
        candidate=make_candidate((asset,)),
        fetcher=MappingFetcher({asset.url: data}),
        repository=repository,
        storage=storage,
        publisher=publisher,
    )

    result = await pipeline.run("abc123")

    assert result.status is Phase2Status.DUPLICATE
    assert result.reason == "concurrent_duplicate_source_recorded"
    assert result.media_item_id == "concurrent-media"
    assert len(repository.sources) == 1
    assert publisher.calls == []
