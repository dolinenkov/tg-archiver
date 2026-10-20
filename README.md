# tg-archiver

A personal Telegram cloud-history archive: async Python, SQLAlchemy, SQLite FTS5, attachment files, and a small web UI. One process. All chats are included by default, including archived chats and Saved Messages.

Messages and captured edits remain after Telegram deletion. Live capture starts immediately; historical collection and attachment downloads run in the background. New chats are discovered hourly, recent messages reconciled daily, and collection progress survives restarts. Attachments can be filtered globally or per chat. Nothing automatically prunes the archive.

## Run with Python

Requires Python 3.12+ and SQLite with FTS5 (checked at startup).

```sh
python -m venv .venv
# Linux/macOS:
. .venv/bin/activate
# Windows PowerShell instead:
# .venv\Scripts\Activate.ps1
python -m pip install -e .
```

Copy `config.example.toml` to `config.toml`. Set Telegram API credentials from [my.telegram.org](https://my.telegram.org).

```sh
export TG_API_ID=123456
export TG_API_HASH=your_api_hash
# PowerShell instead:
# $env:TG_API_ID='123456'
# $env:TG_API_HASH='your_api_hash'

tg-archiver login
tg-archiver chats
tg-archiver run
```

`login` prompts for your phone number, Telegram login code, and two-factor password if enabled. Do not supply bot credentials. Subsequent runs use the saved session and never prompt. Browse <http://127.0.0.1:8080>. `python -m tg_archiver` works in place of `tg-archiver`.

Pass `--config /path/to/config.toml` to any command. Paths in configuration are relative to that configuration file. On Windows, forward slashes work in TOML paths. Stop the application before running `login`, `chats`, or `retry`; a file lock prevents concurrent ownership of the session and data directory.

## Run with Docker Compose

Copy the example configuration to `config.toml`, retaining `data_dir = "data"`. Change **host to `"0.0.0.0"`** so the web server accepts connections through the container interface. Compose publishes port 8080 on host loopback only.

Create `.env` beside `compose.yaml`:

```dotenv
TG_API_ID=123456
TG_API_HASH=your_api_hash
```

```sh
docker compose build
docker compose run --rm --no-deps archiver tg-archiver login
docker compose up -d
docker compose logs -f
```

List chat IDs while the service is stopped:

```sh
docker compose stop
docker compose run --rm --no-deps archiver tg-archiver chats
docker compose up -d
```

The image runs as root by default for portable bind-mount setup. Linux operators can set Compose `user: "UID:GID"` after making the data directory writable by that user.

## Configuration

Use the numeric, marked chat IDs printed by `chats`: users are positive, basic groups negative, channels/supergroups generally start with `-100`. Names and usernames can change; IDs are stable.

```toml
[archive]
data_dir = "data"
host = "127.0.0.1"
port = 8080
excluded_chats = [123456789]
max_file_bytes = 0 # unlimited
discovery_seconds = 3600
reconcile_seconds = 86400
reconcile_days = 7

[media]
videos = false

[chats."-1001234567890"]
enabled = true
max_file_bytes = 104857600

[chats."-1001234567890".media]
videos = true
```

Unspecified media categories are enabled. Categories: `photos`, `videos`, `voice`, `video_notes`, `audio`, `stickers` (also GIFs/animations), `files`. Included chats always retain text and metadata. Excluding a chat wins over its per-chat media settings.

Restart after configuration changes. Exclusions stop future collection/downloads; saved content stays visible and searchable. Changing effective settings for a chat restarts its resumable history scan, allowing newly enabled content to be discovered. This can be slow for large histories. Already downloaded files remain untouched.

## Browse and search

The web UI uses a Telegram-style layout: a persistent chat sidebar with message previews, an independent conversation pane, and light/dark themes. On mobile, the chat list and conversation use separate views. Selecting chats and submitting search updates the main pane without reloading the sidebar; regular links also work without JavaScript. Conversations read oldest to newest and open at the latest message. Scroll upward to load older history while keeping your reading position.

Current contact/group/channel avatars download in the background and are cached as `avatars/<chat_id>.jpg` inside the data directory. Discovery on startup and hourly checks for changed or removed photos; unchanged photos are reused. Excluded chats do not fetch new photos. The UI refreshes visible avatars automatically, with initials as a fallback. Only the current small profile photo is cached; profile-photo history is not collected. Failed avatar downloads honor flood waits and retry with backoff; `tg-archiver retry` also resets their retries.

- `/`: chats ordered by their newest archived message, including excluded chats with existing archive data. Chats without messages appear last.
- `/chat/ID`: messages in chronological order, opening at the latest message, sender labels, reply links, album IDs, captured versions, attachments.
- `/search`: English/Russian case-insensitive words or quoted phrases, chat and date filters. Dates refer to the original message in UTC. Search includes older captured versions, with links to the matching version.
- `/status`: connection state, history progress, reconciliation timestamps, download states, flood waits, and errors.
- `/health`: database/process health; HTTP 503 on a fatal collection storage error. A healthy process does not guarantee Telegram authorization or connectivity; inspect `/status` too.

Messages are plain escaped text with clickable HTTP(S) links. Supported image/audio/video types use native browser playback; unsupported codecs and other files can be downloaded. Polls, locations, contacts, and other specialized types have a structured metadata fallback. Consecutive album messages are grouped within each page; the UI does not recreate Telegram's visual layout. No OCR, transcription, fuzzy search, stemming, or search inside attachment files.

Chats and search results load more as you scroll down; conversations load older messages as you scroll up, in batches of 50. Search filters remain active. A Load more link also works without JavaScript and lets you retry failed requests.

## Recovery and limits

Private chats (including Saved Messages) come first in each history, offline catch-up, and reconciliation pass; history still rotates through all included chats. Their attachments take priority over group/channel attachments. Within each category, recent live attachments are preferred, with every tenth download preferring older work. A running request or download is not interrupted when private-chat work arrives.

Attachments larger than 10 MB (10 × 1024 × 1024 bytes) wait until all enabled chats have exhausted accessible history and offline catch-up, and the text worker is idle. Chats with reported history errors do not block this indefinitely. Eligible smaller attachments always come first, ahead of private-chat priority. Large files pause at the next download boundary when new text collection work starts; files already downloading finish normally. Unknown-size attachments are treated as small until their size is known.

Telethon receives pushed updates, reconnects, and attempts catch-up. On reconnection, the archiver also scans accessible messages newer than the saved pre-connection boundary, so new live messages do not hide an offline gap. Historical work rotates between chats. Channel/supergroup backfill requests explicit message-ID batches, avoiding the documented old-history message-box limit of `getHistory`; missing/inaccessible IDs cannot be reconstructed. A finished scan means the available API range was exhausted, not proof that every message ever sent was recovered.

History calls are conservatively paced. Telegram publishes no universal safe quota; server flood waits take precedence. Waits pause background requests and are visible in Status. Downloads are streamed, completed with an atomic rename, and recorded as pending/downloading/complete/skipped/unavailable/error. A download interrupted by a restart is retried from the beginning. File references are refreshed by refetching source messages. Recent attachments are prioritized while older work continues.

Transient failures retry with capped backoff. To retry unavailable attachments and history access errors after fixing access:

```sh
# Stop the application first.
tg-archiver retry
tg-archiver run
# Compose equivalent:
# docker compose stop
# docker compose run --rm --no-deps archiver tg-archiver retry
# docker compose up -d
```

If Telegram revokes authorization, stop the service, run `login`, and restart. A data directory belongs to one Telegram account; do not reuse it for a different account. On persistent database errors or a full disk, collection stops and reports a fatal error rather than silently dropping writes; fix storage and restart.

Every **observed** content revision is retained. Intermediate edits missed during downtime, content deleted before capture, and unavailable old media cannot be recovered. Telethon's session checkpoint is separate from archive commits, so a crash can lose an uncommitted observation. Daily reconciliation checks the configured recent window; it cannot reconstruct historical edit sequences. Deletion notices are best effort; missing history entries are never treated as proof of deletion. Edits to counters/reactions alone do not create content revisions.

Secret chats are device-specific and excluded. Stories, calls, drafts, scheduled messages not yet sent, member-list snapshots, and profile-photo history are outside scope. Protected content is not downloaded. Source chats are never marked read, edited, or deleted by the archiver.

## Protect and back up the archive

New attachments are stored as `media/<chat_id>/<attachment_id><extension>`, preserving the original filename extension when safe. Files without an extension use one inferred from their MIME type, or `.bin` when unknown. Shared media is stored once under the chat used to download it. Existing downloaded files retain their recorded paths and remain accessible.

There is no built-in web authentication. Keep the listener on localhost or use your secured reverse proxy/tunnel. The entire archive is private, and the Telegram session grants account access. The web server exposes attachment IDs, never the database, configuration, or session directory. Unix files are created with a private umask; on Windows restrict the data directory's ACL to your account. The application does not encrypt data; use disk/backup encryption if required.

For a consistent backup:

1. Stop the process (`docker compose stop` for Compose).
2. Copy the **entire** data directory and configuration to your backup destination. Protect credentials and session material as secrets. Include any SQLite WAL/SHM files still present.
3. Start the process again.

Restore into an empty directory, restore configuration/credentials, and run the same application version. Do not copy only a running SQLite database file. Session backups should not be started simultaneously on different servers.

## Development checks

Python renders autoescaped Jinja2 templates in `tg_archiver/templates/`. The frontend is custom CSS and TypeScript in `frontend/`, built with Vite; no frontend framework or Node server is used at runtime. Built assets in `tg_archiver/static/` are included in Python packages and committed so native installation works without Node.

To change the frontend, install Node.js 22.12+ (or a supported newer release):

```sh
npm ci
npm run build  # type-check and rebuild packaged assets
# During development, rebuild assets as files change; refresh the browser:
npm run dev
```

Run `npm run build` after frontend changes and include the updated static files. Docker uses a Node build stage and serves the result from the Python application.

```sh
python -m unittest discover -s tests -v
python -m compileall -q tg_archiver tests
```

Tests use synthetic Telegram objects and fake API boundaries; they do not log in or touch an account. Database and web integration tests require the declared dependencies. The web tests bind an ephemeral loopback socket. See [DESIGN.md](DESIGN.md) and [REQUIREMENTS.md](REQUIREMENTS.md) for the approved scope.
