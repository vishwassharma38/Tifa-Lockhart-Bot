# Tifa Lockhart Bot

A personal, automated media-discovery, acquisition, preservation, and delivery system dedicated to Tifa Lockhart.

**Current implementation:** Phase 1 - Foundation
**Runtime:** Python 3.11.8
**Discord integration:** `discord.py` 2.x
**Database:** Supabase PostgreSQL
**Media storage:** Private Backblaze B2 bucket (S3-compatible API)
**Deployment target:** Render Web Service (Phase 7)

> **V1 pipeline:** Discover -> inspect -> verify -> acquire -> deduplicate -> preserve -> record -> deliver.

The complete, authoritative specification for V1 is [Tifa_Lockhart_Bot_V1_Blueprint.md](Tifa_Lockhart_Bot_V1_Blueprint.md). This README documents the Phase 1 implementation and its local setup.

## Phase 1 status

Phase 1 provides the application foundation:

- Typed environment configuration and secret redaction in logs.
- Discord gateway client with `/about` and `/status`.
- Supabase client initialization and checked-in SQL migrations with provider-neutral storage columns.
- Private Backblaze B2 client initialization and a safe upload/download verification utility.
- `GET /healthz` HTTP endpoint bound to `0.0.0.0:$PORT`.
- Single-process startup and graceful shutdown of Discord, HTTP, Supabase, and B2 resources.
- Unit tests that mock all external services by default.

Not implemented yet (later V1 phases):

- Reddit collection, Brave discovery, recursive crawling, classification, and The Dumpster.
- Media acquisition, media validation, SHA-256 deduplication, and B2 media uploads.
- Scheduled jobs, daily digest, and publication tracking.
- `/start`, `/stop`, `/save`, `/random`, and `/stats`.
- Render deployment and acceptance testing.

Only `/about` and `/status` are registered in Phase 1. The other commands are not registered and will not claim to work.

## Architecture

V1 runs as one Python process:

- Discord Gateway client and application commands.
- HTTP health endpoint.
- Shared configuration, Supabase, and Backblaze B2 clients.

The local filesystem is temporary working space only. Durable metadata belongs in Supabase and media belongs in Backblaze B2. Phase 1 does not download or store real media.

Object storage is accessed through the provider-independent `ObjectStorage` interface in `app/storage/base.py`. `B2Storage` is the current implementation; the rest of the application uses the neutral interface and the `create_storage` factory, so a future provider change does not require changing application workflows.

```text
app/
  main.py                 Application startup, orchestration, shutdown
  config.py               Typed environment configuration
  logging_config.py       Secret-redacting logging setup
  status.py               Dependency status aggregation
  bot/
    client.py             discord.py client and command sync
    commands.py           /about and /status
  db/
    client.py             Supabase client initialization and query health check
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
scripts/
  verify_b2.py            Safe temporary upload/download/delete check
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
3. No privileged Gateway intents are required for the Phase 1 slash commands. The client uses the default intents.
4. Invite the bot to your private test server with the `bot` and `applications.commands` scopes. Grant the minimum permissions needed for Phase 1: View Channels, Send Messages, Embed Links, and Attach Files.
5. Copy the server (guild) ID into `DISCORD_GUILD_ID` if you want immediate development-guild command registration. Copy your Discord user ID into `OWNER_DISCORD_ID`.
6. Optionally fill `DISCORD_DISCOVERIES_CHANNEL_ID` and `DISCORD_SAVES_CHANNEL_ID` now. They are validated but are not used until later phases.

Command registration behavior:

- When `DISCORD_GUILD_ID` is set, commands are synced to that development guild once per application startup. Guild command updates normally appear within seconds.
- When `DISCORD_GUILD_ID` is empty, commands are synced globally. Global command propagation can take up to about one hour.
- The tree is synced deliberately once per startup. The sync replaces the current command set rather than adding duplicate definitions. If a sync fails, the application logs a warning and keeps running; restart it after fixing the Discord configuration.

### 6. Configure Supabase and apply the migration

1. Create a Supabase project.
2. Set `SUPABASE_URL` to the project API URL.
3. Set `SUPABASE_SERVICE_ROLE_KEY` to the server-side service-role key. This key bypasses Row Level Security and must never be exposed to browsers, clients, logs, or source control.
4. Apply the checked-in migrations in numeric order. The recommended local workflow is to open the Supabase Dashboard SQL Editor, review each file under `migrations/`, and run them once in order. If `001_initial_schema.sql` has already been applied to your project, do not edit or rerun it; apply `002_provider_neutral_storage_columns.sql` only. If you use your own PostgreSQL migration tooling, apply the files in numeric order and record their versions there.

The migrations create `discoveries`, `discovery_edges`, `media_items`, `media_sources`, `publications`, `job_runs`, `runtime_state`, and `digest_state`. They use UUID keys, `timestamptz`, foreign keys, uniqueness constraints including SHA-256 uniqueness, status checks, indexes, singleton state rows, and Row Level Security with no public policies. `001` creates the initial schema. `002` renames the historical `r2_bucket` and `r2_object_key` columns to `storage_bucket` and `storage_object_key` in place, renames the related uniqueness constraint, preserves existing rows, and does not drop or recreate `media_items`. The migrations do not delete data or store media binaries in PostgreSQL.

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

If only `r2_bucket` and `r2_object_key` are returned, `002` has not been applied yet.

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
- `/status` shows the Discord connection state, version/environment, Supabase state, B2 storage state, and a clear statement that collection is not implemented in Phase 1.

Dependency states are reported truthfully:

- `Not configured` - no credentials were supplied.
- `Configured, not checked` - a client exists but no health check has completed.
- `Connected / verified` - the bounded health query/check succeeded.
- `Unavailable` - the service is configured but the check failed or timed out.

`/status` never prints tokens, service-role keys, signed URLs, channel IDs, or internal exception messages.

### 10. Call `/healthz`

With the default local port from `.env.example`:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:10000/healthz"
```

