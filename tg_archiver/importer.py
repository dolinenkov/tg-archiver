"""Offline, additive import of Telegram Desktop result.json exports."""
from collections import Counter
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import mimetypes
from pathlib import Path, PureWindowsPath
import re
import shutil
import sqlite3
import tempfile

from sqlalchemy import text

from .archive import stamp
from .db import Attachment, Chat, Database, Message, Sender, Version, VersionAttachment
from .profiles import queue_profile
from .transcription import queue_transcription


USER_TYPES = {"personal_chat", "bot_chat", "saved_messages", "replies", "verification_codes"}
CHANNEL_TYPES = {"private_supergroup", "public_supergroup", "private_channel", "public_channel"}


def integer(value):
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)):
        raise ValueError("Export contains an invalid numeric ID")
    result = int(value)
    if not 0 < result < 2**63 - 10**12:
        raise ValueError("Export contains an out-of-range ID")
    return result


def peer(value):
    if value is None:
        return None
    match = re.fullmatch(r"(user|chat|channel)([0-9]+)", str(value))
    if not match:
        raise ValueError("Export contains an invalid sender ID")
    kind, number = match.groups()
    number = integer(number)
    return number if kind == "user" else (-number if kind == "chat" else -10**12 - number)


def timestamp(record, edited=False):
    prefix = "edited" if edited else "date"
    value = record.get(prefix + "_unixtime")
    try:
        if value is not None:
            return stamp(datetime.fromtimestamp(int(value), timezone.utc))
        date = datetime.fromisoformat(record[prefix])
        if date.tzinfo is None:
            raise ValueError("Export dates without an offset require date_unixtime")
        return stamp(date)
    except (KeyError, TypeError, OverflowError, OSError) as error:
        raise ValueError("Export contains an invalid timestamp") from error


def body_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(body_text(part) for part in value)
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        return value["text"]
    raise ValueError("Export contains invalid message text")


def media(record, root):
    key = "file" if record.get("file") else "photo" if record.get("photo") else None
    if key is None:
        return None
    name = record[key]
    if not isinstance(name, str):
        raise ValueError("Export attachment path must be a string")
    # Telegram writes explanatory placeholders for media omitted from an export.
    source = None
    if not name.startswith("("):
        if PureWindowsPath(name).is_absolute() or "\\" in name:
            raise ValueError("Unsafe export attachment path")
        candidate = (root / name).resolve()
        if not candidate.is_relative_to(root):
            raise ValueError("Export attachment escapes the export directory")
        if candidate.is_file():
            source = candidate
    filename = record.get("file_name") or PureWindowsPath(name).name
    if not isinstance(filename, str):
        raise ValueError("Invalid export attachment filename")
    mime = record.get("mime_type") or mimetypes.guess_type(filename)[0] or ("image/jpeg" if key == "photo" else "application/octet-stream")
    if not isinstance(mime, str):
        raise ValueError("Invalid export MIME type")
    category = {"sticker": "stickers", "animation": "stickers", "video_message": "video_notes",
                "voice_message": "voice", "video_file": "videos", "audio_file": "audio"}.get(record.get("media_type"), "photos" if key == "photo" else "files")
    size = source.stat().st_size if source else 0
    return dict(source=source, name=filename, mime=mime, size=size, category=category)


def read_export(path, config):
    path = path.resolve()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, UnicodeError):
        raise ValueError("Invalid UTF-8 Telegram JSON export") from None
    if not isinstance(data, dict):
        raise ValueError("Expected a Telegram Desktop JSON object")
    if "messages" in data:
        chats = [data]
    else:
        chats = []
        for key in ("chats", "left_chats"):
            section = data.get(key, {})
            if not isinstance(section, dict) or not isinstance(section.get("list", []), list):
                raise ValueError("Invalid export chat list")
            chats.extend(section.get("list", []))
        if not any(key in data for key in ("chats", "left_chats")):
            raise ValueError("Expected a full-account or single-chat Telegram export")
    personal = data.get("personal_information", {})
    if not isinstance(personal, dict):
        raise ValueError("Invalid export account information")
    owner = personal.get("user_id")
    owner = integer(owner) if owner is not None else None
    plan, seen = [], set()
    for chat in chats:
        if not isinstance(chat, dict):
            raise ValueError("Invalid export chat")
        kind, bare = chat.get("type"), integer(chat.get("id"))
        if kind in USER_TYPES:
            chat_id, db_kind = bare, "User"
        elif kind == "private_group":
            chat_id, db_kind = -bare, "Chat"
        elif kind in CHANNEL_TYPES:
            chat_id, db_kind = -10**12 - bare, "Channel"
        else:
            raise ValueError("Unsupported export chat type")
        if kind == "saved_messages":
            if owner is not None and owner != bare:
                raise ValueError("Export has inconsistent account IDs")
            owner = bare
        records = chat.get("messages")
        if not isinstance(records, list):
            raise ValueError("Invalid export messages list")
        title = chat.get("name") or ("Saved Messages" if kind == "saved_messages" else str(chat_id))
        if not isinstance(title, str):
            raise ValueError("Invalid export chat name")
        for record in records:
            if not isinstance(record, dict) or record.get("type") not in {"message", "service"}:
                raise ValueError("Unsupported export message type")
            message_id = integer(record.get("id"))
            identity = (chat_id, message_id)
            if identity in seen:
                raise ValueError("Duplicate message IDs inside export")
            seen.add(identity)
            sent = timestamp(record)
            edited = timestamp(record, True) if record.get("edited") or record.get("edited_unixtime") else sent
            sender = peer(record.get("from_id") or record.get("actor_id"))
            sender_name = record.get("from") or record.get("actor") or str(sender)
            if not isinstance(sender_name, str):
                raise ValueError("Invalid export sender name")
            body = body_text(record.get("text", ""))
            attachment = media(record, path.parent)
            reply_to = integer(record["reply_to_message_id"]) if record.get("reply_to_message_id") else None
            if record.get("reply_to_peer_id") and peer(record["reply_to_peer_id"]) != chat_id:
                reply_to = None
            plan.append(dict(chat_id=chat_id, kind=db_kind, title=title, telegram_id=message_id,
                             sent_at=sent, source_at=edited, sender_id=sender, sender_name=sender_name,
                             reply_to=reply_to,
                             body=body, attachment=attachment, raw=record, included=config.included(chat_id)))
    return plan, owner


