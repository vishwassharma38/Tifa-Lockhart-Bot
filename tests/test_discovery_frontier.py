from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta


class SimulatedPersistentFrontier:
    """Small in-memory model of the SQL round order for large queue fixtures."""

    def __init__(self, rows: list[dict[str, object]], known_domains: set[str]) -> None:
        self.rows = rows
        self.claim_times: dict[str, int] = {}
        self.known_domains = known_domains

    def claim(self, *, limit: int, per_domain_limit: int = 2, new_domain_share: float = 0.8):
        grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
        for row in self.rows:
            if row["status"] == "queued":
                grouped[str(row["registrable_domain"])].append(row)
        for domain_rows in grouped.values():
            domain_rows.sort(
                key=lambda item: (
                    -int(item.get("crawl_priority") or 0),
                    str(item["discovered_at"]),
                    str(item["id"]),
                )
            )

        cold_domains = sorted(
            (domain for domain in grouped if domain not in self.known_domains),
            key=lambda domain: (self.claim_times.get(domain, -1), domain),
        )
        cold_rank = {domain: index + 1 for index, domain in enumerate(cold_domains)}
        quota = max(1, int(limit * new_domain_share))
        candidates: list[tuple[tuple[object, ...], dict[str, object]]] = []
        for domain, domain_rows in grouped.items():
            if domain in cold_rank and cold_rank[domain] <= quota:
                group = 0
            elif domain in self.known_domains:
                group = 1
            else:
                group = 2
            for domain_round, row in enumerate(domain_rows[:per_domain_limit], start=1):
                key = (
                    domain_round,
                    group,
                    cold_rank.get(domain, 10**9),
                    self.claim_times.get(domain, -1),
                    -int(row.get("crawl_priority") or 0),
                    str(row["discovered_at"]),
                    str(row["id"]),
                )
                candidates.append((key, row))

        selected = [row for _key, row in sorted(candidates, key=lambda pair: pair[0])[:limit]]
        timestamp = max(self.claim_times.values(), default=0) + 1
        for row in selected:
            row["status"] = "processing"
            self.claim_times[str(row["registrable_domain"])] = timestamp
        return selected


def _row(domain: str, index: int, *, host: str | None = None) -> dict[str, object]:
    now = datetime(2026, 11, 10, tzinfo=UTC) - timedelta(days=365)
    return {
        "id": f"{domain}-{index}",
        "hostname": host or domain,
        "registrable_domain": domain,
        "status": "queued",
        "crawl_priority": 0,
        "discovered_at": now.isoformat(),
    }


def test_first_batch_explores_all_twenty_smaller_domains_before_repeats() -> None:
    rows = [_row("deviantart.example", i) for i in range(10_000)]
    rows.extend(_row("zerochan.example", i) for i in range(10_000))
    small_domains = {f"creator{i}.example" for i in range(20)}
    for domain in small_domains:
        rows.extend(_row(domain, i) for i in range(5))

    frontier = SimulatedPersistentFrontier(
        rows,
        known_domains={"deviantart.example", "zerochan.example"},
    )
    selected = frontier.claim(limit=25, per_domain_limit=2, new_domain_share=0.8)

    selected_domains = [str(row["registrable_domain"]) for row in selected]
    assert small_domains <= set(selected_domains)
    assert len(set(selected_domains)) >= 20
    assert selected_domains.count("deviantart.example") <= 2
    assert selected_domains.count("zerochan.example") <= 2


def test_later_batches_keep_large_old_domains_eligible() -> None:
    rows = [_row("large-a.example", i) for i in range(100)]
    rows.extend(_row("large-b.example", i) for i in range(100))
    rows.extend(_row(f"small{i}.example", 0) for i in range(8))
    frontier = SimulatedPersistentFrontier(
        rows,
        known_domains={"large-a.example", "large-b.example"},
    )

    selected_domains: list[str] = []
    for _round in range(4):
        selected = frontier.claim(limit=6, per_domain_limit=2, new_domain_share=0.8)
        selected_domains.extend(str(row["registrable_domain"]) for row in selected)
        for row in selected:
            row["status"] = "queued"

    assert "large-a.example" in selected_domains
    assert "large-b.example" in selected_domains
    assert len(set(selected_domains)) > 2


def test_subdomains_share_the_same_registrable_domain_budget() -> None:
    rows = [
        _row("example.com", 0, host="www.example.com"),
        _row("example.com", 1, host="artist.example.com"),
        _row("example.com", 2, host="gallery.example.com"),
        _row("another.example", 0),
    ]
    frontier = SimulatedPersistentFrontier(rows, known_domains=set())

    selected = frontier.claim(limit=4, per_domain_limit=2, new_domain_share=0.8)

    assert sum(row["registrable_domain"] == "example.com" for row in selected) == 2
    assert any(row["registrable_domain"] == "another.example" for row in selected)
