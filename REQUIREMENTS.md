# Telegram archive requirements

## Confirmed

- Async Python application for one personal Telegram account.
- Include all accessible chats by default, with per-chat exclusions.
- Support all content where possible, with configurable content exclusions.
- Retain saved messages after Telegram deletion and retain each captured edit.
- Provide a minimal web interface for browsing, reading, and searching the local archive.
- Primary deployment: Linux with Docker Compose.
- Also run as a regular Python program on macOS and Windows without Docker.
- Collect continuously and catch up after interruptions; determine reconciliation behavior from documented API capabilities and limits.
- Keep the architecture minimal.
- One SQLite database for messages and full-text search, plus attachment files on disk.
- Use SQLAlchemy's async API for database interaction, with aiosqlite as the SQLite driver.
- No built-in web authentication; access through localhost, SSH tunnel, or a secured reverse proxy.
- Chat and content settings live in a configuration file; restart to apply changes.
- Exclusions stop future collection and never remove existing archived data.
- Search text and captions across saved versions, with chat and date filters. No OCR, transcription, or attachment-content indexing initially.
- Download enabled attachments without a default size cap; provide an optional size limit. Expose failures and allow retries.
- Expected scale: approximately 100 chats; up to 1 TB of storage is acceptable.
- Best-effort cloud-chat capture: preserve every observed version; secret chats and content unavailable before capture cannot be recovered. Respect content-protection restrictions and report unavailable content.
- One-time terminal authorization with Telegram API credentials, phone, login code, and optional two-factor password. Persist the session; revoked authorization requires terminal login again.
- Begin live collection immediately and backfill history newest first in the background. Save message records before attachments and resume unfinished work after restarts.
- Discover chats hourly and reconcile the last seven days daily, with configurable intervals and server-directed flood waits. Older changes depend on update delivery.
- Global attachment-category switches with per-chat overrides: photos, videos, voice messages, video notes, audio, stickers/animations, and other files. Always retain text and metadata for included chats.
- Re-enabling chats or attachment categories queues still-accessible historical content.
- Web UI: chat list, chronological message view, search, version history, and collection status. Show replies, albums, downloadable attachments, and browser-playable media where supported.
- Show the newest saved version by default and allow expansion of earlier versions.
- Case-insensitive English and Russian word/phrase search; no fuzzy search or stemming.
- Operator manages off-server archive copies; document a consistent backup procedure without remote-storage integrations.
- Do not begin application code until the requirements and implementation details are resolved and shared understanding is confirmed.

## Review gate

The user approved the design, including async SQLAlchemy, and authorized implementation.
