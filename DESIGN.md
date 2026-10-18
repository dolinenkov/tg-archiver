# Implementation design

Status: proposed for final review; no application code authorized yet.

## Runtime

Python 3.12 or newer. One process and one asyncio event loop run Telethon, an aiohttp web server, and background collection tasks. Use SQLAlchemy's async engine and straightforward mapped models, with aiosqlite as the SQLite driver. Give each concurrent task or request its own short-lived AsyncSession; never share a session across concurrent tasks. Use explicit queries rather than implicit lazy loading. Keep FTS5-specific SQL explicit and execute it through SQLAlchemy. Use ordinary functions and small modules for configuration, storage, collection, web views, and the command entry point; no repository or service layers, external queue, or frontend build.

Provide terminal commands to log in, list chats and their stable IDs, and run the application. Login prompts never occur during unattended operation. Use a TOML configuration file and environment variables for Telegram API credentials. The Telegram session is a separate private file, never accessible through the web server. Only one archiver process may own a data directory.

## Data

Store chats, known senders, messages, immutable captured versions, attachments, and collection progress in SQLite. Identify messages by peer type, peer ID, and message ID; display names are never keys. Preserve source timestamps and observation timestamps in UTC. Store normalized searchable fields plus serialized Telegram message metadata for later interpretation. Changes to counters alone do not create message-content revisions; changes to text, entities, or attached content do. Out-of-order historical observations must not replace newer known versions.

Use FTS5 with Unicode tokenization for word and phrase search across versions. Bind SQL parameters and validate search syntax. Search results link to the matching version and its conversation context. Paginate chats, messages, and results; do not load entire histories into memory. Fail clearly at startup if SQLite lacks FTS5. Serialize write transactions; use a separate read connection for web requests.

Attachment files live under application-generated relative paths, referenced by database IDs. Preserve original filenames as metadata only. Reuse files for the same stable Telegram media identity when possible. Write to temporary files and atomically rename after successful completion. Pending, downloading, complete, skipped, unavailable, and retryable-error states are persistent. Recover interrupted downloads on restart; a partial file may restart from the beginning. Stream files rather than buffering them in memory. Old versions retain their attachment references.

## Collection

Register live handlers before catch-up; enable Telethon reconnect and catch-up. Save captured messages and pending attachment work transactionally. Resume history with durable per-chat progress. Deduplicate overlapping live, history, and reconciliation observations without dropping distinct content revisions.

Start live capture first. Backfill chats round-robin in bounded pages, newest first within each chat, so one large chat cannot block all others. Use one history/reconciliation worker and one attachment downloader initially. Prefer recent live attachments over historical downloads while ensuring historical work progresses. Refresh expired file references from accessible source messages. Honor flood waits and retry transient failures with bounded backoff; expose permanent failures and provide a terminal retry command.

Discover chats on startup and hourly. Include archived Telegram folders and Saved Messages. Automatically include newly discovered accessible chats unless excluded. Refresh titles and known sender details opportunistically, without exhaustive member-list collection. Handle group-to-supergroup migration as linked chat records. Forum topics are retained as message metadata and reply context, without a separate topic navigation feature initially.

Run daily reconciliation of the last seven days, comparing content with saved versions. Reconciliation cannot reconstruct intermediate edits. Do not infer deletion merely because a message is missing from a history page. Record deletion events when attributable; keep all captured data regardless. Report incomplete or inaccessible history honestly. Validate documented old-channel history limitations before claiming exhaustive coverage; use explicit-ID recovery where practical and expose unresolved gaps.

Configuration exclusions use stable chat IDs, with global content settings and per-chat overrides. Enabled included chats always retain text and metadata. Attachment categories have independent switches and an optional maximum file size. Exclusion never deletes saved data. Re-enabling triggers a resumable history revisit to discover newly enabled attachments; persist enough effective configuration state to detect this after restart. Conflicting or invalid settings fail at startup.

Secret chats, stories, calls, scheduled drafts, and profile-photo history are outside message-history collection. Preserve polls, contact cards, locations, service events, reactions, and other available message metadata; the initial UI gives unsupported specialized types a readable fallback. Do not promise every reaction/counter change. Respect protected content and expose its unavailable state. Capture is best effort, including across process crashes: Telegram session progress and archive commits are not a single atomic transaction.

## Web interface

Server-rendered, escaped HTML and a small stylesheet; no SPA or frontend build. Chat list, paginated conversation view, search, expandable versions, and status. Support replies, albums, safe links, images, and native browser audio/video playback when the format permits. Other attachments download. Render untrusted active formats as downloads, never executable inline content. Resolve attachment requests through database IDs and prevent paths escaping the attachment root.

Status reports connection/auth state, per-chat backfill progress, last reconciliation, attachment backlog, flood waits, and errors. Collection errors do not silently disappear. Existing archive views remain available during recoverable Telegram failures or revoked authorization. The application cannot send Telegram messages or edit/delete source messages; avoid marking conversations read.

Default native listener: 127.0.0.1:8080. In Compose, listen on the container interface but publish to host loopback by default. Authentication and public TLS belong to the operator's reverse proxy or tunnel. No settings editor or archive deletion endpoint.

## Operations and verification

One Compose service, a persistent data directory, a mounted configuration file, restart policy, and a lightweight health endpoint. Same application command runs natively on Linux, macOS, and Windows. Use filesystem permissions appropriate to each OS; never log secrets or message bodies. Archive data is not encrypted by the application; disk and backup encryption belong to the operator.

On disk-full or persistent database-write failure, stop collection rather than pretending success; report failure and retain committed data. Graceful shutdown cancels and awaits tasks and closes connections. Document stop-copy-start backup of the full data directory and configuration, including the private session; do not recommend copying a live SQLite database file alone. Retain content indefinitely; no automatic pruning.

Verify version preservation, idempotent replay, history/live overlap, restart progress, exclusions and re-enabling, flood waits/retries, attachment atomic completion and path safety, English/Russian search, and web escaping. Smoke-test Compose and native execution where available; clearly identify untested OS paths and live-account behavior. Real Telegram integration requires operator-provided credentials and consent; tests use synthetic fixtures.

## Primary references

- https://core.telegram.org/api/updates
- https://core.telegram.org/api/errors
- https://core.telegram.org/api/files
- https://core.telegram.org/api/content-protection
- https://docs.telethon.dev/en/stable/modules/client.html
- https://docs.aiohttp.org/en/stable/web_advanced.html
- https://aiosqlite.omnilib.dev/en/stable/
- https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html
- https://docs.sqlalchemy.org/en/20/dialects/sqlite.html
- https://www.sqlite.org/fts5.html