Expected response:

```json
{"status": "ok"}
```

The endpoint is served on `0.0.0.0:$PORT`, returns HTTP 200 while the process is alive, and deliberately reveals no configuration or dependency details. It does not fail merely because Supabase, B2, Reddit, or Brave is unavailable.

### 11. Run tests and checks

Always use the project's virtual environment:

```powershell
& ".\.venv\Scripts\python.exe" -m pytest -q
& ".\.venv\Scripts\python.exe" -m ruff check .
& ".\.venv\Scripts\python.exe" -m ruff format --check .
& ".\.venv\Scripts\python.exe" -m pip check
```

Tests do not require real Discord, Supabase, Reddit, or B2 credentials. External services are mocked. No destructive integration tests are run against live services.

#### Automated verification

GitHub Actions runs the same checks through `.github/workflows/ci.yml` on every push to `main` and every pull request targeting `main`. The workflow uses Python 3.11.8, installs `requirements-dev.txt`, and runs pytest, Ruff lint checks, Ruff formatting checks, and `pip check`. It needs no Discord, Supabase, Reddit, or B2 secrets; all external-service tests use mocks.

## Configuration reference

The Phase 1 settings are:

| Variable | Purpose | Safe default |
|---|---|---|
| `APP_ENV` | Environment label shown by `/status` | `development` |
| `APP_VERSION` | Version shown by `/about` and `/status` | `1.0.0` |
| `LOG_LEVEL` | Python logging level | `INFO` |
| `TIMEZONE` | IANA timezone used for later scheduling | `Asia/Kolkata` |
| `PORT` | HTTP health port | `10000` |
| `DISCORD_TOKEN` | Discord bot token (required to start) | empty |
| `DISCORD_GUILD_ID` | Development guild for immediate command sync | empty |
| `OWNER_DISCORD_ID` | Owner user ID for later owner-only commands | empty |
| `DISCORD_DISCOVERIES_CHANNEL_ID` | Future digest destination | empty |
| `DISCORD_SAVES_CHANNEL_ID` | Future manual-save destination | empty |
| `SUPABASE_URL` | Supabase project URL | empty |
| `SUPABASE_SERVICE_ROLE_KEY` | Server-only Supabase service-role key | empty |
| `B2_ENDPOINT_URL` | B2 S3-compatible endpoint; must match the bucket region | empty |
| `B2_REGION` | B2 bucket region, such as `us-west-004` | empty |
| `B2_BUCKET_NAME` | Private B2 bucket name | empty |
| `B2_KEY_ID` | B2 application key ID | empty |
| `B2_APPLICATION_KEY` | B2 application key secret | empty |

`REDDIT_ACCESS_APPROVED=false` and `BRAVE_HTML_ENABLED=false` remain in `.env.example` as future-phase safety gates. The other Reddit, Brave, and operating-limit values in the template are reserved for later phases and are not used by Phase 1.

## Security and responsible collection

- Never commit `.env`, tokens, service-role keys, passwords, private keys, or signed URLs.
- Logs redact known configured secrets and common credential patterns. Do not add logging that dumps environment variables, configuration objects, request headers, signed URLs, or full SDK exception payloads.
- Keep the B2 bucket private. Phase 1 creates no public objects and uploads no real media.
- RLS is enabled on every Phase 1 table with no public policies. The service-role key bypasses RLS, so keep it server-side only.
- Respect platform policies, site terms, robots directives, copyright, takedown requests, and rate limits in later phases. Do not bypass authentication, paywalls, DRM, CAPTCHAs, anti-bot controls, or access restrictions.
- A private Discord server does not automatically grant redistribution rights.

## Troubleshooting

- **`DISCORD_TOKEN is required`** - set the bot token in `.env` before running.
- **Partial Supabase/B2 configuration error** - set all fields for that service or leave all of them empty for degraded mode.
- **`/status` shows Unavailable** - check that the Supabase migration was applied, that the B2 bucket exists, that the application key has bucket access, and that the endpoint and region both match the bucket.
- **`B2_ENDPOINT_URL must match B2_REGION`** - copy the endpoint and region from the same bucket's details. The region is the second label of `s3.<region>.backblazeb2.com`. For example, EU Central uses `B2_REGION=eu-central-003` with `B2_ENDPOINT_URL=https://s3.eu-central-003.backblazeb2.com`.
- **`verify_b2` exits nonzero with a cleanup failure** - the temporary object may still exist. Confirm the application key has Delete Files access and add a lifecycle rule for the `healthchecks/` prefix if historical versions should expire. Do not delete the bucket or the `assets/` prefix to work around this error.
- **Slash commands are missing** - confirm the bot was invited with `applications.commands`; allow up to an hour for global sync or set `DISCORD_GUILD_ID` for immediate guild sync.
- **Port already in use** - change `PORT` or stop the other process. The health endpoint binds to `0.0.0.0`.

## Development roadmap

The locked V1 blueprint defines the implementation sequence:

1. Foundation: configuration, logging, Discord client, Supabase, Backblaze B2, health endpoint. **Current.**
2. First Reddit-to-Discord vertical slice.
3. Media reliability, metadata, and provenance.
4. Reddit automation and Discord controls.
5. Brave discovery and controlled crawler, only if permitted.
6. Digest and publication reliability.
7. Render deployment and acceptance testing.

V1 is complete only when the acceptance checklist in Section 19 of the blueprint passes.

## License

No open-source license has been added. Unless a license is added later, all rights are reserved by the project owner.
