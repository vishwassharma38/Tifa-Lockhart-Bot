# Tifa Lockhart Bot

A personal, automated media-discovery, acquisition, preservation, and delivery system dedicated to Tifa Lockhart.

**Version:** 1.0 (locked V1 scope)  
**Runtime:** Python 3.12+  
**Discord integration:** `discord.py` 2.x  
**Database:** Supabase PostgreSQL  
**Media storage:** Private Cloudflare R2 bucket  
**Deployment target:** Render Web Service

> **V1 pipeline:** Discover → inspect → verify → acquire → deduplicate → preserve → record → deliver.

The complete, authoritative specification for V1 is [Tifa_Lockhart_Bot_V1_Blueprint.md](Tifa_Lockhart_Bot_V1_Blueprint.md). Implementation should follow that document; this README is the practical repository entry point.

## What V1 does

- Discovers Tifa-related submissions on Reddit through a modular PRAW connector, subject to authorized access.
- Discovers external public pages through a Brave Search HTML adapter, kept disabled until the planned access method is confirmed permissible.
- Inspects eligible public pages with a bounded, safety-conscious crawler.
- Acquires supported original media: JPG/JPEG, PNG, WebP, GIF, MP4, and WebM.
- Enforces a maximum media size of 10,000,000 bytes per file.
- Uses SHA-256 for exact duplicate detection; optional perceptual hashes are review hints only.
- Stores media in a private Cloudflare R2 bucket and metadata/provenance in Supabase.
- Delivers actual media attachments to Discord with source and creator attribution.
- Provides Discord slash commands: `/start`, `/stop`, `/save`, `/random`, `/status`, `/stats`, and `/about`.
- Schedules Reddit collection approximately hourly, Brave discovery every 8 hours when enabled, and a daily digest at 04:00 Asia/Kolkata.

Uncertain discoveries are staged in the database as **The Dumpster** and are not automatically published as verified finds.

## Architecture at a glance

The V1 application runs as one Python process in one Render Web Service:

- Discord Gateway client and slash-command interface
- APScheduler `AsyncIOScheduler`
- HTTP health endpoint at `/healthz`, bound to `0.0.0.0:$PORT`
- Source connectors, discovery/crawler pipeline, media processing, storage adapters, and database repositories

Supabase is the source of truth for metadata, queues, and job/publication state. R2 stores durable media binaries. The local filesystem is temporary working space only; do not rely on Render's local disk for persistent data.

## Repository layout

The codebase will be introduced incrementally. The target separation is:

```text
app/
  main.py
  config.py
  logging_config.py
  bot/
  connectors/
  discovery/
  media/
  storage/
  db/
  jobs/
  health/
migrations/
tests/
scripts/
.env.example
.gitignore
README.md
requirements.txt
Tifa_Lockhart_Bot_V1_Blueprint.md
```

See Section 14.2 of the V1 blueprint for the proposed module responsibilities.

## Requirements

- Python 3.12 or newer
- Git
- A Discord application/bot and a private test server
- Supabase project (PostgreSQL)
- Cloudflare R2 bucket and S3-compatible credentials
- Reddit credentials and authorization appropriate for the intended use before enabling Reddit collection
- Confirmation that the chosen Brave Search HTML access method is permitted before enabling that adapter

Provider credentials and account IDs are supplied by the project owner; none belong in source control.

## Local development setup

### 1. Clone the repository

```powershell
git clone https://github.com/vishwassharma38/Tifa-Lockhart-Bot.git
cd Tifa-Lockhart-Bot
```

If you already have the repository locally, open that existing folder instead.

### 2. Create and activate a virtual environment

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks activation, use the Python executable directly or adjust your execution policy only in accordance with your machine's security policy.

### 3. Install dependencies

Once `requirements.txt` has been added:

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Create your local environment file

Copy the committed template:

```powershell
Copy-Item .env.example .env
```

Edit `.env` with your own local configuration and credentials. **Never commit or share this file.** It is excluded by `.gitignore`.

### 5. Run the application

The application entry point and run command will be documented here when Phase 1 implementation is added. The current repository contains the finalized V1 blueprint and project setup files; the application itself has not yet been implemented.

## Configuration

All credentials and deployment-specific IDs are supplied through environment variables. See [.env.example](.env.example) for the complete V1 configuration template.

Important safety defaults:

- `REDDIT_ACCESS_APPROVED=false`: live Reddit collection must remain disabled until access is authorized.
- `BRAVE_HTML_ENABLED=false`: do not enable automated HTML discovery until the specific access method is confirmed permissible.
- Secrets are configured in Render's environment settings for deployment, not committed to the repository or baked into an image.

## V1 operating limits

| Control | Default |
|---|---:|
| Reddit candidates per scheduled run | 100 |
| Brave discovery interval | 8 hours |
| Maximum crawler depth | 6 |
| URLs admitted per web-discovery job | 300 |
| Pages per domain per job | 20 |
| Global concurrent crawler requests | 5 |
| Minimum delay between requests to one domain | 2 seconds |
| Maximum HTML response body | 5,000,000 bytes |
| Maximum redirects | 5 |
| Network timeout | 10s connect / 20s read |
| Media size ceiling | 10,000,000 bytes |
| Daily digest | 04:00 Asia/Kolkata |

A site's stricter limits, access restrictions, robots directives, and `Retry-After` instructions always take precedence.

## Security and responsible collection

- Never commit `.env`, tokens, passwords, private keys, or service-role credentials.
- Never bypass authentication, paywalls, DRM, CAPTCHAs, anti-bot controls, or other access restrictions.
- Respect platform policies, site terms, robots directives, copyright, takedown requests, and rate limits.
- Preserve creator attribution and source links where available.
- A private Discord server does not automatically grant redistribution rights.
- Apply SSRF protections to URLs and redirects; do not fetch private or internal network destinations.
- Do not log secrets or signed URLs containing credentials.

## Development roadmap

The locked V1 blueprint defines the implementation sequence:

1. Foundation: configuration, logging, Discord client, Supabase, R2, health endpoint.
2. First Reddit-to-Discord vertical slice.
3. Media reliability, metadata, and provenance.
4. Reddit automation and Discord controls.
5. Brave discovery and controlled crawler (only if permitted).
6. Digest and publication reliability.
7. Render deployment and acceptance testing.

V1 is complete only when the acceptance checklist in Section 19 of the blueprint passes.

## License

No open-source license has been added. Unless a license is added later, all rights are reserved by the project owner.
