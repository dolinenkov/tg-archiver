import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timezone
from html import escape
from itertools import groupby
import json
import mimetypes
import os
from pathlib import Path
import re
from urllib.parse import urlencode

from aiohttp import web
from jinja2 import Environment, PackageLoader, select_autoescape
from markupsafe import Markup
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError

from .avatars import cached_file
from .db import Attachment, Avatar, Chat, Message, MessageReactions, Profile, ProfilePhoto, Sender, Setting, Transcription, Version, VersionAttachment


IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
VIDEO_TYPES = {"video/mp4", "video/webm"}
AUDIO_TYPES = {"audio/ogg", "audio/mpeg", "audio/mp4", "audio/wav", "audio/webm"}


def media_type(item):
    mime = (item.mime or "").lower().split(";", 1)[0].strip()
    if mime in {"", "application/octet-stream", "binary/octet-stream"}:
        mime = mimetypes.guess_type(item.name or "")[0] or mime
    return mime
TEMPLATES = Environment(loader=PackageLoader("tg_archiver"), autoescape=select_autoescape(["html"]))


def e(value):
    return escape(str(value if value is not None else ""), quote=True)


def message_text(value):
    parts, offset = [], 0
    for match in re.finditer(r"https?://[^\s<>\"']+", value):
        parts.append(e(value[offset:match.start()]))
        url = match.group()
        parts.append(f'<a href="{e(url)}" rel="noreferrer noopener">{e(url)}</a>')
        offset = match.end()
    parts.append(e(value[offset:]))
    return Markup("".join(parts))


def fallback(raw, body):
    data = json.loads(raw)
    if data.get("_") == "TelegramDesktopExport":
        return Markup(f'<details class="message-version"><summary>Export metadata</summary><pre class="body">{e(json.dumps(data["export"], ensure_ascii=False, indent=2))}</pre></details>')
    media = data.get("media") or data.get("action")
    if not media:
        return Markup("")
    kind = media.get("_", "Content")
    if kind in {"MessageMediaPhoto", "MessageMediaDocument"}:
        return Markup("")
    return Markup(f'<details class="message-version"><summary>{e(kind)}</summary><pre class="body">{e(json.dumps(media, ensure_ascii=False, indent=2))}</pre></details>')


def display_date(value):
    if not value:
        return ""
    return datetime.fromisoformat(value).astimezone(timezone.utc).strftime("%d %b %Y · %H:%M")


def short_date(value):
    if not value:
        return ""
    date = datetime.fromisoformat(value).astimezone(timezone.utc)
    return date.strftime("%H:%M" if date.date() == datetime.now(timezone.utc).date() else "%d %b")


def initials(title):
    words = title.split()
    return "".join(word[0] for word in words[:2]).upper() or "?"


def reaction_labels(raw):
    if isinstance(raw, dict):
        raw = raw.get("results", [])
    if not isinstance(raw, list):
        return []
    result = []
    for entry in raw:
        if not isinstance(entry, dict) or not isinstance(entry.get("count"), int) or entry["count"] <= 0:
            continue
        reaction = entry.get("reaction", entry)
        if not isinstance(reaction, dict):
            continue
        kind = reaction.get("_", reaction.get("type"))
        if kind in ("ReactionEmoji", "emoji"):
            label = reaction.get("emoticon", reaction.get("emoji", ""))
        elif kind in ("ReactionCustomEmoji", "custom_emoji"):
            label = "Custom emoji"
        elif kind in ("ReactionPaid", "paid"):
            label = "⭐"
        else:
            continue
        if isinstance(label, str) and label:
            result.append({"label": label, "count": entry["count"], "chosen": entry.get("chosen_order") is not None})
    return result


TEMPLATES.filters.update(display_date=display_date, short_date=short_date)
TEMPLATES.filters['retry_time'] = lambda value: datetime.fromtimestamp(value, timezone.utc).isoformat()
TEMPLATES.globals.update(initials=initials, message_text=message_text, fallback=fallback,
                         image_types=IMAGE_TYPES, video_types=VIDEO_TYPES, audio_types=AUDIO_TYPES,
                         media_type=media_type)


