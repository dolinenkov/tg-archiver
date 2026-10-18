from datetime import datetime, timezone
from html import escape
from itertools import groupby
import json
import re
from urllib.parse import urlencode

from aiohttp import web
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError

from .db import Attachment, Chat, Message, Sender, Setting, Version, VersionAttachment


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
    return "".join(parts)


STYLE = """
body{font:16px system-ui,sans-serif;max-width:1000px;margin:2rem auto;padding:0 1rem;background:#fafafa;color:#202124}
a{color:#1758a3} nav{display:flex;gap:1rem;margin-bottom:2rem} input,button,select{font:inherit;padding:.4rem;max-width:100%}
article{padding:1rem;margin:1rem 0;background:white;border:1px solid #ddd;border-radius:6px;overflow-wrap:anywhere}
.body{white-space:pre-wrap} small,.muted{color:#555} img,video{max-width:100%;max-height:500px} audio{max-width:100%}
table{width:100%;border-collapse:collapse} td,th{padding:.5rem;text-align:left;border-bottom:1px solid #ddd} summary{cursor:pointer}
form{display:flex;gap:.5rem;flex-wrap:wrap} .error{color:#a12121} :target{outline:2px solid #1758a3}
"""


@web.middleware
async def headers(request, handler):
    response = await handler(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self'; media-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    return response


def page(title, body, status=200):
    return web.Response(text=f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)} · Telegram archive</title><style>{STYLE}</style><nav><a href="/">Chats</a><a href="/search">Search</a><a href="/status">Status</a></nav><main><h1>{e(title)}</h1>{body}</main></html>',
                        content_type="text/html", status=status)


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


def pager(path, params, offset, has_more):
    links = []
    if offset:
        links.append(f'<a href="{e(path + "?" + urlencode(dict(params, offset=max(0, offset - 50))))}">Previous</a>')
    if has_more:
        links.append(f'<a href="{e(path + "?" + urlencode(dict(params, offset=offset + 50)))}">Next</a>')
    return '<nav aria-label="Pagination">' + " ".join(links) + "</nav>"


async def chats(request):
    offset = integer(request.query.get("offset"))
    async with request.app["db"].session() as session:
        records = (await session.scalars(select(Chat).order_by(Chat.title, Chat.id).offset(offset).limit(51))).all()
    rows = "".join(f'<tr><td><a href="/chat/{c.id}">{e(c.title)}</a><br><small>{c.id}</small></td><td>{"included" if c.enabled else "excluded"}</td><td>{e(c.error) if c.error else ("History API exhausted" if c.done else "Collecting history")}</td></tr>' for c in records[:50])
    return page("Chats", '<table><tr><th>Chat</th><th>Collection</th><th>History</th></tr>' + rows + '</table>' +
                ("<p>No chats discovered yet. See Status.</p>" if not records else "") + pager("/", {}, offset, len(records) > 50))


def attachment_html(attachment):
    name = e(attachment.name)
    if attachment.state != "complete":
        return f'<p>{name} <small>{e(attachment.state)} {e(attachment.error)}</small></p>'
    url = f"/attachment/{attachment.id}"
    media = ""
    # Only a small allowlist of passive formats may render inline.
    if attachment.mime in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
        media = f'<img loading="lazy" src="{url}?inline=1" alt="{name}">'
    elif attachment.mime in {"video/mp4", "video/webm"}:
        media = f'<video controls preload="none" src="{url}?inline=1"></video>'
    elif attachment.mime in {"audio/ogg", "audio/mpeg", "audio/mp4", "audio/wav", "audio/webm"}:
        media = f'<audio controls preload="none" src="{url}?inline=1"></audio>'
    return f'<div>{media}<p><a href="{url}">Download {name}</a> <small>{attachment.size:,} bytes</small></p></div>'


def fallback(raw, body):
    data = json.loads(raw)
    media = data.get("media") or data.get("action")
    if not media:
        return ""
    # Preserve structured message types without inventing a renderer for each Telegram constructor.
    kind = media.get("_", "Content")
    details = ""
    if kind not in {"MessageMediaPhoto", "MessageMediaDocument"}:
        details = f'<details><summary>{e(kind)}</summary><pre class="body">{e(json.dumps(media, ensure_ascii=False, indent=2))}</pre></details>'
    return details


