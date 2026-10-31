# Tifa Lockhart Bot

A personal, automated media-discovery, acquisition, preservation, and delivery system dedicated to Tifa Lockhart.

**Current implementation:** Phase 1 - Foundation
**Runtime:** Python 3.11.8
**Discord integration:** `discord.py` 2.x
**Database:** Supabase PostgreSQL
**Media storage:** Private Cloudflare R2 bucket
**Deployment target:** Render Web Service (Phase 7)

> **V1 pipeline:** Discover -> inspect -> verify -> acquire -> deduplicate -> preserve -> record -> deliver.

The complete, authoritative specification for V1 is [Tifa_Lockhart_Bot_V1_Blueprint.md](Tifa_Lockhart_Bot_V1_Blueprint.md). This README documents the Phase 1 implementation and its local setup.

## Phase 1 status

Phase 1 provides the application foundation:

- Typed environment configuration and secret redaction in logs.
- Discord gateway client with `/about` and `/status`.
- Supabase client initialization and checked-in initial SQL migration.
- Private Cloudflare R2 client initialization and a safe upload/download verification utility.
- `GET /healthz` HTTP endpoint bound to `0.0.0.0:$PORT`.
- Single-process startup and graceful shutdown of Discord, HTTP, Supabase, and R2 resources.
- Unit tests that mock all external services by default.

Not implemented yet (later V1 phases):

- Reddit collection, Brave discovery, recursive crawling, classification, and The Dumpster.
- Media acquisition, media validation, SHA-256 deduplication, and R2 media uploads.
- Scheduled jobs, daily digest, and publication tracking.
- `/start`, `/stop`, `/save`, `/random`, and `/stats`.
- Render deployment and acceptance testing.

Only `/about` and `/status` are registered in Phase 1. The other commands are not registered and will not claim to work.

## Architecture

V1 runs as one Python process:

- Discord Gateway client and application commands.
- HTTP health endpoint.
- Shared configuration, Supabase, and R2 clients.

The local filesystem is temporary working space only. Durable metadata belongs in Supabase and media belongs in R2. Phase 1 does not download or store real media.

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
    r2.py                 Private R2 S3-compatible adapter
  health/
    server.py             /healthz aiohttp server
    status.py             Shared dependency status models
migrations/
  001_initial_schema.sql  Initial Phase 1 schema
scripts/
  verify_r2.py            Safe temporary upload/download/delete check
