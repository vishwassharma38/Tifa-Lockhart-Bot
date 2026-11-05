from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from app.reddit.collector import RedditCollector
from app.reddit.models import MediaAsset, RedditCandidate


class Repo:
    def __init__(self):
        self.state = {"collection_enabled": True}
        self.jobs = []
        self.finished = []
        self.discoveries = []

    async def get_runtime_state(self):
        return self.state

    async def create_job_run(self, config):
        self.jobs.append(config)
        return "job"

    async def finish_job_run(self, *args):
        self.finished.append(args)

    async def mark_reddit_success(self):
        pass

    async def upsert_discovery(self, candidate):
        self.discoveries.append(candidate)


class Connector:
    def __init__(self, values):
        self.values, self.calls = values, []

    async def search(self, phrase, *, limit):
        self.calls.append((phrase, limit))
        return self.values.get(phrase, [])


class Pipeline:
    async def process_candidate(self, candidate, *, publish):
        assert publish is False
        return []


def settings(make_settings, **kwargs):
    return make_settings(
        REDDIT_CLIENT_ID="id",
        REDDIT_CLIENT_SECRET="secret",
        REDDIT_USER_AGENT="agent",
        REDDIT_ACCESS_APPROVED="true",
        **kwargs,
    )


async def test_global_unique_cap_and_overlapping_searches(make_settings):
    first = [
        SimpleNamespace(
            id=f"id{i}",
            permalink=f"/r/x/comments/id{i}/title",
            title="x",
            selftext="",
            created_utc=1,
            subreddit=SimpleNamespace(display_name="x"),
            author=None,
            url="https://example.com/nope.png",
        )
        for i in range(100)
    ]
    connector = Connector({"one": first, "two": [first[0]]})
    repo = Repo()
    collector = RedditCollector(
        settings(make_settings, REDDIT_SEARCH_PHRASES="one|two|three"), repo, Pipeline(), connector
    )
    result = await collector.run()
    assert result["examined_count"] == 100
    assert result["discovered_count"] == 100
    assert len(repo.discoveries) == 100
    assert connector.calls == [("one", 100)]


async def test_gate_blocks_all_reddit_search(make_settings):
    connector = Connector({})
    repo = Repo()
    collector = RedditCollector(
        make_settings(
            REDDIT_CLIENT_ID="id", REDDIT_CLIENT_SECRET="secret", REDDIT_USER_AGENT="agent"
        ),
        repo,
        Pipeline(),
        connector,
    )
    await collector.run()
    assert connector.calls == []
    assert repo.finished[0][2] == "skipped"
    assert "approval" in repo.finished[0][3]


async def test_unattended_collection_skips_yt_dlp_video_path(make_settings):
    candidate = RedditCandidate(
        submission_id="video1",
        permalink="https://www.reddit.com/r/x/comments/video1/title",
        source_url="https://www.reddit.com/r/x/comments/video1/title",
        title="video",
        body=None,
        subreddit="x",
        author_name=None,
        author_profile_url=None,
        created_at=datetime.now(UTC),
        media_url=None,
        media_assets=(
            MediaAsset(url="https://v.redd.it/video1/file.mp4", requires_video_download=True),
        ),
    )
    repo = Repo()
    connector = Connector({"Tifa Lockhart": [candidate]})
    pipeline = Pipeline()
    collector = RedditCollector(settings(make_settings), repo, pipeline, connector)
    counters = await collector.run()
    assert counters["processed_count"] == 1
    assert counters["skipped_count"] == 1
    assert repo.discoveries[0].submission_id == "video1"
