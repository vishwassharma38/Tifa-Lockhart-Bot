# Tifa Lockhart Bot

A personal, automated media-discovery, acquisition, preservation, and delivery system dedicated to Tifa Lockhart.

**Current implementation:** Phase 6 - Daily digest and publication reliability
**Runtime:** Python 3.11.8
**Discord integration:** `discord.py` 2.x
**Database:** Supabase PostgreSQL
**Media storage:** Private Backblaze B2 bucket (S3-compatible API)
**Deployment target:** Render Web Service (Phase 7)

> **V1 pipeline:** Discover -> inspect -> verify -> acquire -> deduplicate -> preserve -> record -> deliver.

The complete V1 specification is [Tifa_Lockhart_Bot_V1_Blueprint.md](Tifa_Lockhart_Bot_V1_Blueprint.md). This README documents the current implementation through Phase 6.

## Implementation status

Phase 1 provides the application foundation:

- Typed environment configuration and secret redaction in logs.
- Discord gateway client with `/about` and `/status`.
- Supabase client initialization and checked-in SQL migrations with provider-neutral storage columns.
- Private Backblaze B2 client initialization and a safe upload/download verification utility.
- `GET /healthz` HTTP endpoint bound to `0.0.0.0:$PORT`.
- Single-process startup and graceful shutdown of Discord, HTTP, Supabase, and B2 resources.
- Unit tests that mock all external services by default.

Phase 2 and Phase 3 provide the shared Reddit-to-archive media pipeline:

- PRAW-backed submission lookup behind the explicit `REDDIT_ACCESS_APPROVED` gate.
- Normalized Reddit candidates with truthful title, body, author, profile, and source attribution.
- Direct images, supported galleries, and Reddit-hosted video acquisition with streaming downloads, actual signature checks, extension checks, and a hard 10,000,000-byte ceiling.
- SHA-256 deduplication before upload and content-addressed private B2 keys (`assets/<first-two-hex>/<sha256>.<ext>`).
- Supabase discovery, media, source, and publication records through a repository layer.
- Actual Discord file attachment publication, with failure recording and retained archived media.
- A development-only `scripts/phase2_run.py` entry point for one submission at a time.

Phase 4 adds scheduled discovery and controls:

- Broad PRAW searches through `r/all` for the five configured phrases, sorted newest first, with a global configurable cap of 100 unique submissions per hourly run.
- Automated collection archives through the shared media pipeline without immediately publishing to Discord. `/save` publishes immediately.
- Hourly scheduling in `Asia/Kolkata`, persistent pause/start state, one in-process overlapping-run guard, run counters in Supabase, and interrupted-run recovery.
- `/start`, `/stop`, `/save`, `/random`, `/stats`, `/about`, and `/status` slash commands.
- `REDDIT_ENABLED` and `REDDIT_ACCESS_APPROVED` both gate automated Reddit access. Approval defaults to false and must reflect actual authorization; valid credentials do not grant approval.
- Media URL resolution blocks non-public destinations, including redirect destinations. Existing B2 objects are downloaded and checked against expected size and SHA-256 before reuse.
- Scheduled collection skips standalone Reddit-hosted video because yt-dlp performs segmented requests outside the public-only media resolver. Manual `/save` retains the existing Phase 3 video path.

Phase 5 adds Brave HTML discovery, safe crawling, classification, and The Dumpster:

- A modular Brave Search HTML adapter that is disabled unless both `BRAVE_HTML_ENABLED` and `BRAVE_HTML_ACCESS_APPROVED` are true. V1 does not use the Brave Search API or an API key.
- URL normalization and a persistent Supabase discovery queue shared by Brave results and discovered child links. `discovery_edges` records the path from a search result or manual save to each descendant.
- A bounded, SSRF-safe crawler with public-only DNS resolution, pinned-IP connections, manual redirect revalidation, `robots.txt` handling, global concurrency limits, per-domain throttling, page-size/depth/URL caps, and bounded retries.
- Deterministic text and metadata relevance classification. Search-query text alone cannot verify a page. Ambiguous, preview-only, or extraction-limited pages are staged in The Dumpster instead of being acquired or published.
- Verified crawler acquisitions are archived through the existing media pipeline but are not published automatically. `/save` with a public URL uses the same safe fetch/extraction path and publishes immediately.
- Scheduler integration for Brave discovery and crawl processing, persistent Brave restriction pause state, interrupted-job recovery, and expanded `/status` reporting.

Phase 6 adds the daily digest and restart-safe publication delivery:

- A daily digest job at 04:00 `Asia/Kolkata` on the existing scheduler. It is independent of `/start` and `/stop`; pausing collection never disables digest delivery.
- Strict digest routing through `DISCORD_CATEGORY_CHANNELS_JSON`, keyed by validated `media_type` (`image`, `animation`, `video`, `dumpster`). All four channel IDs are required when Supabase and B2 are configured; there is no digest fallback channel.
- Digest selection is limited to `verification_status='verified'` and `download_status='stored'` items with a valid archived B2 reference and a recorded size within the 10,000,000-byte ceiling.
- Each item is downloaded from private B2, checked against its stored SHA-256, and published as a real Discord file attachment with truthful title, category, creator, and source attribution.
- Durable `publications` rows with purpose `daily_digest`, bounded three-attempt retries, deterministic publication markers, and Discord message-history reconciliation for ambiguous deliveries.
- A persistent, database-serialized budget of 50 unique media items per 04:00 IST cycle, shared across destinations, with oldest-first backlog carry-forward and global per-item digest deduplication.
- Compact Markdown source attribution with all distinct associated source links; automatic embeds are suppressed while real file attachments remain enabled.
- A persisted digest window, startup catch-up after downtime, incomplete-window resume, and first-install backlog processing.
- Expanded `/status` reporting for schedule, last success, latest outcome and error, publication backlog/retry/exhausted counts, and incomplete-window state.

Not implemented yet (later V1 phases):

- Render deployment and full V1 acceptance testing (Phase 7).

`/start` and `/stop` require the configured `OWNER_DISCORD_ID`. They persist desired state; the Discord client and health endpoint remain online while automated collection, Brave discovery, and crawling are paused. `/status` reports effective collection gating and the separate Brave feature-gate, approval, and persistent-pause state. `/start` never clears a Brave access-restriction pause. The daily digest is not tied to collection state: it continues to deliver already archived, verified media while collection is paused, and `/start` does not trigger a catch-up by itself. `scripts/phase2_run.py` remains available for controlled one-submission testing.

## Architecture

V1 runs as one Python process:

- Discord Gateway client and application commands.
- HTTP health endpoint.
- Shared configuration, Supabase, and Backblaze B2 clients.

The local filesystem is temporary working space only. Durable metadata belongs in Supabase and media belongs in Backblaze B2. Media acquisition downloads one asset to a temporary directory and removes that directory after the run.

Object storage is accessed through the provider-independent `ObjectStorage` interface in `app/storage/base.py`. `B2Storage` is the current implementation; the rest of the application uses the neutral interface and the `create_storage` factory, so a future provider change does not require changing application workflows.

