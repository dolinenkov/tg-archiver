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

## Sign in and import from the browser

Start `tg-archiver run`, open the archive, and select **Settings** in the left navigation. API credentials still come from `TG_API_ID` and `TG_API_HASH` on the server.

- **Telegram account:** enter your phone number, then the Telegram login code and two-step verification password if requested. The existing session is reused. Collection pauses during sign-in and resumes after success, cancellation, or expiry after five minutes. Codes and passwords are never stored in the archive.
- **Import a Telegram export:** enter the JSON file's path on the machine running the archiver, choose **Preview import**, review the counts, then choose **Import messages and attachments**. Keep media folders beside the JSON. This reads local files rather than uploading them; in Docker, mount the export directory and use its container path.

Preview and import pause collection automatically and reuse the running database and process lock. Work continues if you navigate away; return to Settings for results. Only one operation runs at a time. Settings refreshes automatically during work; without JavaScript, use the refresh link. CLI login and import remain available.

## Import a Telegram Desktop export

Stop the archiver, then point the offline importer at a Telegram Desktop **JSON** export. Keep its accompanying media folders beside `result.json`, with their original relative paths. Both single-chat exports and full-account exports (including `left_chats`) are supported. No Telegram login, API credentials, or network access is needed.

```sh
tg-archiver import /path/to/export/result.json --dry-run
tg-archiver import /path/to/export/result.json
# Custom configuration:
tg-archiver import /path/to/export/result.json --config /path/to/config.toml
```

The dry run validates the entire JSON and reports new messages, existing messages, excluded messages, files to copy, missing files, and filtered media. It does not initialize or modify the archive database or copy media; the CLI may create the data directory and its lock file. The regular import uses the same exclusive lock as other commands.

Import is additive: existing `(chat_id, message_id)` entries are skipped entirely, preserving captured edits, deletions, and collection checkpoints. Repeating an import is safe, but does not repair missing attachments on already imported messages. Chat exclusions and media filters apply. Available attachments are copied into `media/<chat_id>/<attachment_id>.<original extension>`; unavailable files are recorded without attempting Telegram downloads. Text is searchable immediately, timestamps are normalized to UTC, and original message JSON remains available under **Export metadata**.

A full export's account ID is checked against the archive and recorded when available. Single-chat exports may not identify the owning account; use exports from the same account as the archive. Exports provide snapshots rather than complete edit/deletion histories. Later live collection may save a richer Telegram observation as another version. Cross-chat reply targets and album layout are not reconstructed; their original metadata is retained.

JSON is loaded into memory and validated before import. Writes commit in batches of 100 messages; after interruption, rerun the command to skip completed batches. Keep the source export until import succeeds. No archive schema change or additional dependency is required.

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

The web UI uses a Telegram-style layout: a persistent chat sidebar with message previews, an independent conversation pane, and light/dark themes. On mobile, the chat list and conversation use separate views. Selecting chats and submitting search updates the main pane without reloading the sidebar; regular links also work without JavaScript. Conversations read oldest to newest and open at the latest message. Scroll upward to load older history while keeping your reading position. Message times, chat previews, captured edits, status timestamps, and day dividers use the browser’s time zone and locale, including daylight-saving changes. Stored timestamps and search date filters remain UTC; without JavaScript, timestamps display in UTC.

Current contact/group/channel avatars download in the background and are cached as `avatars/<chat_id>.jpg` inside the data directory. Discovery on startup and hourly checks for changed or removed photos; unchanged photos are reused. Excluded chats do not fetch new photos. The UI refreshes visible avatars automatically, with initials as a fallback. Full-size still profile photos and accessible photo history are also cached by the profile worker, including profiles of observed message senders. Failed avatar downloads honor flood waits and retry with backoff; `tg-archiver retry` also resets their retries.

- `/`: chats ordered by their newest archived message, including excluded chats with existing archive data. Chats without messages appear last.
- `/chat/ID`: messages in chronological order, opening at the latest message, sender labels, reply links, album IDs, captured versions, attachments.
- `/profile/ID`: contact details, cached profile-photo history with a fullscreen viewer, and downloaded shared media/files grouped by category. Open profiles from sidebar avatars, conversation headers, or message sender names/photos.
- `/search`: English/Russian case-insensitive words or quoted phrases, chat and date filters. Dates refer to the original message in UTC. Search includes older captured versions, with links to the matching version.
- `/status`: connection state, history progress, reconciliation timestamps, download states, flood waits, and errors.
- `/health`: database/process health; HTTP 503 on a fatal collection storage error. A healthy process does not guarantee Telegram authorization or connectivity; inspect `/status` too.