async def render_message(session, message, expanded=None):
    versions = (await session.scalars(select(Version).where(Version.message_id == message.id).order_by(Version.source_at.desc(), Version.id.desc()))).all()
    sender = await session.get(Sender, message.sender_id) if message.sender_id else None
    title = sender.name if sender else str(message.sender_id or "Service")
    reply = f'<a href="/message/{message.chat_id}/{message.reply_to}">Reply to #{message.reply_to}</a>' if message.reply_to else ""
    album = f" · Album {e(message.album)}" if message.album else ""
    output = f'<article id="m{message.telegram_id}"><small>{e(title)} · {e(message.sent_at)} · #{message.telegram_id}{album} {reply} {" · Deleted in Telegram" if message.deleted_at else ""}</small>'
    versions.sort(key=lambda v: (v.id != message.latest_id, -v.id))
    for version in versions:
        attachments = (await session.scalars(select(Attachment).join(VersionAttachment).where(VersionAttachment.version_id == version.id))).all()
        body = f'<div class="body">{message_text(version.body)}</div>' + fallback(version.raw, version.body) + "".join(attachment_html(a) for a in attachments)
        label = f"Version saved {version.observed_at}; source {version.source_at}"
        if version.id == message.latest_id:
            output += body + f'<small>{e(label)}</small>'
        else:
            output += f'<details {"open" if expanded == version.id else ""}><summary>{e(label)}</summary>{body}</details>'
    return output + "</article>"


async def render_messages(session, records, expanded=None):
    output = []
    for album, group in groupby(records, key=lambda m: m.album):
        if album:
            output.append(f'<section aria-label="Album {e(album)}"><h2>Album</h2>')
        for record in group:
            output.append(await render_message(session, record, expanded))
        if album:
            output.append('</section>')
    return "".join(output)


async def chat(request):
    chat_id = int(request.match_info["chat_id"])
    offset = integer(request.query.get("offset"))
    async with request.app["db"].session() as session:
        current = await session.get(Chat, chat_id)
        if not current:
            raise web.HTTPNotFound()
        records = (await session.scalars(select(Message).where(Message.chat_id == chat_id).order_by(Message.sent_at.desc(), Message.telegram_id.desc()).offset(offset).limit(51))).all()
        body = await render_messages(session, reversed(records[:50]))
        if current.migrated_to:
            body = f'<p>This chat migrated to <a href="/chat/{current.migrated_to}">{current.migrated_to}</a>.</p>' + body
    return page(current.title, '<p>Newest page first; messages within each page are chronological. Times are UTC.</p>' + body + pager(f"/chat/{chat_id}", {}, offset, len(records) > 50))


async def message(request):
    chat_id, telegram_id = int(request.match_info["chat_id"]), int(request.match_info["message_id"])
    version = integer(request.query.get("version"))
    async with request.app["db"].session() as session:
        record = await session.scalar(select(Message).where(Message.chat_id == chat_id, Message.telegram_id == telegram_id))
        if not record:
            raise web.HTTPNotFound(text="Message not captured")
        records = (await session.scalars(select(Message).where(Message.chat_id == chat_id,
                   Message.telegram_id.between(max(0, telegram_id - 3), telegram_id + 3)).order_by(Message.telegram_id))).all()
        body = f'<p><a href="/chat/{chat_id}">Back to conversation</a></p>'
        body += await render_messages(session, records, version)
    return page(f"Message #{telegram_id}", body)


async def search(request):
    params = request.query
    query = params.get("q", "").strip()
    if len(query) > 500:
        raise web.HTTPBadRequest(text="Search query too long")
    offset = integer(params.get("offset"))
    selected = params.get("chat", "")
    start, end = date_filter(params.get("from")), date_filter(params.get("to"), end=True)
    async with request.app["db"].session() as session:
        chat_rows = (await session.scalars(select(Chat).order_by(Chat.title))).all()
        options = '<option value="">All chats</option>' + "".join(f'<option value="{c.id}" {"selected" if selected == str(c.id) else ""}>{e(c.title)}</option>' for c in chat_rows)
        body = f'<form><label>Text <input name="q" value="{e(query)}" maxlength="500"></label><label>Chat <select name="chat">{options}</select></label><label>From <input type="date" name="from" value="{e(params.get("from", ""))}"></label><label>To <input type="date" name="to" value="{e(params.get("to", ""))}"></label><button>Search</button></form><p>Words and quoted phrases; dates refer to the original message in UTC.</p>'
        if query:
            sql = "SELECT v.id, v.body, m.chat_id, m.telegram_id, m.sent_at, c.title FROM search JOIN versions v ON v.id=search.rowid JOIN messages m ON m.id=v.message_id JOIN chats c ON c.id=m.chat_id WHERE search MATCH :query"
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
                if "fts5" in str(error).lower() or "syntax" in str(error).lower() or "unterminated" in str(error).lower() or "no such column" in str(error).lower():
                    return page("Search", body + '<p class="error">Invalid search expression. Try words or a quoted phrase.</p>', status=400)
                raise
            for row in results[:50]:
                body += f'<article><a href="/message/{row["chat_id"]}/{row["telegram_id"]}?version={row["id"]}#m{row["telegram_id"]}">{e(row["title"])} · {e(row["sent_at"])}</a><div class="body">{e(row["body"][:1000])}</div></article>'
            if not results:
                body += "<p>No matches.</p>"
            body += pager("/search", dict(params), offset, len(results) > 50)
    return page("Search", body)