Phase 5 adds one in-process discovery subsystem. Brave Search, the crawler, and manual public-URL saves share the same URL normalization, public-only DNS validation, `SafeHttpClient`, robots cache, retry budget, and domain throttle. The crawler uses the existing `discoveries`, `discovery_edges`, and `job_runs` tables and does not introduce a second queue, storage provider, scheduler, or process. Verified web acquisitions pass through the existing Phase 2 pipeline and are archived under `assets/`; scheduled crawler acquisitions are not published automatically.

Phase 6 adds one in-process digest service on the existing `AsyncIOScheduler`. It reads verified, stored archive items from Supabase, retrieves their media from private B2 through the existing storage interface, and publishes them through the existing publisher as real Discord file attachments. It reuses `publications`, `digest_state`, `runtime_state`, and `job_runs`; it does not add a second scheduler, worker, database client, queue, or archive. Scheduled and startup-catch-up runs share the same service, an in-process `asyncio.Lock`, and the `daily_digest` job-run record.

```text
app/
  main.py                 Application startup, orchestration, shutdown
  config.py               Typed environment configuration
  logging_config.py       Secret-redacting logging setup
  status.py               Dependency status aggregation
  bot/
    client.py             discord.py client, scheduler, recovery, background-task shutdown
    commands.py           Slash commands, /save routing, /status reporting
    publisher.py          Actual Discord file attachment publication
  db/
    client.py             Supabase client initialization and query health check
    repository.py         Discovery queue, edges, media, state, jobs, and counts
  discovery/
    __init__.py           Phase 5 package marker
    url_safety.py         URL normalization, public DNS validation, pinned resolver
    challenges.py         Brave/page challenge and access-denial detection
    extractor.py          HTML metadata, link, and original-media extraction
    models.py             WebCandidate adapter for the Phase 2 pipeline
    classifier.py         Deterministic relevance classification and Dumpster status
    http.py               SSRF-safe HTTP transport, budgets, throttling, redirects, retries
    robots.py             robots.txt parsing and per-job policy
    brave.py              Brave HTML parser, adapter, gates, and persistent pause
    crawler.py            Page inspector, bounded queue crawler, manual web saves
  jobs/
    __init__.py           Phase 6 jobs package marker
    digest.py             Daily digest, window persistence, retries, catch-up
  reddit/
    connector.py          Gated PRAW lookup, broad search, and normalization
    collector.py          Bounded scheduled collection and job-run accounting
    models.py             Typed Reddit candidate and URL normalization
  media/
    validation.py         Signature checks, size ceiling, object keys
    fetcher.py            Streaming bounded media download
  pipeline/
    phase2.py             One-submission orchestration and result statuses
  storage/
    base.py               Provider-independent storage interface and errors
    b2.py                 Private Backblaze B2 S3-compatible adapter
  health/
    server.py             /healthz aiohttp server
    status.py             Shared dependency status models
migrations/
  001_initial_schema.sql  Initial Phase 1 schema
  002_provider_neutral_storage_columns.sql
                          Renames historical storage columns without data loss
  003_harden_updated_at_function_search_path.sql
                          Pins the updated_at trigger function search path
  004_phase2_test_discovery_publication.sql
                          Adds the test_discovery publication purpose
  005_phase3_per_asset_source_provenance.sql
                          Adds per-asset gallery source attribution
  006_phase4_reddit_collection.sql
                          Adds persistent collection counters and state timestamp
  007_phase5_discovery_controls.sql
                          Adds queue scheduling indexes and Brave pause state
  008_global_digest_budget.sql
                          Adds atomic global digest reservation and cycle accounting
scripts/
  verify_b2.py            Safe temporary upload/download/delete check
  phase2_run.py           Controlled one-submission Phase 2 run
tests/                    Unit tests with external services mocked
```

## Requirements

- Python 3.11.8 at `D:\Python\Python311`
- Git
- A Discord application, bot token, and a private test server
- A Supabase project (PostgreSQL)
- A Backblaze B2 bucket and a bucket-scoped S3-compatible application key
- Credentials and IDs supplied by the project owner; none are committed to the repository

The project is pinned to Python 3.11 for this implementation. Do not use Python 3.12-only syntax or dependencies here.

## Local development setup

All commands below are Windows PowerShell commands. Use the project's `.venv` interpreter for every install, run, and test.

### 1. Verify Python 3.11.8

```powershell
cd "D:\My Programs\Tifa Lockhart Bot"

& "D:\Python\Python311\python.exe" --version
```

The output must be `Python 3.11.8`. If that executable is missing, install Python 3.11.8 from the official Python website at the documented path, then rerun the command. Do not use a newer Python interpreter for this project.

### 2. Create and activate the virtual environment

If `.venv` does not exist:

```powershell
& "D:\Python\Python311\python.exe" -m venv .venv
```

If `.venv` already exists, inspect it first and reuse it when it is based on Python 3.11.8. Do not delete or recreate a working environment unnecessarily:

```powershell
& ".\.venv\Scripts\python.exe" --version
```

Activate it for the current PowerShell session:

```powershell
.\.venv\Scripts\Activate.ps1
python --version
```

If PowerShell blocks activation, call `.\.venv\Scripts\python.exe` directly for the remaining commands.

### 3. Install dependencies

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
```

`requirements-dev.txt` includes the runtime dependencies plus pytest, pytest-asyncio, and Ruff. For a runtime-only install, use `python -m pip install -r requirements.txt`.

### 4. Create the local environment file

```powershell
Copy-Item .env.example .env
```

Edit `.env` with your own values. `.env` is ignored by Git and must never be committed or shared. `.env.example` contains placeholders and safe defaults only.

### 5. Create and configure the Discord bot

1. Create an application in the Discord Developer Portal, then add a bot.
2. Copy the bot token into `DISCORD_TOKEN` in `.env`.
3. No privileged Gateway intents are required for the slash commands. The client uses the default intents.
4. Invite the bot to your private test server with the `bot` and `applications.commands` scopes. Grant the minimum permissions needed: View Channels, Send Messages, Embed Links, Attach Files, and Read Message History. Read Message History is required for digest destination channels so an interrupted delivery can be reconciled against channel history instead of being blindly reposted.
5. Copy the server (guild) ID into `DISCORD_GUILD_ID` if you want immediate development-guild command registration. Copy your Discord user ID into `OWNER_DISCORD_ID`.
6. Fill `DISCORD_DISCOVERIES_CHANNEL_ID` and `DISCORD_SAVES_CHANNEL_ID` with channels where the bot can attach files. `/save` publishes to the saves channel (falling back to discoveries).

Command registration behavior:

- When `DISCORD_GUILD_ID` is set, commands are synced to that development guild once per application startup. Guild command updates normally appear within seconds.
- When `DISCORD_GUILD_ID` is empty, commands are synced globally. Global command propagation can take up to about one hour.
- The tree is synced deliberately once per startup. The sync replaces the current command set rather than adding duplicate definitions. If a sync fails, the application logs a warning and keeps running; restart it after fixing the Discord configuration.

### 6. Configure Supabase and apply the migration

1. Create a Supabase project.
2. Set `SUPABASE_URL` to the project API URL.
3. Set `SUPABASE_SERVICE_ROLE_KEY` to the server-side service-role key. This key bypasses Row Level Security and must never be exposed to browsers, clients, logs, or source control.
4. Apply the checked-in migrations in numeric order. The recommended local workflow is to open the Supabase Dashboard SQL Editor, review each file under `migrations/`, and run them once in order. If the project is already at migration 007, apply `008_global_digest_budget.sql` in the Supabase SQL Editor before deploying this version. It adds cycle and reservation columns, preserves historical publication rows, backfills one canonical digest record per media item, and creates the database-side atomic reservation function and indexes. It does not delete publication or media history.

The migrations create `discoveries`, `discovery_edges`, `media_items`, `media_sources`, `publications`, `job_runs`, `runtime_state`, and `digest_state`. Migration 006 extends the existing `job_runs` and `runtime_state` tables in place. Migration 007 extends `discoveries` and `runtime_state` in place. The migrations do not delete data or store media binaries in PostgreSQL.

Verify the tables after applying:

```sql
select table_name
from information_schema.tables
where table_schema = 'public'
  and table_name in (
    'discoveries', 'discovery_edges', 'media_items', 'media_sources',
    'publications', 'job_runs', 'runtime_state', 'digest_state'
  )
