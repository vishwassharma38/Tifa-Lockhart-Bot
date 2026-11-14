"""Opt-in integration coverage for the real SQL claim RPC.

Set CRAWL_TEST_SUPABASE_URL and CRAWL_TEST_SUPABASE_SERVICE_ROLE_KEY to a local,
isolated Supabase stack with migrations 001 through 014 applied. The guard below
rejects non-loopback endpoints so this suite cannot write to a hosted project.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from urllib.parse import urlsplit

import pytest
from supabase import create_client


def _local_test_clients():
    url = os.environ.get("CRAWL_TEST_SUPABASE_URL")
    key = os.environ.get("CRAWL_TEST_SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        pytest.skip("local isolated Supabase test credentials are not configured")
    host = (urlsplit(url).hostname or "").lower()
    if host not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("crawl frontier integration tests only allow a loopback Supabase URL")
    return create_client(url, key), create_client(url, key)


def _claim(client, limit: int) -> list[dict[str, object]]:
    response = client.rpc(
        "claim_crawl_discoveries",
        {
            "p_limit": limit,
            "p_max_depth": 6,
            "p_per_domain_limit": 1,
            "p_new_domain_share": 0.8,
        },
    ).execute()
    return response.data or []


@pytest.mark.asyncio
async def test_real_claim_rpc_bounds_batches_and_prevents_concurrent_double_claims() -> None:
    first, second = _local_test_clients()
    token = uuid.uuid4().hex
    domains = [f"{token}-a.invalid", f"{token}-b.invalid"]
    rows = [
        {
            "url": f"https://{domain}/page-{index}",
            "normalized_url": f"https://{domain}/page-{index}",
            "source_type": "seed",
            "domain": domain,
            "hostname": domain,
            "registrable_domain": domain,
            "depth": 0,
            "status": "queued",
            "crawl_priority": 50,
        }
        for domain in domains
        for index in range(2)
    ]
    inserted = first.table("discoveries").insert(rows).execute().data or []
    ids = {str(row["id"]) for row in inserted}
    if len(ids) != len(rows):
        pytest.fail("isolated SQL test setup did not insert every discovery row")
    try:
        batches = await asyncio.gather(
            asyncio.to_thread(_claim, first, 2),
            asyncio.to_thread(_claim, second, 2),
        )
        claimed = [str(row["id"]) for batch in batches for row in batch]
        assert len(batches[0]) <= 2
        assert len(batches[1]) <= 2
        assert len(claimed) == len(set(claimed))
        assert set(claimed) == ids
    finally:
        first.table("discoveries").delete().in_("id", list(ids)).execute()
        first.table("crawl_domain_state").delete().in_("registrable_domain", domains).execute()
