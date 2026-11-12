# Domain-aware crawler

The crawler uses the existing `discoveries` queue and media pipeline. Bing and
Reddit links seed the queue; every eligible page can contribute bounded child
links to the same queue. A child keeps its root `source_type`, depth, parent,
and link evidence, so recursive discoveries remain traceable.

## Frontier and fairness

The Public Suffix List library is configured for offline use and includes
private suffixes. It groups URLs by registrable domain, so `a.example.com` and
`b.example.com` share crawl limits, while unrelated tenants on `github.io` stay
separate. The checked-in PSL snapshot changes only when the dependency is
updated.

Migration `013_domain_aware_crawl_frontier.sql` adds `hostname`,
`registrable_domain`, `crawl_priority`, and `claimed_at` to discoveries. It
also adds `crawl_domain_state` and origin-scoped `crawl_origin_state` tables.
Startup backfills legacy discovery identities in batches of 500; the operation
is repeatable and does not delete or rewrite discovery history.

The `claim_crawl_discoveries` RPC selects due rows and marks them `processing`
in one PostgreSQL statement. `FOR UPDATE SKIP LOCKED` prevents concurrent
workers from claiming the same row. Rows are partitioned by registrable domain
and ordered in rounds, so the first eligible row for a domain is considered
before its second row. At most `CRAWL_DOMAIN_BATCH_LIMIT` rows per domain are
claimed in a pass. `CRAWL_NEW_DOMAIN_SHARE` reserves most of the batch for
unexplored domains; previously crawled domains remain eligible in every pass.
`last_claimed_at` rotates domains with comparable priority. A 900-second claim
lease recovers work left in `processing` after a restart.

Per-job HTTP request budgets, concurrency, timeouts, redirect count, page bytes,
depth, URL admissions, and site delay remain bounded. Requests to a registrable
domain share the request budget and throttle even when hostnames differ. A
`429` or explicit `Retry-After` also creates a durable domain cooldown, so other
queued URLs for that site wait too.

## Link discovery

The HTML extractor collects anchors and areas, useful `rel` links, and links in
HTTP `Link` headers. It resolves relative URLs against the first valid HTML
`base` element, removes duplicate URLs, and preserves link text, nearby context,
relation, and admission reasons.

Link scoring favors character, franchise, artist, creator, gallery, and
portfolio signals. A page's own strong character evidence can support further
exploration, while an unrelated page needs a strong destination link itself.
Login, account, search, faceted navigation, calendar archives, and repetitive
path patterns are discarded. Each page admits at most 10 internal and 40
external links, subject to the job-wide URL and depth limits. Direct media URLs
continue through the media pipeline rather than the HTML frontier.

Robots-declared sitemap URLs are preferred; otherwise the crawler checks
`/sitemap.xml`. It reads no more than five sitemap files and 100 page URLs per
registrable domain per scan, caps each compressed and expanded document at
512 KB, rejects XML external entities, and only accepts locations on the
sitemap's host. Sitemap candidates still pass URL safety, robots, scoring,
deduplication, and ordinary queue admission.

## Robots, redirects, and retries

Robots decisions are keyed by scheme, host, and port. Path matching follows
RFC 9309 longest-match semantics, including `*`/`$` patterns, with `Allow`
winning equal-length ties. Parsed rules, crawl delay, sitemap declarations, and
response status are cached in memory per job and in `crawl_origin_state` with a
bounded expiry. Successful and missing policies
expire after 24 hours; other outcomes expire after 10 minutes. `404` and `410`
allow crawling. Access denial, rate limits, server errors, network failures,
timeouts, and unreadable policies fail closed for that origin.

Before every redirect hop, the destination is normalized and resolved through
the public-only URL validator. The crawler then loads that origin's robots
policy and checks the redirected path before requesting the page. Redirects
remain bounded by `MAX_REDIRECTS`.

Transient transport and server errors receive bounded exponential backoff and
jitter. `Retry-After` values and domain cooldowns are stored as future queue due
times; the crawler does not sleep for long server-requested delays inside a
worker. Retries stop at `CRAWL_MAX_ATTEMPTS`. Access restrictions, challenges,
robots denials, unsupported content, and oversize pages are recorded with
distinct outcomes.

## State and operations

`discovery_edges` stores parent and child IDs, source and destination domains,
depth, link text, original link URL, admission score, and whether the edge came
from an HTML link or sitemap. `crawl_domain_state` keeps first and last crawl
times, request outcomes, HTTP status counts, inspection-latency average, health,
relevance, cooldown, and sitemap scan time. The `/status` command reports
frontier totals; each crawler run records visited domains, the distinct
external domains that received newly created queue rows, internal and external
admissions, skipped URLs, and whether the URL budget was exhausted.

Apply migrations 013 and 014 before deploying code that calls their RPCs. A
local PostgreSQL server is not part of this repository's test setup. The
loopback-only integration test exercises the claim RPC when a local Supabase
stack is available; without it, production RPC execution and query-plan
performance remain unverified.