order by table_name;
```

The repository does not apply migrations to a live project automatically, and no live migration should be considered verified until you run it and check the result yourself. After applying `002`, confirm the provider-neutral columns exist:

```sql
select column_name
from information_schema.columns
where table_schema = 'public'
  and table_name = 'media_items'
  and column_name in ('storage_bucket', 'storage_object_key')
order by column_name;
```

If only `r2_bucket` and `r2_object_key` are returned, `002` has not been applied yet. After applying `003`, confirm the trigger function uses the hardened search path:

```sql
select p.proconfig
from pg_proc p
join pg_namespace n on n.oid = p.pronamespace
where n.nspname = 'public'
  and p.proname = 'set_tifa_updated_at';
```

The result should include `search_path=` (an empty search path). If it does not, `003` has not been applied yet. After applying `004`, confirm the publication-purpose constraint includes `test_discovery`:

```sql
select pg_get_constraintdef(oid)
from pg_constraint
where conrelid = 'public.publications'::regclass
  and conname = 'publications_purpose_check';
```

After applying `007`, confirm the Phase 5 queue and Brave pause columns exist:

```sql
select table_name, column_name
from information_schema.columns
where table_schema = 'public'
  and (
    (table_name = 'discoveries' and column_name = 'next_attempt_at')
    or (table_name = 'runtime_state' and column_name in (
      'brave_paused', 'brave_pause_reason', 'brave_paused_at'
    ))
  )
order by table_name, column_name;
```

### 7. Create the private Backblaze B2 bucket and credentials

1. Create a Backblaze account, enable B2 Cloud Storage, and review Backblaze's current free allowance and billing terms. This repository does not create an account or configure storage for you.
2. Create a B2 bucket and keep it private. Do not enable public bucket access, a public custom domain, or public object URLs. A Backblaze B2 account is tied to one region, and the bucket's S3 endpoint reflects that region.
3. In the bucket details, note the bucket region and S3 endpoint. The endpoint format is `https://s3.<region>.backblazeb2.com`. For example, a bucket in `us-west-004` uses `https://s3.us-west-004.backblazeb2.com`, and an EU Central bucket in `eu-central-003` uses `https://s3.eu-central-003.backblazeb2.com`. Do not use an endpoint from another region, and do not use `auto` as the region.
4. Create an application key scoped to this bucket. Do not use the master application key. Grant the minimum capabilities needed by Phase 1: List All Bucket Names (required for SDK compatibility with bucket-restricted keys), List Files, Read Files, Write Files, and Delete Files. The verification utility needs write, read, and delete access to its temporary object; `/status` checks bucket access with a `HeadBucket` request.
5. Record the Application Key ID as `B2_KEY_ID` and the Application Key secret as `B2_APPLICATION_KEY`. Backblaze shows the application key only when it is created.
6. Configure these values in `.env`:
   - `B2_ENDPOINT_URL` - the bucket's S3 endpoint, such as `https://s3.us-west-004.backblazeb2.com`
   - `B2_REGION` - the region from the endpoint and bucket details, such as `us-west-004`
   - `B2_BUCKET_NAME` - the private bucket name
   - `B2_KEY_ID` - the application key ID
   - `B2_APPLICATION_KEY` - the application key secret
7. Verify the credentials and bucket with the safe temporary-object check:

```powershell
python -m scripts.verify_b2
```

This uploads a uniquely named tiny text object under `healthchecks/`, downloads and compares the bytes, then attempts to delete only that generated object. It never uploads real media, never creates a public URL, and never performs bucket-wide deletion. The command exits `0` only when the upload, download, byte comparison, and requested cleanup all succeed. If cleanup fails, it prints an error and exits nonzero; upload/download success is not reported as a complete verification when the temporary object could not be deleted.

The `/status` health check also performs a bucket-level `HeadBucket` request, so the application key must permit bucket access; if `/status` reports B2 unavailable while `verify_b2` succeeds, check the key's List Files/List Bucket Names capabilities.

#### B2 object versioning and verification cleanup

Backblaze B2 retains object versions. Deleting an object through the S3-compatible `DeleteObject` operation may create a hide marker or delete marker instead of permanently removing every historical version. A hidden object is no longer returned by normal reads, but its earlier versions may still occupy storage until they are explicitly deleted or expired by a lifecycle rule. Permanent version deletion is a separate operation; the verification utility does not attempt it.

The verification utility only requests deletion of the exact uniquely named `healthchecks/<uuid>.txt` object created by the current run. It never lists and bulk-deletes the bucket, never deletes objects under `assets/`, and never purges all versions in the bucket. If you want historical verification versions to expire automatically, add a Backblaze lifecycle rule scoped to the `healthchecks/` prefix that hides or deletes current objects after a short interval (for example, one day) and expires non-current versions after a short interval. Do not apply an expiration rule to the `assets/` prefix or to the bucket as a whole, because archived media must be retained.

Common B2 configuration errors:

- `B2_ENDPOINT_URL must match B2_REGION` - the endpoint and region belong to different buckets or regions. Copy both from the same bucket's details.
- `403` / `AccessDenied` - the application key is missing a required capability or is scoped to a different bucket.
- `InvalidAccessKeyId` or `SignatureDoesNotMatch` - `B2_KEY_ID` and `B2_APPLICATION_KEY` are swapped, truncated, or from different keys.
- `NoSuchBucket` or `404` - the bucket name is wrong or the endpoint points at another region.
- Connection errors or timeouts - check the network, the endpoint URL, and whether the bucket's region changed. Storage operations have bounded timeouts and do not block the asyncio event loop.

If Supabase or B2 settings are completely absent, the application starts in a documented degraded mode and `/status` reports the service as not configured. If only part of a service's settings is present, startup fails with a clear configuration error rather than pretending the service is available. Missing B2 credentials never prevent the process from starting.

The `media_items` table uses the provider-neutral columns `storage_bucket` and `storage_object_key`. Migration `002` renames the historical `r2_bucket` and `r2_object_key` columns in place without changing media identity. Those columns store the private B2 bucket name and object key, and the SHA-256 content-addressing and deduplication rules are unchanged.