tests/                    Unit tests with external services mocked
```

## Requirements

- Python 3.11.8 at `D:\Python\Python311`
- Git
- A Discord application, bot token, and a private test server
- A Supabase project (PostgreSQL)
- A Cloudflare R2 bucket and S3-compatible credentials
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
4. Apply the checked-in migration in order. The recommended local workflow is to open the Supabase Dashboard SQL Editor, review `migrations/001_initial_schema.sql`, and run the entire file once. If you use your own PostgreSQL migration tooling, apply the files under `migrations/` in numeric order and record their versions there.

The migration creates `discoveries`, `discovery_edges`, `media_items`, `media_sources`, `publications`, `job_runs`, `runtime_state`, and `digest_state`. It uses UUID keys, `timestamptz`, foreign keys, uniqueness constraints including SHA-256 uniqueness, status checks, indexes, singleton state rows, and Row Level Security with no public policies. It does not delete data or store media binaries in PostgreSQL.

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

The repository does not apply migrations to a live project automatically, and no live migration should be considered verified until you run it and check the result yourself.

### 7. Create the private R2 bucket and credentials

1. Create a Cloudflare R2 bucket. Keep it private; do not enable public access, public development URLs, or a public custom domain.
2. Create an R2 API token scoped to the bucket with Object Read & Write access.
3. Set:
   - `R2_ENDPOINT_URL` to `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`
   - `R2_ACCESS_KEY_ID`
   - `R2_SECRET_ACCESS_KEY`
   - `R2_BUCKET_NAME`
   - `R2_REGION=auto`
4. Verify the credentials and bucket with the safe temporary-object check:

```powershell
python -m scripts.verify_r2
```

This uploads a tiny text object under `healthchecks/`, downloads and compares it, then deletes only that generated object. It never uploads real media and never creates a public URL. The `/status` health check also performs a bucket-level `HeadBucket` request, so the R2 token must permit bucket read/list access; if `/status` reports R2 unavailable while `verify_r2` succeeds, check the token's bucket permissions.

If Supabase or R2 settings are completely absent, the application starts in a documented degraded mode and `/status` reports the service as not configured. If only part of a service's settings is present, startup fails with a clear configuration error rather than pretending the service is available.

### 8. Run the application locally

```powershell
python -m app.main
```

The application requires `DISCORD_TOKEN` to connect to Discord. It starts the health endpoint first, initializes configured Supabase and R2 clients, then starts the Discord client. Startup failures clean up resources that were already started.

The process stays online while Discord is connected. Stop it with `Ctrl+C`; the Discord client, HTTP runner, Supabase client, and R2 client are closed during shutdown.

### 9. Test `/about` and `/status`

In the configured Discord server:

- `/about` shows the bot name, purpose, version from `APP_VERSION`, and developer `SoftwareFromVish`.
- `/status` shows the Discord connection state, version/environment, Supabase state, R2 state, and a clear statement that collection is not implemented in Phase 1.

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

The endpoint is served on `0.0.0.0:$PORT`, returns HTTP 200 while the process is alive, and deliberately reveals no configuration or dependency details. It does not fail merely because Supabase, R2, Reddit, or Brave is unavailable.

### 11. Run tests and checks

Always use the project's virtual environment:

```powershell
& ".\.venv\Scripts\python.exe" -m pytest -q
& ".\.venv\Scripts\python.exe" -m ruff check .
& ".\.venv\Scripts\python.exe" -m ruff format --check .
```

Tests do not require real Discord, Supabase, or R2 credentials. External services are mocked. No destructive integration tests are run against live services.

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
| `R2_ENDPOINT_URL` | R2 S3-compatible endpoint | empty |
| `R2_ACCESS_KEY_ID` | R2 access key ID | empty |
| `R2_SECRET_ACCESS_KEY` | R2 secret access key | empty |
| `R2_BUCKET_NAME` | Private bucket name | empty |
| `R2_REGION` | R2 signing region | `auto` |

`REDDIT_ACCESS_APPROVED=false` and `BRAVE_HTML_ENABLED=false` remain in `.env.example` as future-phase safety gates. The other Reddit, Brave, and operating-limit values in the template are reserved for later phases and are not used by Phase 1.

## Security and responsible collection

- Never commit `.env`, tokens, service-role keys, passwords, private keys, or signed URLs.
- Logs redact known configured secrets and common credential patterns. Do not add logging that dumps environment variables, configuration objects, request headers, signed URLs, or full SDK exception payloads.
- Keep the R2 bucket private. Phase 1 creates no public objects and uploads no real media.
- RLS is enabled on every Phase 1 table with no public policies. The service-role key bypasses RLS, so keep it server-side only.
- Respect platform policies, site terms, robots directives, copyright, takedown requests, and rate limits in later phases. Do not bypass authentication, paywalls, DRM, CAPTCHAs, anti-bot controls, or access restrictions.
- A private Discord server does not automatically grant redistribution rights.

## Troubleshooting

- **`DISCORD_TOKEN is required`** - set the bot token in `.env` before running.
- **Partial Supabase/R2 configuration error** - set all fields for that service or leave all of them empty for degraded mode.
- **`/status` shows Unavailable** - check that the Supabase migration was applied and that the R2 bucket exists, the token has bucket access, and the endpoint is correct.
- **Slash commands are missing** - confirm the bot was invited with `applications.commands`; allow up to an hour for global sync or set `DISCORD_GUILD_ID` for immediate guild sync.
- **Port already in use** - change `PORT` or stop the other process. The health endpoint binds to `0.0.0.0`.

## Development roadmap

The locked V1 blueprint defines the implementation sequence:

1. Foundation: configuration, logging, Discord client, Supabase, R2, health endpoint. **Current.**
2. First Reddit-to-Discord vertical slice.
3. Media reliability, metadata, and provenance.
4. Reddit automation and Discord controls.
5. Brave discovery and controlled crawler, only if permitted.
6. Digest and publication reliability.
7. Render deployment and acceptance testing.

V1 is complete only when the acceptance checklist in Section 19 of the blueprint passes.

## License

No open-source license has been added. Unless a license is added later, all rights are reserved by the project owner.
