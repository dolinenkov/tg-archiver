import base64
from datetime import datetime, timezone
import hashlib
import json

from sqlalchemy import select, text
from telethon import types, utils

from .db import Attachment, Chat, Message, Sender, Version, VersionAttachment
from .profiles import queue_profile
from .transcription import index_versions, queue_transcription


def stamp(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def json_default(value):
    if isinstance(value, datetime):
        return stamp(value)
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    raise TypeError(f"Unsupported Telegram metadata type: {type(value).__name__}")


def stable(value):
    # Transport credentials and mutable counters are not content revisions.
    ignored = {"access_hash", "file_reference", "dc_id", "date", "views", "forwards", "reactions", "results"}
    if isinstance(value, dict):
        return {key: stable(item) for key, item in value.items() if key not in ignored}
    if isinstance(value, list):
        return [stable(item) for item in value]
    return value


def media_info(message):
    file = message.file
    if not file:
        return None
    media = message.photo or message.document
    if not media:
        return None
    category = next((name for name, present in [
        ("photos", message.photo), ("stickers", message.sticker or message.gif),
        ("video_notes", message.video_note), ("voice", message.voice),
        ("videos", message.video), ("audio", message.audio),
    ] if present), "files")
    return dict(media_key=f"{type(media).__name__}:{media.id}", category=category,
                name=file.name or f"{media.id}{file.ext or ''}", mime=file.mime_type or "application/octet-stream",
                size=file.size or 0)


async def save_message(db, config, message, live=False, protected=False):
    chat_id = utils.get_peer_id(message.peer_id)
    if not config.included(chat_id):
        return
    raw_dict = message.to_dict()
    raw = json.dumps(raw_dict, default=json_default, ensure_ascii=False)
    body = message.message or ""
    content = {key: raw_dict.get(key) for key in ("message", "entities", "media", "action", "reply_markup")}
    fingerprint = hashlib.sha256(json.dumps(stable(content), default=json_default, sort_keys=True).encode()).hexdigest()
    source_at = stamp(message.edit_date or message.date)
    info = media_info(message)
    sender = message.sender
    async with db.write() as session:
        chat = await session.get(Chat, chat_id)
        if not chat:
            chat = Chat(id=chat_id, kind=type(message.peer_id).__name__, title=str(chat_id))
            session.add(chat)
            await session.flush()
        await queue_profile(session, chat_id, name=chat.title)
        if sender:
            sender_id = utils.get_peer_id(sender)
            name = utils.get_display_name(sender) or str(sender_id)
            known = await session.get(Sender, sender_id)
            if known:
                known.name = name
            else:
                session.add(Sender(id=sender_id, name=name))
            if config.included(sender_id):
                await queue_profile(session, sender_id, sender, name)
        elif message.sender_id is not None and config.included(message.sender_id):
            await queue_profile(session, message.sender_id)
        saved = await session.scalar(select(Message).where(Message.chat_id == chat_id, Message.telegram_id == message.id))
        if not saved:
            saved = Message(chat_id=chat_id, telegram_id=message.id, sent_at=stamp(message.date),
                            sender_id=message.sender_id, reply_to=message.reply_to_msg_id,
                            album=str(message.grouped_id) if message.grouped_id else None)
            session.add(saved)
            await session.flush()
        latest = await session.get(Version, saved.latest_id) if saved.latest_id else None
        version = await session.scalar(select(Version).where(Version.message_id == saved.id, Version.fingerprint == fingerprint,
                                                            Version.source_at == source_at))
        if not version and latest and latest.fingerprint == fingerprint:
            version = latest
        if not version:
            version = Version(message_id=saved.id, source_at=source_at, fingerprint=fingerprint, body=body, raw=raw)
            session.add(version)
            await session.flush()
            await session.execute(text("INSERT INTO search(rowid,body) VALUES (:id,:body)"), {"id": version.id, "body": body})
        if not latest or source_at >= saved.latest_source_at:
            saved.latest_id = version.id
            saved.latest_source_at = source_at
        if info:
            attachment = await session.scalar(select(Attachment).where(Attachment.media_key == info["media_key"]))
            allowed = config.allows(chat_id, info["category"], info["size"])
            blocked = protected or bool(getattr(message, "noforwards", False))
            if not attachment:
                attachment = Attachment(**info, state="unavailable" if blocked else ("pending" if allowed else "skipped"),
                                        error="Protected content" if blocked else "", priority=1 if live else 0)
                session.add(attachment)
                await session.flush()
            elif allowed and not blocked and attachment.state == "skipped":
                attachment.state, attachment.retry_at = "pending", 0
            if live:
                attachment.priority = 1
            if not await session.get(VersionAttachment, (version.id, attachment.id)):
                session.add(VersionAttachment(version_id=version.id, attachment_id=attachment.id))
                await session.flush()
            transcription = await queue_transcription(session, attachment, blocked)
            if transcription and transcription.body:
                await index_versions(session, [version.id])
        action = getattr(message, "action", None)
        if type(action).__name__ == "MessageActionChatMigrateTo":
            chat.migrated_to = utils.get_peer_id(types.PeerChannel(action.channel_id))


async def save_deletions(db, ids, chat_id=None, config=None):
    async with db.write() as session:
        query = select(Message).join(Chat).where(Message.telegram_id.in_(ids))
        if chat_id is not None:
            query = query.where(Message.chat_id == chat_id)
        else:
            # Private chats and basic groups share the account message-ID space.
            query = query.where(Chat.kind.in_(["PeerUser", "PeerChat", "User", "Chat"]))
        from .db import now
        for message in (await session.scalars(query)).all():
            if config is None or config.included(message.chat_id):
                message.deleted_at = message.deleted_at or now()