### 8. Run the application locally

```powershell
python -m app.main
```

The application requires `DISCORD_TOKEN` to connect to Discord. It starts the health endpoint first, initializes configured Supabase and B2 clients, then starts the Discord client. Startup failures clean up resources that were already started.

The process stays online while Discord is connected. Stop it with `Ctrl+C`; the Discord client, HTTP runner, Supabase client, and B2 client are closed during shutdown.

### 9. Test `/about` and `/status`

In the configured Discord server:

- `/about` shows the bot name, purpose, version from `APP_VERSION`, and developer `SoftwareFromVish`.
- `/status` shows Discord, Supabase, B2, collection gates and persisted state, latest Reddit, Brave, crawler, and daily-digest job information, and digest publication counts.

Dependency states are reported truthfully:

- `Not configured` - no credentials were supplied.
- `Configured, not checked` - a client exists but no health check has completed.
- `Connected / verified` - the bounded health query/check succeeded.
- `Unavailable` - the service is configured but the check failed or timed out.

`/status` never prints tokens, service-role keys, signed URLs, channel IDs, or internal exception messages.

### 10. Run one controlled Phase 2 acquisition

Phase 2 is deliberately a one-submission development run, not an automated collection loop. It requires:

- `REDDIT_ACCESS_APPROVED=true`.
- Authorized `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, and `REDDIT_USER_AGENT` values.
- `DISCORD_DISCOVERIES_CHANNEL_ID` set to the private destination channel.
- Supabase configured, with migrations `001` through `007` applied.
- Private B2 configured.

Run one submission by ID or permalink:

```powershell
& ".\.venv\Scripts\python.exe" -m scripts.phase2_run --submission abc123
```

The command starts the Discord client, waits for readiness, fetches exactly one Reddit submission, downloads one direct image or video asset, validates the actual bytes and size, computes SHA-256, deduplicates, uploads to the private B2 `assets/` prefix, writes Supabase metadata and attribution, sends a real Discord file attachment, and exits. It prints the result as JSON. It exits `0` for `published` or `duplicate`, and nonzero for skipped, failed, or recoverable results.

Supported Reddit galleries and Reddit-hosted video use the same processing path. Preview hosts, Reddit page URLs, and unsupported extensions are rejected with explicit reasons. If Discord delivery fails after the B2 upload, the archived object is retained and the publication is recorded as failed.

### 11. Enable Phase 4 collection and use commands

Apply `migrations/006_phase4_reddit_collection.sql` manually in Supabase after migration 005, and apply migration 007 as described in the next section for Phase 5. Set the Reddit OAuth values and truthful user agent in `.env`. Keep `REDDIT_ACCESS_APPROVED=false` until authorized API access is confirmed; set it to `true` only after that confirmation. `REDDIT_ENABLED` is an additional feature gate and defaults to `true`. Configure `REDDIT_MAX_ITEMS_PER_RUN` (default `100`) and, optionally, `REDDIT_SEARCH_PHRASES` as pipe-separated phrases. Do not copy credentials into `.env.example`.

Start the application with `python -m app.main`. The scheduler checks the persisted collection state each hour at minute 0 in `Asia/Kolkata`. In Discord, `/start` and `/stop` are restricted to `OWNER_DISCORD_ID` and persist the desired state across restarts. `/status` displays that state and the effective access gates. `/save <url>` immediately archives and publishes supported Reddit media, `/random` delivers an actual verified stored attachment, `/stats week|month|lifetime` separates unique archived assets from successful deliveries, and `/about` describes the bot. Pausing collection does not disable manual save or random delivery.

### 12. Enable Phase 5 Brave discovery and crawling (optional)

Phase 5 is additive and inactive by default. The crawler requires Supabase; automated Brave discovery and crawl processing additionally require the persisted collection state to be enabled. Manual `/save` of a public URL remains available while automated work is paused.

#### Confirm Brave access and set both gates

V1 uses public Brave Search HTML result pages, not the Brave Search API. No Brave API key is used or required. Before setting the approval flag, the owner must independently confirm that programmatic access to the intended Brave HTML search interface is permitted by Brave's current terms, `robots.txt`, and applicable law. Parsing public HTML is not itself permission to automate access.

Two independent settings gate the adapter:

| Setting | Purpose | Safe default |
|---|---|---|
| `BRAVE_HTML_ENABLED` | Feature activation for the Brave adapter | `false` |
| `BRAVE_HTML_ACCESS_APPROVED` | Explicit owner acknowledgement that HTML access has been reviewed and approved | `false` |

Both must be `true` before any live Brave request is made. `BRAVE_HTML_ACCESS_APPROVED` is never set automatically, and valid credentials or a parseable page do not imply approval. If either gate is false, the scheduled Brave job records a skipped run and `/status` shows why. The Reddit gates and the Brave gates are independent.

#### Configure phrases and schedule

- `BRAVE_SEARCH_PHRASES` is a pipe-separated list. It defaults to the same five Tifa-related phrases used by Reddit collection but is configured independently. Each phrase must be 200 characters or fewer.
- `BRAVE_SEARCH_INTERVAL_HOURS` controls the Brave interval job and accepts `1` through `168`; the default is `8`.
- `BRAVE_SEARCH_MAX_PAGES_PER_PHRASE` controls how many HTML result pages are requested per phrase and accepts `1` through `10`; the default is `2`.
- The Brave interval job and the five-minute crawl-processing job run in the configured `TIMEZONE` (`Asia/Kolkata` by default). A scheduled Brave run also performs one crawl pass afterward.
- `/stop` pauses scheduled Reddit collection, Brave discovery, and crawl processing. `/start` resumes them but never clears a persistent Brave restriction pause.

#### How Brave HTML discovery works

For each configured phrase, the adapter requests up to the configured number of result pages, isolates the result list with parser selectors, and emits only public `http`/`https` result links. Selectors are confined to `app/discovery/brave.py` and are tested with local HTML fixtures; Brave markup can change, so a parser failure is reported as a failed job rather than treated as proof of relevance. The adapter does not use a browser, solve challenges, rotate proxies or IP addresses, or retry around access controls.

Each accepted result is normalized, deduplicated by normalized URL, and inserted into the persistent `discoveries` queue with its search phrase, result title, snippet, and originating URL. If Brave returns a CAPTCHA/anti-automation challenge, HTTP `401`, `403`, or `429`, or an explicit access-denial page, the run stops immediately, further Brave requests are not attempted, and the adapter is persistently paused until the owner reviews the restriction. The reason and timestamp are stored in `runtime_state`; a restart or `/start` does not clear them.

#### Persistent queue and discovery path

Brave results, crawler-discovered child links, and manual public-URL saves share the existing `discoveries` table. There is no second queue or scheduler. A queue row moves through statuses such as `queued`, `processing`, `verified`, `dumpster`, `rejected`, `blocked`, `failed`, and `oversize`.

- `next_attempt_at` provides durable retry scheduling. HTTP `429` honors `Retry-After` when present and otherwise schedules a later attempt; interrupted or paused work is returned to `queued`.
- Each crawl pass claims at most `CRAWL_BATCH_SIZE` (default `25`) due rows and atomically moves them to `processing`. A single in-process lock prevents overlapping crawl runs.
- On startup, job rows left in `running` are marked interrupted and `processing` discoveries are returned to `queued`, so a restart does not strand work.
- `discovery_edges` records parent/child relationships, the discovered-from URL, and depth. Descendants keep the originating `source_type`; crawler-link evidence is retained in metadata and the edge record.
- Only `verified` and `dumpster` pages propagate child links. `rejected`, `blocked`, `failed`, and `oversize` pages do not.
- A crawl job admits at most `MAX_URLS_PER_JOB` URLs, including child admissions, and does not process rows deeper than `MAX_CRAWL_DEPTH`.

Verified pages with original media are archived through the existing Phase 2 media pipeline with publication disabled. They become normal archived assets but are not posted to Discord automatically. A verified page that exposes only previews, or no original media, is moved to The Dumpster with a review reason. Direct media links discovered as children are skipped by the crawler; acquisition uses the media pipeline and its safe media fetcher.

#### Crawler safety limits

The defaults are hard maximums enforced by configuration validation; malformed values fail startup instead of silently weakening safety.

| Limit | Default and maximum | Setting |
|---|---|---|
| URLs admitted per job | `300` | `MAX_URLS_PER_JOB` |
| Pages per domain per job | `20` | `MAX_PAGES_PER_DOMAIN` |
| Globally concurrent requests | `5` | `MAX_CONCURRENT_REQUESTS` |
| Minimum delay per domain | `2` seconds (configurable up to `60`) | `PER_DOMAIN_DELAY_SECONDS` |
| Page body ceiling | `5,000,000` bytes | `MAX_PAGE_BYTES` |
| Maximum crawl depth | `6` | `MAX_CRAWL_DEPTH` |
| Maximum redirects | `5` | `MAX_REDIRECTS` |
| Connect timeout | `10` seconds | `CRAWL_CONNECT_TIMEOUT_SECONDS` |
| Read timeout | `20` seconds | `CRAWL_READ_TIMEOUT_SECONDS` |
| Transient attempts | `3` | `CRAWL_MAX_ATTEMPTS` |
| Rows claimed per pass | `25` | `CRAWL_BATCH_SIZE` |

The concurrency limit is global to the crawler instance, not per domain. No two requests to the same domain begin less than the configured delay apart, and a parsed `robots.txt` `Crawl-delay` can increase that spacing. Waits are coordinated through shared throttling rather than scattered fixed sleeps.

#### URL normalization and SSRF protection

The same rules apply to Brave results, crawler links, redirects, direct media URLs, and public `/save` inputs:

- Only absolute `http` and `https` URLs without embedded credentials or fragments are accepted. Control characters, backslashes, malformed ports, overlong URLs, localhost names, and private/reserved destinations are rejected.
- Every hostname is resolved through the public-only resolver, and every returned address is checked against private, loopback, link-local, multicast, reserved, and otherwise non-public ranges. If any address is non-public, the URL is rejected.
- Discovery and page connections are pinned to a validated public address through the shared `SafeHttpClient`, which prevents DNS rebinding between validation and connection. The media pipeline's fetcher applies the same public-only resolver checks to direct media downloads.
- Redirects are followed manually, one hop at a time. Each destination is normalized, resolved, and revalidated before the next request; the redirect count is bounded and the final URL is rechecked.
- Response bodies are streamed into a hard byte ceiling, content types are checked, and compressed output expansion is bounded.

These checks are transport-level protections, not a substitute for site terms or robots policy.

#### robots.txt policy

The crawler fetches and caches `robots.txt` once per origin for each crawl job. A `2xx` response is parsed and its rules and `Crawl-delay` are honored. `404` and `410` are treated as no restrictions, following the unavailable policy in RFC 9309. A `401`, `403`, `429`, `5xx`, network failure, timeout, or unreadable policy marks that origin unavailable for the rest of the job; this is deliberately stricter than the RFC so an unreadable policy cannot be used to ignore a restriction. A disallowed URL is recorded as `blocked` with `robots_disallowed` and is not retried during that job.

#### HTTP status, redirect, and retry behavior

- `2xx` HTML within the page-size and content-type limits is extracted.
- `401`/`403` mark the row `blocked`; the domain is paused for the rest of the job.
- `429` pauses the domain, records `retry_not_before`, and returns the row to `queued` for a durable retry instead of hammering the site.
- Other permanent `4xx` responses, unsupported content types, invalid redirects, and too many redirects are recorded as rejected or failed without endless retries.
- `5xx`, network errors, and timeouts are retried up to three times with bounded exponential backoff and jitter (maximum 30 seconds per delay). Exhausted attempts mark the row `failed`.
- A detected challenge page marks the row `blocked`; for Brave it also triggers the persistent adapter pause described above.

#### Classification outcomes and The Dumpster

Classification is deterministic and uses page text and metadata only. It does not inspect pixels or call a hosted model. Search title, snippet, and phrase are kept separate from page evidence; search metadata contributes at most one point and can never verify a page by itself.

| Outcome | Meaning |
|---|---|
| `verified` | Strong Tifa evidence in the page's own metadata/text and original media extracted. The page is archived through the media pipeline without automatic publication. |
| `dumpster` | Ambiguous or insufficient page evidence, missing page metadata, preview-only media, no original media, or extraction failure. The row is staged for review and is not automatically acquired or published. |
| `rejected` | No Tifa evidence in the page or its metadata. Child links are not followed. |
| `blocked` | Robots, access control, or a challenge prevented inspection. |
| `oversize` | The page exceeded the configured byte ceiling. |
| `failed` | Fetch or processing failed after bounded attempts. |

A Brave search hit that merely contains the search phrase is therefore not enough: a page with only search-result evidence lands in `dumpster`, not `verified`.

#### Reviewing The Dumpster

The Dumpster is a Supabase staging status, not a separate archive, Discord channel, or dashboard. Phase 5 adds no review slash command. Review rows directly in Supabase, for example:

```sql
select id, url, title, last_error,
       metadata_json -> 'classification' as classification,
       metadata_json ->> 'review_reason' as review_reason,
       reviewed_at, reviewed_by, review_note,
       discovered_at