Messages are plain escaped text with clickable HTTP(S) links. Supported image/audio/video types use native browser playback; unsupported codecs and other files can be downloaded. Polls, locations, contacts, and other specialized types have a structured metadata fallback. Consecutive album messages are grouped within each page; the UI does not recreate Telegram's visual layout. No OCR, fuzzy search, stemming, or document-content indexing. Telegram speech transcriptions are indexed separately from message text.

Chats and search results load more as you scroll down; conversations load older messages as you scroll up, in batches of 50. Search filters remain active. A Load more link also works without JavaScript and lets you retry failed requests.

## Edit history and speech recognition

Every observed content change creates an immutable message version, including edits received live and different content found during history/reconciliation. Repeated observations do not create duplicate versions, and older observations never replace newer content. Expand **Version history** beneath an edited message to compare captured text, attachments, source times, and capture times. Search results open the matching saved version. Edits made before capture, or intermediate edits while the archiver was offline, cannot be reconstructed.

Telegram-native transcription runs automatically for queued voice, audio, round-video, and video attachments. Existing archives are queued on startup too. Results appear beside the recording and update while the conversation is open. Search covers captions and recognized speech, including recordings attached to older saved versions. Recognition results are stored separately from immutable message versions and shared across references to the same Telegram media.

```toml
[archive]
transcribe_audio = true # default; false stops new requests and keeps cached results
```

The worker calls Telegram's [`messages.transcribeAudio`](https://core.telegram.org/method/messages.transcribeAudio) on the original message. It never uploads local files or uses another recognition service, and does not need to download an attachment first. Telegram decides which media it supports; ordinary audio/video files may be rejected. Imported files without a verified Telegram media identity, inaccessible/deleted sources, and protected content are not transcribed.

[Telegram Premium and free-trial limits](https://core.telegram.org/api/transcribe) apply. Free-trial exhaustion pauses new requests until Telegram's reset time; in-flight results can still complete. Premium-required errors pause new requests for a day. Pending results survive restarts and are recovered using Telegram updates or polling. Flood waits use the shared API throttle. Private chats get priority; excluded chats do not initiate recognition. Check **Status → Speech recognition** and each recording for progress or unavailable reasons. The CLI `retry` command resets failed recognition work after stopping the app.

## Contact profiles and photos

Profiles are collected for included dialogs and observed message senders, including group senders without a private conversation. Available fields include names, phone numbers, usernames, bio/description, birthday, account badges, last-seen information, common-group count, and business address/introduction/hours. Telegram privacy and API access determine which fields and photos are visible; the archiver does not enumerate every member of every group. Contact details are cached snapshots, with the last check time shown.

A background worker downloads full-size **still images** from accessible profile-photo history, including current, personal, and public fallback photos where provided. Animated avatar video is not collected. Photos are cached under `avatars/profiles/<peer_id>/<photo_id>.jpg`, reused by sender avatars and profile galleries, and retained when a source photo is later removed. Nothing can recover photos deleted before collection. Downloads use atomic files, resume the history scan in pages of 20, honor flood waits and exclusions, and refresh daily; a changed current photo queues a fresh scan. Unavailable individual photos do not block the remaining history. Refresh the profile page to see newly collected details/photos.

Shared media tabs list completed attachment downloads, deduplicated across captured revisions. A contact profile includes both directions of its private conversation plus files sent by that contact across archived chats. Group/channel profiles show the conversation’s downloaded files. Photo and file lists load more as you scroll. Files remain available offline through the existing attachment routes; category filters here do not change collection settings.

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

If Telegram revokes authorization, open **Settings** to sign in again, or stop the service, run `login`, and restart. A data directory belongs to one Telegram account; do not reuse it for a different account. On persistent database errors or a full disk, collection stops and reports a fatal error rather than silently dropping writes; fix storage and restart.

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