async def attachment(request):
    async with request.app["db"].session() as session:
        item = await session.get(Attachment, int(request.match_info["attachment_id"]))
    if not item or item.state != "complete" or not item.path:
        raise web.HTTPNotFound()
    root = (request.app["config"].data_dir / "media").resolve()
    target = (root / item.path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise web.HTTPNotFound()
    inline_types = {"image/jpeg", "image/png", "image/gif", "image/webp", "video/mp4", "video/webm", "audio/ogg", "audio/mpeg", "audio/mp4", "audio/wav", "audio/webm"}
    inline = request.query.get("inline") == "1" and item.mime in inline_types
    from urllib.parse import quote
    filename = quote(item.name, safe="")
    response = web.FileResponse(target)
    response.headers["Content-Type"] = item.mime if inline else "application/octet-stream"
    response.headers["Content-Disposition"] = f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{filename}"
    return response


async def status(request):
    async with request.app["db"].session() as session:
        settings = (await session.scalars(select(Setting).order_by(Setting.key))).all()
        counts = (await session.execute(select(Attachment.state, func.count()).group_by(Attachment.state))).all()
        failures = (await session.scalars(select(Attachment).where(Attachment.error != "").limit(50))).all()
        chats_with_errors = (await session.scalars(select(Chat).where(Chat.error != ""))).all()
        chat_progress = (await session.scalars(select(Chat).order_by(Chat.title))).all()
    collector = request.app["collector"]
    body = f'<p class="error">{e(collector.fatal or "")}</p><p>Telegram transport: {"connected" if collector.client.is_connected() else "disconnected"}</p>'
    body += "<dl>" + "".join(f'<dt>{e(s.key)}</dt><dd>{e(s.value)}</dd>' for s in settings if s.key != "account_id") + "</dl>"
    body += '<h2>Attachments</h2><ul>' + "".join(f'<li>{e(state)}: {count}</li>' for state, count in counts) + '</ul>'
    body += '<h2>History progress</h2><table><tr><th>Chat</th><th>Oldest processed ID</th><th>History</th><th>Last reconciliation</th></tr>' + "".join(f'<tr><td>{e(c.title)}</td><td>{c.cursor}</td><td>{"API exhausted" if c.done else "pending"}</td><td>{e(c.reconciled)}</td></tr>' for c in chat_progress) + '</table>'
    body += '<h2>Errors / unavailable content</h2><ul>' + "".join(f'<li>{e(c.title)}: {e(c.error)}</li>' for c in chats_with_errors)
    body += "".join(f'<li>Attachment {a.id}: {e(a.error)}</li>' for a in failures) + '</ul><p>Stop the application and run the retry command to retry failed history and unavailable attachments. Missing messages or versions cannot always be recovered.</p>'
    return page("Status", body)


async def health(request):
    async with request.app["db"].session() as session:
        await session.execute(text("SELECT 1"))
    fatal = request.app["collector"].fatal
    return web.json_response({"ok": not bool(fatal), "collection": "stopped" if fatal else "running"}, status=503 if fatal else 200)


def make_app(db, config, collector):
    app = web.Application(middlewares=[headers])
    app["db"], app["config"], app["collector"] = db, config, collector
    app.add_routes([web.get("/", chats), web.get(r"/chat/{chat_id:-?\d+}", chat),
                    web.get(r"/message/{chat_id:-?\d+}/{message_id:\d+}", message),
                    web.get("/search", search), web.get("/status", status), web.get("/health", health),
                    web.get(r"/attachment/{attachment_id:\d+}", attachment)])
    return app