from public.discoveries
where status = 'dumpster'
order by discovered_at desc;
```

Record review decisions in the existing `reviewed_at`, `reviewed_by`, and `review_note` columns. Dumpster items never enter automatic acquisition or verified-media delivery. After reviewing one, either mark it `rejected` if it is not relevant, or return it to `queued` with `next_attempt_at = null` so the existing safe crawler re-examines it through the same URL, robots, extraction, classification, and acquisition path. Do not bypass the crawler by inserting media or publication rows manually. A reviewed `verified` item still archives without automatic Discord publication; use the existing manual delivery paths when appropriate.

#### Diagnosing and resuming paused or failed Brave discovery

`/status` reports the Brave feature flag, approval state, persistent pause reason, current block reason, latest Brave and crawler job outcomes, last successful Brave discovery, queue/backlog count, and Dumpster count. It never claims Brave is running merely because it is configured.

Common block or pause causes:

- `BRAVE_HTML_ENABLED is false` or `Brave HTML access approval has not been confirmed` - set both gates only after permission review.
- `anti_automation_challenge`, `explicit_access_denial_page`, `http_401_access_denied`, `http_403_access_denied`, or `http_429_rate_limited` - Brave or an intermediary restricted access. The adapter is paused; do not try to defeat the control with CAPTCHA solving, browser automation, proxy rotation, or IP rotation.
- Parser or job failure without a pause - inspect the latest `brave_discovery` row in `job_runs` and the application log. Fix the parser/configuration, then let the next interval run attempt again.
- Queued work not progressing - confirm collection is enabled with `/start`, Supabase is reachable, migration 007 is applied, and no crawl run is already active.

A persistent Brave pause is cleared only by a deliberate operator action after reviewing the restriction and confirming that access is still permitted. There is no `/resume-brave` command. The repository provides `clear_brave_pause()` for an operator script; the equivalent direct database operation is:

```sql
update public.runtime_state
set brave_paused = false,
    brave_pause_reason = null,
    brave_paused_at = null