@web.middleware
async def headers(request, handler):
    response = await handler(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = "default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'self'; img-src 'self'; media-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    return response


def integer(value, default=0, minimum=0):
    try:
        result = int(value) if value is not None else default
    except (ValueError, TypeError):
        raise web.HTTPBadRequest(text="Invalid integer") from None
    if result < minimum:
        raise web.HTTPBadRequest(text="Integer outside allowed range")
    return result


def date_filter(value, end=False):
    if not value:
        return None
    try:
        date = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise web.HTTPBadRequest(text="Dates must use YYYY-MM-DD") from None
    if end:
        from datetime import timedelta
        date += timedelta(days=1)
    return date.isoformat()


def scroll_footer(path, params, offset, has_more, older=False, offset_key="offset"):
    if has_more:
        url = path + "?" + urlencode(dict(params, **{offset_key: offset + 50}))
        label = "Load older messages" if older else "Load more"
        return Markup(f'<div class="scroll-footer"><a data-scroll-next href="{e(url)}">{label}</a><p data-scroll-status role="status" aria-live="polite"></p></div>')
    label = "Beginning of conversation." if older else "All items loaded."
    return Markup(f'<div class="scroll-footer"><p data-scroll-status role="status">{label}</p></div>')


async def sidebar_data(session, offset=0):
    latest = (select(Message.chat_id, func.max(Message.sent_at).label("sent_at"))
              .group_by(Message.chat_id).subquery())
    preview = (select(Version.body).join(Message, Message.latest_id == Version.id)
               .where(Message.chat_id == Chat.id).order_by(Message.sent_at.desc(), Message.telegram_id.desc())
               .limit(1).correlate(Chat).scalar_subquery())
    records = (await session.execute(select(Chat, latest.c.sent_at, preview)
               .outerjoin(latest, latest.c.chat_id == Chat.id)
               .order_by(latest.c.sent_at.desc(), Chat.id.desc()).offset(offset).limit(51))).all()
    return [dict(chat=chat, latest_at=sent_at, preview=body) for chat, sent_at, body in records[:50]], len(records) > 50


async def render_page(request, title, content_template, view, status=200, **context):
    offset = integer(request.query.get("offset")) if view == "home" else 0
    current = context.get("current")
    # In-app conversation navigation keeps the existing sidebar and app shell.
    if request.headers.get("X-Archive-Fragment") == "workspace" and view in {"chat", "message"}:
        async with request.app["db"].session() as session:
            photos = await avatar_versions(session, request.app["config"].data_dir, [current.id])
        html = TEMPLATES.get_template("workspace.html").render(
            title=title, content_template=content_template, view=view,
            current_chat_id=current.id, avatar_versions=photos, **context)
        return web.Response(text=html, content_type="text/html", status=status,
                            headers={"Vary": "X-Archive-Fragment"})
    async with request.app["db"].session() as session:
        sidebar_chats, more = await sidebar_data(session, offset)
        chat_count = await session.scalar(select(func.count()).select_from(Chat))
        ids = [row["chat"].id for row in sidebar_chats] + ([current.id] if current else [])
        photos = await avatar_versions(session, request.app["config"].data_dir, ids)
    collector = request.app["collector"]
    client = getattr(collector, "client", None)
    connected = bool(client and client.is_connected())
    html = TEMPLATES.get_template("base.html").render(
        title=title, content_template=content_template, view=view,
        current_chat_id=current.id if current else None, sidebar_chats=sidebar_chats,
        sidebar_footer=scroll_footer("/", {}, offset, more), chat_count=chat_count,
        connected=connected, avatar_versions=photos, **context)
    return web.Response(text=html, content_type="text/html", status=status,
                        headers={"Vary": "X-Archive-Fragment"})


async def chats(request):
    return await render_page(request, "Chats", "home.html", "home")


def render_message(message, versions, attachments, senders, profiles, saved_reactions,
                   transcripts, expanded=None, account_id=None, photos=None, transcription_enabled=True):
    versions = sorted(versions, key=lambda v: v.id != message.latest_id)
    sender = senders.get(message.sender_id)
    profile = profiles.get(message.sender_id)
    if saved_reactions:
        reactions = json.loads(saved_reactions.raw)
    else:
        raw = json.loads(versions[0].raw) if versions else {}
        reactions = raw.get("export", {}).get("reactions") if raw.get("_") == "TelegramDesktopExport" else raw.get("reactions")
    return TEMPLATES.get_template("message.html").render(
        message=message, versions=versions, version_attachments=attachments, expanded=expanded,
        outgoing=account_id is not None and str(message.sender_id) == account_id,
        sender=sender.name if sender else (profile.name if profile and profile.name else str(message.sender_id or "Service")),
        avatar_versions=photos or {}, transcripts=transcripts, transcription_enabled=transcription_enabled,
        reactions=reaction_labels(reactions))


async def render_messages(session, records, expanded=None, data_dir=None, transcription_enabled=True):
    records = list(records)
    # Fetch each related table once per page, rather than once per message/version.
    message_ids = [m.id for m in records]
    sender_ids = {m.sender_id for m in records if m.sender_id is not None}
    versions = defaultdict(list)
    for version in (await session.scalars(select(Version).where(Version.message_id.in_(message_ids))
                    .order_by(Version.source_at.desc(), Version.id.desc()))).all():
        versions[version.message_id].append(version)
    attachments = defaultdict(list)
    version_ids = [v.id for items in versions.values() for v in items]
    for version_id, attachment in (await session.execute(select(VersionAttachment.version_id, Attachment)
            .join(Attachment, Attachment.id == VersionAttachment.attachment_id)
            .where(VersionAttachment.version_id.in_(version_ids)))).all():
        attachments[version_id].append(attachment)
    attachment_ids = {a.id for items in attachments.values() for a in items}
    transcripts = {t.attachment_id: t for t in (await session.scalars(select(Transcription)
                   .where(Transcription.attachment_id.in_(attachment_ids)))).all()}
    senders = {s.id: s for s in (await session.scalars(select(Sender).where(Sender.id.in_(sender_ids)))).all()}
    profiles = {p.id: p for p in (await session.scalars(select(Profile).where(Profile.id.in_(sender_ids)))).all()}
    reactions = {r.message_id: r for r in (await session.scalars(select(MessageReactions)
                 .where(MessageReactions.message_id.in_(message_ids)))).all()}
    owner = await session.get(Setting, "account_id")
    # Cached paths only; no Telegram requests while rendering a conversation.
    photos = await avatar_versions(session, data_dir, [m.sender_id for m in records if m.sender_id is not None]) if data_dir else {}
    output = []
    previous_date = None
    for album, group in groupby(records, key=lambda m: m.album):
        if album:
            output.append(f'<section class="album" aria-label="Album {e(album)}"><h2>Album</h2>')
        for record in group:
            date = record.sent_at[:10]
            if date != previous_date:
                output.append(f'<div class="date-divider" data-message-date="{e(date)}"><span>{e(datetime.fromisoformat(record.sent_at).strftime("%d %B %Y"))}</span></div>')
                previous_date = date
            output.append(render_message(record, versions[record.id], attachments, senders, profiles, reactions.get(record.id),
                                         transcripts, expanded, owner.value if owner else None, photos, transcription_enabled))
        if album:
            output.append('</section>')
    return Markup("".join(output))


async def chat(request):
    chat_id = int(request.match_info["chat_id"])
    offset = integer(request.query.get("offset"))
    at = request.query.get("at")
    target_id, jump_notice = None, ""
    if at is not None:
        try:
            instant = datetime.fromisoformat(at)
            if instant.tzinfo is None:
                raise ValueError()
            at = instant.astimezone(timezone.utc).isoformat()
        except (ValueError, OverflowError):
            raise web.HTTPBadRequest(text="Date navigation requires a timestamp with a timezone") from None
    async with request.app["db"].session() as session:
        current = await session.get(Chat, chat_id)
        if not current:
            raise web.HTTPNotFound()
        if at is not None:
            target = await session.scalar(select(Message).where(Message.chat_id == chat_id, Message.sent_at >= at)
                .order_by(Message.sent_at, Message.telegram_id).limit(1))
            if target is None:
                target = await session.scalar(select(Message).where(Message.chat_id == chat_id)
                    .order_by(Message.sent_at.desc(), Message.telegram_id.desc()).limit(1))
                if target:
                    jump_notice = "No messages on or after this date. Showing the most recent message."
            if target:
                target_id = target.telegram_id
                newer_count = await session.scalar(select(func.count()).select_from(Message).where(
                    Message.chat_id == chat_id, (Message.sent_at > target.sent_at) |
                    ((Message.sent_at == target.sent_at) & (Message.telegram_id >= target.telegram_id))))
                # Keep a full page after the target, so a jump never becomes a
                # dead-end context view. Both edges continue loading normally.
                offset = max(0, newer_count - 50)
        records = (await session.scalars(select(Message).where(Message.chat_id == chat_id)
                   .order_by(Message.sent_at.desc(), Message.telegram_id.desc()).offset(offset).limit(51))).all()
        messages = await render_messages(session, reversed(records[:50]), data_dir=request.app["config"].data_dir,
                                         transcription_enabled=request.app["config"].transcribe_audio)
    return await render_page(request, current.title, "conversation.html", "chat", current=current,
                             messages=messages, target_id=target_id, jump_notice=jump_notice,
                             newer_footer=Markup(f'<div class="scroll-footer"><a data-scroll-newer href="/chat/{chat_id}?offset={max(0, offset - 50)}">Load newer messages</a><p data-scroll-status role="status" aria-live="polite"></p></div>') if offset else Markup(""),
                             footer=scroll_footer(f"/chat/{chat_id}", {}, offset, len(records) > 50, older=True))


async def message(request):
    chat_id, telegram_id = int(request.match_info["chat_id"]), int(request.match_info["message_id"])
    version = integer(request.query.get("version"))
    async with request.app["db"].session() as session:
        record = await session.scalar(select(Message).where(Message.chat_id == chat_id, Message.telegram_id == telegram_id))
        if not record:
            raise web.HTTPNotFound(text="Message not captured")
        current = await session.get(Chat, chat_id)
        records = (await session.scalars(select(Message).where(Message.chat_id == chat_id,
                   Message.telegram_id.between(max(0, telegram_id - 3), telegram_id + 3))
                   .order_by(Message.sent_at, Message.telegram_id))).all()
        messages = await render_messages(session, records, version, request.app["config"].data_dir, request.app["config"].transcribe_audio)
    return await render_page(request, f"Message #{telegram_id}", "conversation.html", "message",
                             current=current, messages=messages, footer=Markup(""), target_id=telegram_id)


async def search(request):
    params = request.query
    query = params.get("q", "").strip()
    if len(query) > 500:
        raise web.HTTPBadRequest(text="Search query too long")
    offset = integer(params.get("offset"))
    selected = params.get("chat", "")
    start, end = date_filter(params.get("from")), date_filter(params.get("to"), end=True)
    results, search_error = [], ""
    response_status = 200
    async with request.app["db"].session() as session:
        chat_options = (await session.scalars(select(Chat).order_by(Chat.title))).all()
        if query:
            sql = """SELECT v.id, v.body, m.chat_id, m.telegram_id, m.sent_at, c.title,
                (SELECT group_concat(t.body,char(10)) FROM transcriptions t JOIN version_attachments va
                 ON va.attachment_id=t.attachment_id WHERE va.version_id=v.id AND t.body!='') AS transcript
                FROM search JOIN versions v ON v.id=search.rowid JOIN messages m ON m.id=v.message_id
                JOIN chats c ON c.id=m.chat_id WHERE search MATCH :query"""
            values = {"query": query, "offset": offset}
            if selected:
                try:
                    values["chat"] = int(selected)
                except ValueError:
                    raise web.HTTPBadRequest(text="Invalid chat ID") from None
                sql += " AND m.chat_id=:chat"
            if start:
                sql += " AND m.sent_at>=:start"
                values["start"] = start
            if end:
                sql += " AND m.sent_at<:end"
                values["end"] = end
            sql += " ORDER BY m.sent_at DESC,v.id DESC LIMIT 51 OFFSET :offset"
            try:
                results = (await session.execute(text(sql), values)).mappings().all()
            except OperationalError as error:
                if any(term in str(error).lower() for term in ("fts5", "syntax", "unterminated", "no such column")):
                    search_error = "Invalid search expression. Try words or a quoted phrase."
                    response_status = 400
                else:
                    raise
    return await render_page(request, "Search", "search.html", "search", status=response_status,
                             query=query, selected=selected, from_date=params.get("from", ""), to_date=params.get("to", ""),
                             chat_options=chat_options, results=results[:50], search_error=search_error,
                             footer=scroll_footer("/search", dict(params), offset, len(results) > 50) if query else Markup(""))


async def attachment(request):
    async with request.app["db"].session() as session:
        item = await session.get(Attachment, int(request.match_info["attachment_id"]))
    if not item or item.state != "complete" or not item.path:
        raise web.HTTPNotFound()
    root = (request.app["config"].data_dir / "media").resolve()
    target = (root / item.path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise web.HTTPNotFound()
    mime = media_type(item)
    inline = request.query.get("inline") == "1" and mime in IMAGE_TYPES | VIDEO_TYPES | AUDIO_TYPES
    from urllib.parse import quote
    filename = quote(item.name, safe="")
    response = web.FileResponse(target)
    response.headers["Content-Type"] = mime if inline else "application/octet-stream"
    response.headers["Content-Disposition"] = f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{filename}"
    return response



DISK_CATEGORIES = {
    "photos": "Pictures", "videos": "Videos", "voice": "Voice messages",
    "video_notes": "Video messages", "audio": "Audio", "stickers": "Stickers and animations",
    "files": "Files",
}


def disk_usage(data_dir, attachments):
    """Count file sizes once, without following links outside the archive."""
    categories = {str(Path("media") / path): category for path, category in attachments if path}
    sizes = Counter()
    incomplete = False

    def onerror(error):
        nonlocal incomplete
        incomplete = True

    for root, dirs, files in os.walk(data_dir, followlinks=False, onerror=onerror):
        dirs[:] = [name for name in dirs if not (Path(root) / name).is_symlink()]
        for name in files:
            path = Path(root) / name
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                category = categories.get(str(path.relative_to(data_dir)))
                sizes[category if category in DISK_CATEGORIES else "other"] += path.stat().st_size
            except OSError:
                incomplete = True
    return sizes, incomplete


def format_bytes(size):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if size < 1024 or unit == "PiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1024


async def status(request):
    async with request.app["db"].session() as session:
        settings = (await session.scalars(select(Setting).order_by(Setting.key))).all()
        counts = (await session.execute(select(Attachment.state, func.count()).group_by(Attachment.state))).all()
        attachment_paths = (await session.execute(select(Attachment.path, Attachment.category).where(Attachment.path.is_not(None)))).all()
        failures = (await session.scalars(select(Attachment).where(Attachment.error != "").limit(50))).all()
        chats_with_errors = (await session.scalars(select(Chat).where(Chat.error != ""))).all()
        chat_progress = (await session.scalars(select(Chat).order_by(Chat.title))).all()
        transcription_counts = (await session.execute(select(Transcription.state, func.count()).group_by(Transcription.state))).all()
        retry_setting = next((s for s in settings if s.key == 'transcription_retry_at'), None)
        transcription_retry_at = datetime.fromtimestamp(float(retry_setting.value), timezone.utc).isoformat() if retry_setting else None
    sizes, disk_usage_incomplete = await asyncio.to_thread(disk_usage, request.app["config"].data_dir, attachment_paths)
    disk_usage_rows = [(label, format_bytes(sizes.get(category, 0))) for category, label in DISK_CATEGORIES.items()]
    disk_usage_rows.append(("Other archive files", format_bytes(sizes.get("other", 0))))
    return await render_page(request, "Status", "status.html", "status", fatal=request.app["collector"].fatal,
                             disk_usage_total=format_bytes(sum(sizes.values())), disk_usage_rows=disk_usage_rows,
                             disk_usage_incomplete=disk_usage_incomplete,
                             transcription_counts=transcription_counts, transcription_enabled=request.app["config"].transcribe_audio,
                             transcription_retry_at=transcription_retry_at,
                             settings=settings, counts=counts, failures=failures,
                             chats_with_errors=chats_with_errors, chat_progress=chat_progress)


async def health(request):
    async with request.app["db"].session() as session:
        await session.execute(text("SELECT 1"))
    fatal = request.app["collector"].fatal
    return web.json_response({"ok": not bool(fatal), "collection": "stopped" if fatal else "running"}, status=503 if fatal else 200)


async def avatar_versions(session, data_dir, ids):
    records = (await session.scalars(select(Avatar).where(
        Avatar.chat_id.in_(ids), Avatar.path.is_not(None), Avatar.downloaded_photo_id != ""))).all()
    versions = {item.chat_id: item.downloaded_photo_id for item in records if cached_file(data_dir, item.path)}
    photos = (await session.execute(select(Profile.id, ProfilePhoto).join(ProfilePhoto,
        (ProfilePhoto.profile_id == Profile.id) & (ProfilePhoto.telegram_id == Profile.photo_id)).where(Profile.id.in_(ids)))).all()
    for peer_id, photo in photos:
        if cached_file(data_dir, photo.path):
            versions[peer_id] = photo.telegram_id
    return versions


async def avatars(request):
    values = request.query.get("ids", "").split(",")
    if len(values) > 100:
        raise web.HTTPBadRequest(text="Too many chat IDs")
    try:
        ids = {int(value) for value in values if value}
    except ValueError:
        raise web.HTTPBadRequest(text="Invalid chat ID") from None
    if any(value < -(2 ** 63) or value >= 2 ** 63 for value in ids):
        raise web.HTTPBadRequest(text="Chat ID outside allowed range")
    async with request.app["db"].session() as session:
        photos = await avatar_versions(session, request.app["config"].data_dir, ids)
    return web.json_response({str(chat_id): photos.get(chat_id, "") for chat_id in ids})


async def transcriptions(request):
    values = request.query.get("ids", "").split(",")
    if len(values) > 100:
        raise web.HTTPBadRequest(text="Too many attachment IDs")
    try:
        ids = {int(value) for value in values if value}
    except ValueError:
        raise web.HTTPBadRequest(text="Invalid attachment ID") from None
    if any(value <= 0 or value >= 2**63 for value in ids):
        raise web.HTTPBadRequest(text="Invalid attachment ID")
    async with request.app["db"].session() as session:
        items = (await session.scalars(select(Transcription).where(Transcription.attachment_id.in_(ids)))).all()
    return web.json_response({str(item.attachment_id): dict(body=item.body, state=item.state, error=item.error) for item in items})


async def avatar(request):
    chat_id = int(request.match_info["chat_id"])
    if chat_id < -(2 ** 63) or chat_id >= 2 ** 63:
        raise web.HTTPNotFound()
    async with request.app["db"].session() as session:
        item = await session.get(Avatar, chat_id)
        photo = await session.scalar(select(ProfilePhoto).join(Profile,
            (Profile.id == ProfilePhoto.profile_id) & (Profile.photo_id == ProfilePhoto.telegram_id)).where(Profile.id == chat_id))
    target = cached_file(request.app["config"].data_dir, photo.path) if photo else None
    if not target:
        target = cached_file(request.app["config"].data_dir, item.path) if item else None
    if not target:
        raise web.HTTPNotFound()
    response = web.FileResponse(target)
    response.headers["Content-Type"] = "image/jpeg"
    return response



def make_app(db, config, collector):
    from .setup_web import Setup
    from .profile_web import profile_page, profile_photo
    app = web.Application(middlewares=[headers])
    app["db"], app["config"], app["collector"] = db, config, collector
    setup = Setup(db, config, collector)
    app["setup"] = setup
    app.on_shutdown.append(setup.cleanup)
    app.add_routes([web.static("/static", Path(__file__).parent / "static"), web.get("/", chats),
                    web.get(r"/chat/{chat_id:-?\d+}", chat),
                    web.get(r"/message/{chat_id:-?\d+}/{message_id:\d+}", message),
                    web.get("/search", search), web.get("/status", status), web.get("/health", health),
                    web.get("/settings", setup.page), web.post("/settings", setup.post),
                    web.get(r"/profile/{peer_id:-?\d+}", profile_page),
                    web.get(r"/profile-photo/{photo_id:\d+}", profile_photo),
                    web.get("/avatars", avatars), web.get(r"/avatar/{chat_id:-?\d+}", avatar),
                    web.get("/transcriptions", transcriptions),
                    web.get(r"/attachment/{attachment_id:\d+}", attachment)])
    return app