def existing_archive(config, plan, owner):
    """Read-only preflight: dry runs never initialize or migrate the real database."""
    path = config.data_dir / "archive.sqlite3"
    existing = set()
    if path.exists():
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
            known = connection.execute("SELECT value FROM settings WHERE key='account_id'").fetchone()
            if owner is not None and known and known[0] != str(owner):
                raise ValueError("Export belongs to a different Telegram account")
            for chat_id in {item["chat_id"] for item in plan}:
                existing.update((chat_id, row[0]) for row in connection.execute(
                    "SELECT telegram_id FROM messages WHERE chat_id=?", (chat_id,)))
    return existing


async def import_export(config, path, dry_run=False, db=None):
    plan, owner = await asyncio.to_thread(read_export, path, config)
    existing = await asyncio.to_thread(existing_archive, config, plan, owner)
    counts = Counter(messages=0, existing=0, excluded=0, attachments=0, missing=0, skipped_media=0)
    pending = []
    for item in plan:
        if not item["included"]:
            counts["excluded"] += 1
        elif (item["chat_id"], item["telegram_id"]) in existing:
            counts["existing"] += 1
        else:
            counts["messages"] += 1
            pending.append(item)
            info = item["attachment"]
            if info:
                if not config.allows(item["chat_id"], info["category"], info["size"]):
                    counts["skipped_media"] += 1
                elif info["source"]:
                    counts["attachments"] += 1
                else:
                    counts["missing"] += 1
    if dry_run:
        return counts
    own_db = db is None
    db = db or Database(config.data_dir / "archive.sqlite3")
    try:
        if own_db:
            await db.initialize()
        if owner is not None:
            await db.set_status("account_id", str(owner))
        for start in range(0, len(pending), 100):
            async with db.write() as session:
                for item in pending[start:start + 100]:
                    await save_imported(session, config, item)
    finally:
        if own_db:
            await db.close()
    return counts


async def save_imported(session, config, item):
    chat_id = item["chat_id"]
    if not await session.get(Chat, chat_id):
        session.add(Chat(id=chat_id, kind=item["kind"], title=item["title"]))
        await session.flush()
    if item["sender_id"] is not None and not await session.get(Sender, item["sender_id"]):
        session.add(Sender(id=item["sender_id"], name=item["sender_name"]))
    await queue_profile(session, chat_id, name=item["title"])
    if item["sender_id"] is not None and config.included(item["sender_id"]):
        await queue_profile(session, item["sender_id"], name=item["sender_name"])
    message = Message(chat_id=chat_id, telegram_id=item["telegram_id"], sent_at=item["sent_at"],
                      sender_id=item["sender_id"], reply_to=item["reply_to"])
    session.add(message)
    await session.flush()
    raw = json.dumps({"_": "TelegramDesktopExport", "export": item["raw"]}, ensure_ascii=False, sort_keys=True)
    version = Version(message_id=message.id, source_at=item["source_at"], body=item["body"], raw=raw,
                      fingerprint=hashlib.sha256(raw.encode()).hexdigest())
    session.add(version)
    await session.flush()
    message.latest_id, message.latest_source_at = version.id, version.source_at
    await session.execute(text("INSERT INTO search(rowid,body) VALUES (:id,:body)"), {"id": version.id, "body": version.body})
    info = item["attachment"]
    if not info:
        return
    allowed = config.allows(chat_id, info["category"], info["size"])
    attachment = Attachment(media_key=f"export:{chat_id}:{message.telegram_id}", category=info["category"],
                            name=info["name"], mime=info["mime"], size=info["size"],
                            state="skipped" if not allowed else "unavailable", error="Not included in export" if allowed else "")
    session.add(attachment)
    await session.flush()
    if allowed and info["source"]:
        extension = PureWindowsPath(info["name"]).suffix
        if not re.fullmatch(r"\.[A-Za-z0-9_-]{1,32}", extension):
            extension = mimetypes.guess_extension(info["mime"]) or ".bin"
        relative = f"{chat_id}/{attachment.id}{extension}"
        target = config.data_dir / "media" / relative
        await asyncio.to_thread(copy_attachment, info["source"], target, config.data_dir)
        attachment.path, attachment.state, attachment.error = relative, "complete", ""
    session.add(VersionAttachment(version_id=version.id, attachment_id=attachment.id))
    await queue_transcription(session, attachment)


def copy_attachment(source, target, data_dir):
    # Resolve the destination too: an existing symlink must not redirect the copy.
    if not target.resolve().is_relative_to(data_dir.resolve() / "media"):
        raise ValueError("Unsafe archive media destination")
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".part", delete=False) as output:
            partial = Path(output.name)
            with source.open("rb") as input_file:
                shutil.copyfileobj(input_file, output)
        partial.replace(target)
    finally:
        if partial is not None:
            partial.unlink(missing_ok=True)