where id = 1;
```

Do not clear the pause merely to keep a scheduled job running. If the restriction remains in effect, leave the adapter paused and disable `BRAVE_HTML_ENABLED` instead.

### 13. Configure and operate the Phase 6 daily digest

Phase 6 is active by default when Supabase, B2, Discord, and the scheduler are available. Migration `008_global_digest_budget.sql` and the four-channel mapping are required.

#### Schedule and collection separation

- The digest runs daily at `04:00 Asia/Kolkata` on the existing `AsyncIOScheduler` job `daily_digest`, with `coalesce=True`, `max_instances=1`, and a one-hour misfire grace period.
- It is a delivery operation, not a discovery operation. It is not tied to `collection_enabled`, so `/stop` does not disable it and `/start` does not trigger it. Manual `/save`, `/random`, `/stats`, `/about`, and `/status` remain available throughout.
- An in-process `asyncio.Lock` prevents scheduled and startup runs from overlapping. PostgreSQL serializes reservations through the existing `digest_state` singleton, enforces the global 50-item cycle budget, and deduplicates digest publications by canonical media item across channels.

#### Category routing

`DISCORD_CATEGORY_CHANNELS_JSON` accepts a JSON object mapping each validated media type to its positive Discord channel ID:

```dotenv
DISCORD_CATEGORY_CHANNELS_JSON={"image":123456789012345678,"animation":123456789012345679,"video":123456789012345680,"dumpster":123456789012345681}
```

The required keys are `image`, `animation`, `video`, and `dumpster`. Legacy category keys can still be parsed for compatibility but do not affect digest routing. Malformed JSON, non-object JSON, unsupported keys, non-integer values, booleans, and zero or negative IDs fail configuration loading. When Supabase and B2 are configured, omitting any required media-type destination fails startup.

For each item, the digest routes only by `media_items.media_type`: `image` to Pictures, `animation` to GIFs, and `video` to Videos. The validator currently supports no miscellaneous binary format, so no item is routed to Dumpster; unknown types remain out of publication. There is no fallback to another channel. `/save` and `/random` retain their existing destinations.

#### Candidate selection and delivery

Only items with both `verification_status='verified'` and `download_status='stored'`, a valid 64-character SHA-256, a non-empty object key, and a recorded size from 1 through `10,000,000` bytes are eligible. Dumpster, unverified, rejected, failed, and skipped items are never selected.

The digest persists a window before processing it, scans in stable `created_at, id` order using the persisted lower boundary and the fixed upper boundary, and paginates with an offset so more rows than one PostgREST response page are handled. A persisted cursor makes interruption resume safe; because publication records are the per-item source of truth, re-reading the boundary timestamp cannot duplicate a delivered item.

For each item the digest downloads the private object through the existing storage interface, enforces the 10,000,000-byte ceiling, recomputes SHA-256 with a constant-time comparison, and only then publishes. The publisher sends the actual file as a Discord attachment with the title/caption, category, creator name and profile link when recorded, original source URL, source platform and native post ID when available, and a truthful note when creator attribution is missing. Web discoveries are not given fabricated Reddit metadata. One attachment is sent per message, so one failure cannot block unrelated items and multiple messages are expected.

#### Publication records, retries, and reconciliation

Each canonical media item has at most one new `publications` row with `purpose='daily_digest'`, independent of channel. The atomic reservation function assigns one persistent 04:00 IST cycle and reserves one global slot before delivery. Successful and ambiguous deliveries retain their reservation; definitive failures release it only when no message could have been delivered. Delivery attempts remain bounded at three. The digest stops selecting new items at 50 reserved unique media, leaves the window incomplete, and resumes the oldest remaining items in the next cycle.

The message includes the deterministic marker `tifa-digest-publication:<publication UUID>` in its content and embed footer. If a prior attempt is ambiguous (`status='pending'` with `attempt_count > 0`), the digest first scans the destination channel's history for that marker. If found, it records the existing Discord message ID and marks the row published without resending. The scan is bounded to 500 messages; a missing marker is only treated as proof of absence when the scan did not hit that bound. If history is unavailable, the lookup fails, or the scan is inconclusive, the digest fails closed: it records a reconciliation error, leaves the publication unresolved, logs the problem, and does not blindly repost. This is why digest destination channels require **Read Message History**.

#### Window completion, startup catch-up, and first install

`digest_state` stores the stable cycle date, window boundaries, completion time, and media cursor. The database reservation function locks the singleton state row before counting/reserving slots, so concurrent processes cannot overbook. The allowance is never reset by a process restart. A capped window remains incomplete and carries forward; its inclusive timestamp cursor and deterministic `(created_at, id)` ordering safely revisit ties.

On startup, after the Discord client is ready, the same digest service checks whether a window is incomplete or whether the last completion predates the most recent 04:00 IST occurrence. If so, it runs one catch-up. This covers downtime longer than the scheduler's misfire grace period. A fresh installation with no digest state uses an initial window from `1970-01-01T00:00:00Z` to the current time, so the existing verified backlog is processed rather than silently ignored. Catch-up is not run merely because `/start` was invoked.

`runtime_state.last_digest_success_at` advances only for a fully successful digest (`succeeded`). A partial run (for example, an item with a recorded per-item failure, a held window, or a failed success-timestamp update) keeps the latest `job_runs` outcome and error summary visible without claiming a completely successful delivery run.

#### `/status`

`/status` reports the 04:00 IST schedule, the last successful completion, the latest `daily_digest` job outcome and bounded error summary, counts of pending and retryable/exhausted digest publications, and whether a window is incomplete or awaiting recovery. It distinguishes "scheduled" from "actually completed" and never claims digest health merely because the job is registered.

### 14. Call `/healthz`

With the default local port from `.env.example`:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:10000/healthz"
```

Expected response:

```json
{"status": "ok", "scope": "liveness"}
```

The endpoint is served on `0.0.0.0:$PORT`, returns HTTP 200 while the process and event loop are alive, and deliberately reveals no configuration or dependency details. It reports liveness only, not full system health; dependency state is available through the Discord `/status` command. The endpoint does not fail merely because Supabase, B2, Reddit, or Brave is unavailable.

### 15. Run tests and checks

Always use the project's virtual environment:

```powershell
& ".\.venv\Scripts\python.exe" -m pytest -q
& ".\.venv\Scripts\python.exe" -m ruff check .
& ".\.venv\Scripts\python.exe" -m ruff format --check .
& ".\.venv\Scripts\python.exe" -m pip check
```

Tests do not require real Discord, Supabase, Reddit, Brave, B2, or third-party website access. External services and HTTP responses are mocked or served from local fixtures. No live Brave Search or crawler requests and no destructive integration tests are run against live services.

#### Automated verification

GitHub Actions runs the same checks through `.github/workflows/ci.yml` on every push to `main` and every pull request targeting `main`. The workflow uses Python 3.11.8, installs `requirements-dev.txt`, and runs pytest, Ruff lint checks, Ruff formatting checks, and `pip check`. It needs no Discord, Supabase, Reddit, or B2 secrets; all external-service tests use mocks.

## Configuration reference

The implemented settings are:

| Variable | Purpose | Safe default |
|---|---|---|
| `APP_ENV` | Environment label shown by `/status` | `development` |
| `APP_VERSION` | Version shown by `/about` and `/status` | `1.0.0` |
| `LOG_LEVEL` | Python logging level | `INFO` |
| `TIMEZONE` | Validated IANA timezone setting; the current scheduler runs jobs in `Asia/Kolkata` | `Asia/Kolkata` |
| `PORT` | HTTP health port | `10000` |
| `DISCORD_TOKEN` | Discord bot token (required to start) | empty |
| `DISCORD_GUILD_ID` | Development guild for immediate command sync | empty |
| `OWNER_DISCORD_ID` | Owner user ID for later owner-only commands | empty |
| `DISCORD_DISCOVERIES_CHANNEL_ID` | `#tifa-discoveries` destination for discovery notices | empty |
| `DISCORD_SAVES_CHANNEL_ID` | Manual `/save` destination (`#saves`), with existing fallback | empty |
| `DISCORD_CATEGORY_CHANNELS_JSON` | Required JSON map for image, animation, video, and dumpster digest channels when digest services are configured | `{}` |
| `SUPABASE_URL` | Supabase project URL | empty |
| `SUPABASE_SERVICE_ROLE_KEY` | Server-only Supabase service-role key | empty |
| `B2_ENDPOINT_URL` | B2 S3-compatible endpoint; must match the bucket region | empty |
| `B2_REGION` | B2 bucket region, such as `us-west-004` | empty |
| `B2_BUCKET_NAME` | Private B2 bucket name | empty |
| `B2_KEY_ID` | B2 application key ID | empty |
| `B2_APPLICATION_KEY` | B2 application key secret | empty |
| `REDDIT_ACCESS_APPROVED` | Explicit gate for authorized Reddit API access | `false` |
| `REDDIT_ENABLED` | Feature gate for Reddit API use | `true` |
| `REDDIT_MAX_ITEMS_PER_RUN` | Global unique candidate ceiling across all phrases | `100` |
| `REDDIT_CLIENT_ID` | Authorized Reddit OAuth client ID | empty |
| `REDDIT_CLIENT_SECRET` | Authorized Reddit OAuth client secret | empty |
| `REDDIT_USER_AGENT` | Truthful Reddit API user agent | empty |
| `BRAVE_HTML_ENABLED` | Feature gate for Brave HTML discovery | `false` |
| `BRAVE_HTML_ACCESS_APPROVED` | Explicit owner approval for Brave HTML access | `false` |
| `BRAVE_SEARCH_BASE_URL` | Brave Search HTML base URL; must be HTTPS without credentials or fragment | `https://search.brave.com/search` |
| `BRAVE_SEARCH_INTERVAL_HOURS` | Brave discovery interval in hours (`1`-`168`) | `8` |
| `BRAVE_SEARCH_PHRASES` | Pipe-separated Brave search phrases | five Tifa phrases |
| `BRAVE_SEARCH_MAX_PAGES_PER_PHRASE` | Brave result pages per phrase (`1`-`10`) | `2` |
| `MAX_PAGE_BYTES` | HTML page body ceiling (`1`-`5,000,000`) | `5000000` |
| `MAX_CRAWL_DEPTH` | Maximum crawl depth (`1`-`6`) | `6` |
| `MAX_URLS_PER_JOB` | URLs admitted per crawl job (`1`-`300`) | `300` |
| `MAX_PAGES_PER_DOMAIN` | Pages per domain per job (`1`-`20`) | `20` |
| `MAX_CONCURRENT_REQUESTS` | Global discovery HTTP concurrency (`1`-`5`) | `5` |
| `PER_DOMAIN_DELAY_SECONDS` | Minimum delay between same-domain requests (`2`-`60`) | `2` |
| `MAX_REDIRECTS` | Redirects per request (`1`-`5`) | `5` |
| `CRAWL_CONNECT_TIMEOUT_SECONDS` | Connection timeout in seconds (maximum `10`) | `10` |
| `CRAWL_READ_TIMEOUT_SECONDS` | Read timeout in seconds (maximum `20`) | `20` |
| `CRAWL_MAX_ATTEMPTS` | Transient request attempts (`1`-`3`) | `3` |
| `CRAWL_BATCH_SIZE` | Due queue rows claimed per crawl pass | `25` |

`REDDIT_SEARCH_PHRASES` is a pipe-separated optional setting; defaults are `Tifa Lockhart`, `Tifa Lockhart fanart`, `Tifa cosplay`, `Tifa Lockhart cosplay`, and `Tifa Final Fantasy VII`. `/start` persists desired state, while collection also requires both Reddit gates and credentials. Applying migration 006 is required for Phase 4 runtime controls and run counters. Automated collection stores assets without publishing; `/save` publishes immediately and `/random` delivers one verified archived file.

`BRAVE_SEARCH_PHRASES` uses the same default phrases but is independent of the Reddit list. Both Brave gates are required in addition to enabled collection and Supabase availability. The crawler settings are enforced as hard upper bounds: configuration that exceeds them fails startup rather than silently relaxing SSRF, concurrency, depth, redirect, or size limits. Applying migration 007 is required for durable queue retry scheduling and Brave pause state. Automated crawler acquisitions are archived without immediate Discord publication; `/save` with a public URL applies the same safe fetch and publishes immediately.

The Phase 6 digest needs all four `DISCORD_CATEGORY_CHANNELS_JSON` destinations and migration 008. The bounded retry limit is three total attempts per canonical media item. The digest job remains scheduled at 04:00 `Asia/Kolkata` and does not derive its schedule from `TIMEZONE`.

## Security and responsible collection

- Never commit `.env`, tokens, service-role keys, passwords, private keys, or signed URLs.
- Logs redact known configured secrets and common credential patterns. Do not add logging that dumps environment variables, configuration objects, request headers, signed URLs, or full SDK exception payloads.
- Keep the B2 bucket private. Media is uploaded only to the private `assets/` prefix, and the application never creates public object URLs.
- RLS is enabled on every table with no public policies. The service-role key bypasses RLS, so keep it server-side only.
- Phase 5 validates every URL and every resolved DNS address before connecting, pins connections to validated public addresses through `PublicResolver`, revalidates every redirect hop, and applies the same protections to Brave, crawler, direct media, and `/save` public URLs.
- Phase 6 digest delivery downloads only from the private B2 bucket, verifies the stored SHA-256 before publishing, and never sends a public object URL or preview embed in place of the real file. It records bounded error categories rather than raw SDK payloads.
- Brave HTML access is disabled unless both explicit gates are true. Approval is an owner decision about permitted access, not a technical permission granted by credentials.
- A Brave challenge, `401`, `403`, `429`, or explicit access-denial page persistently pauses the adapter. Do not bypass it with CAPTCHA solving, browser automation, authenticated crawling, proxy rotation, or IP rotation. Review the restriction and clear the pause only after confirming access is permitted.
- The crawler honors `robots.txt`, treats an unreadable policy as a domain-wide restriction for the job, enforces global concurrency and same-domain delays, and applies page-size, depth, URL, redirect, and retry bounds.
- Search-query evidence alone cannot verify a page. Weak or ambiguous candidates go to The Dumpster and never enter automatic verified-media delivery or automatic publication.
- Respect platform policies, site terms, copyright, takedown requests, and rate limits. Do not bypass authentication, paywalls, DRM, CAPTCHAs, anti-bot controls, or access restrictions. V1 does not use browser automation, login-based crawling, CAPTCHA solving, or a Brave Search API key.
- A private Discord server does not automatically grant redistribution rights.

## Troubleshooting

- **`DISCORD_TOKEN is required`** - set the bot token in `.env` before running.
- **Partial Supabase/B2 configuration error** - set all fields for that service or leave all of them empty for degraded mode.
- **`/status` shows Unavailable** - check that the Supabase migration was applied, that the B2 bucket exists, that the application key has bucket access, and that the endpoint and region both match the bucket.
- **`B2_ENDPOINT_URL must match B2_REGION`** - copy the endpoint and region from the same bucket's details. The region is the second label of `s3.<region>.backblazeb2.com`. For example, EU Central uses `B2_REGION=eu-central-003` with `B2_ENDPOINT_URL=https://s3.eu-central-003.backblazeb2.com`.
- **`verify_b2` exits nonzero with a cleanup failure** - the temporary object may still exist. Confirm the application key has Delete Files access and add a lifecycle rule for the `healthchecks/` prefix if historical versions should expire. Do not delete the bucket or the `assets/` prefix to work around this error.
- **Slash commands are missing** - confirm the bot was invited with `applications.commands`; allow up to an hour for global sync or set `DISCORD_GUILD_ID` for immediate guild sync.
- **Phase 2 run reports `reddit_access_not_approved`** - set `REDDIT_ACCESS_APPROVED=true` only after you have authorized Reddit API access and reviewed the platform terms.
- **Phase 2 run reports `reddit_credentials_missing`** - set all three Reddit credential values; the connector intentionally fails before constructing PRAW when any are absent.
- **Phase 2 run reports `destination_not_configured`** - set `DISCORD_DISCOVERIES_CHANNEL_ID` and confirm the bot can view and send in that private channel.
- **Phase 2 run reports `publication_record_failed_recoverable`** - apply migration `004`; the `test_discovery` publication purpose must be allowed by the database constraint.
- **Brave job is skipped or `/status` says access is not approved** - set `BRAVE_HTML_ENABLED=true` and `BRAVE_HTML_ACCESS_APPROVED=true` only after reviewing the current Brave terms, `robots.txt`, and applicable law. The scheduled job also requires collection to be enabled with `/start`.
- **Brave is persistently paused** - `/status` shows the stored reason. Read it before doing anything else. If it is a challenge or access restriction, leave the adapter paused; do not attempt to defeat the control. Resume only after an owner review confirms access is permitted, using the operator process in the Phase 5 section.
- **Queue rows are not progressing** - confirm `/start` is active, Supabase is configured and reachable, migration `007` is applied, and a crawl run is not already holding the in-process lock. Rows with a future `next_attempt_at` are intentionally waiting for a retry.
- **Crawler row is `blocked` with `robots_disallowed`** - the site's `robots.txt` does not permit that URL for the crawler identity. Leave it blocked; do not override robots policy.
- **Crawler row is `failed` with `http_429_rate_limited`** - the row is requeued with `retry_not_before`. Reduce search scope or wait; the crawler already honors `Retry-After` and pauses the domain.
- **`/save` returns `invalid_url:<reason>`** - the public URL failed normalization, DNS validation, or the public-address check. `blocked_destination`, `dns_error`, and `credentials_in_url` are deliberate rejections; do not try to bypass them.
- **`/save` returns `page_extraction_failed`, `preview_only_original_unavailable`, or `original_media_not_found`** - the page did not expose a supported original media file. The discovery may be staged in The Dumpster for review rather than published.
- **Phase 5 database errors mention missing columns** - apply `migrations/007_phase5_discovery_controls.sql`. The queue retry schedule and Brave pause state depend on it.
- **Digest reports `destination_not_configured`** - configure the required `image`, `animation`, `video`, and `dumpster` IDs in `DISCORD_CATEGORY_CHANNELS_JSON`. The digest does not fall back to another channel.
- **Digest reports `delivery_unreconciled` or `reconciliation_failed`** - an earlier attempt may have reached Discord, but the bot could not inspect channel history. Grant Read Message History in the digest destination, confirm the channel is still accessible, then let a later recovery run reconcile it. Do not delete the pending publication row to force a resend.
- **Digest reports `integrity_verification_failed` or `invalid_downloaded_size`** - the B2 object does not match the recorded SHA-256 or size bound. The item is not published; inspect the archived object before retrying or repairing the record.
- **Digest publications are exhausted** - each item and destination has three total attempts. Review the stored `last_error`; after correcting the cause, an operator may reset the specific publication row only with a deliberate, reviewed database change. Do not raise retries globally or delete history to hide repeated failures.
- **Digest job is registered but nothing was delivered** - `/status` distinguishes schedule from outcome. Check the latest `daily_digest` job run, the persisted window, publication counts, and whether collection state is irrelevant (the digest does not depend on `/start`).
- **`DISCORD_CATEGORY_CHANNELS_JSON must ...` on startup** - fix the JSON object or channel IDs. Digest keys are `image`, `animation`, `video`, and `dumpster`; each ID must be a positive integer.
- **Port already in use** - change `PORT` or stop the other process. The health endpoint binds to `0.0.0.0`.

## Development roadmap

The locked V1 blueprint defines the implementation sequence:

1. Foundation: configuration, logging, Discord client, Supabase, Backblaze B2, health endpoint. **Complete.**
2. First Reddit-to-Discord vertical slice. **Complete.**
3. Media reliability, metadata, and provenance. **Complete.**
4. Reddit automation and Discord controls. **Complete.**
5. Brave discovery and controlled crawler, only if permitted. **Complete.**
6. Daily digest and publication reliability. **Complete.**
7. Render deployment and acceptance testing. **Not started.**

Phase 6 implements the daily digest, category-specific Discord publication, and restart-safe publication reconciliation. Phase 7 deployment and the full V1 acceptance checklist in Section 19 of the blueprint remain outstanding; V1 is complete only when that checklist passes.

## License

No open-source license has been added. Unless a license is added later, all rights are reserved by the project owner.
