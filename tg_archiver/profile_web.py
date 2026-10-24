"""Offline profile pages, cached photo serving, and shared downloaded media."""
import json
from types import SimpleNamespace

from aiohttp import web
from sqlalchemy import func, or_, select

from .avatars import cached_file
from .db import Attachment, Chat, Message, Profile, ProfilePhoto, Sender, Version, VersionAttachment


TABS = {"all": ("All files", None), "media": ("Photos & videos", {"photos", "videos", "video_notes", "stickers"}),
        "files": ("Documents", {"files"}), "audio": ("Music", {"audio"}), "voice": ("Voice", {"voice"})}


async def profile_page(request):
    from .web import integer, render_page, scroll_footer
    peer_id = int(request.match_info["peer_id"])
    if not -(2**63) <= peer_id < 2**63:
        raise web.HTTPNotFound()
    offset = integer(request.query.get("offset"))
    photo_offset = integer(request.query.get("photo_offset"))
    tab = request.query.get("tab", "all")
    if tab not in TABS:
        raise web.HTTPBadRequest(text="Unknown media tab")
    async with request.app["db"].session() as session:
        profile = await session.get(Profile, peer_id)
        chat = await session.get(Chat, peer_id)
        sender = await session.get(Sender, peer_id)
        if not profile and not chat and not sender:
            raise web.HTTPNotFound()
        name = (profile.name if profile else "") or (chat.title if chat else "") or (sender.name if sender else str(peer_id))
        current = chat or SimpleNamespace(id=peer_id, title=name)
        photos = (await session.scalars(select(ProfilePhoto).where(ProfilePhoto.profile_id == peer_id, ProfilePhoto.path.is_not(None))
            .order_by((ProfilePhoto.telegram_id == (profile.photo_id if profile else "")).desc(),
                      ProfilePhoto.date.desc(), ProfilePhoto.id.desc()).offset(photo_offset).limit(51))).all()
        available_photos = [photo for photo in photos[:50] if cached_file(request.app["config"].data_dir, photo.path)]
        # Include both directions of a private conversation, plus files sent by the
        # contact elsewhere in the archive; revisions can share the same attachment.
        condition = or_(Message.chat_id == peer_id, Message.sender_id == peer_id) if peer_id > 0 or not chat else Message.chat_id == peer_id
        shared = (select(VersionAttachment.attachment_id, func.max(Message.sent_at).label("sent_at"))
            .join(Version, Version.id == VersionAttachment.version_id).join(Message, Message.id == Version.message_id)
            .where(condition).group_by(VersionAttachment.attachment_id).subquery())
        base = select(Attachment).join(shared, shared.c.attachment_id == Attachment.id).where(
            Attachment.state == "complete", Attachment.path.is_not(None))
        categories = TABS[tab][1]
        if categories:
            base = base.where(Attachment.category.in_(categories))
        attachments = (await session.scalars(base.order_by(shared.c.sent_at.desc(), Attachment.id.desc()).offset(offset).limit(51))).all()
        counts = dict((await session.execute(select(Attachment.category, func.count()).join(shared, shared.c.attachment_id == Attachment.id)
            .where(Attachment.state == "complete", Attachment.path.is_not(None)).group_by(Attachment.category))).all())
    params = dict(tab=tab, photo_offset=photo_offset)
    tabs = [dict(key=key, label=label, count=sum(counts.values()) if categories is None else sum(counts.get(c, 0) for c in categories))
            for key, (label, categories) in TABS.items()]
    return await render_page(request, name, "profile.html", "profile", current=current, profile=profile,
        profile_name=name, has_conversation=chat is not None, details=json.loads(profile.details) if profile else {}, photos=available_photos,
        attachments=attachments[:50], tabs=tabs, selected_tab=tab,
        photo_footer=scroll_footer(f"/profile/{peer_id}", dict(tab=tab, offset=offset), photo_offset, len(photos) > 50, offset_key="photo_offset"),
        media_footer=scroll_footer(f"/profile/{peer_id}", params, offset, len(attachments) > 50))


async def profile_photo(request):
    row_id = int(request.match_info["photo_id"])
    if row_id >= 2**63:
        raise web.HTTPNotFound()
    async with request.app["db"].session() as session:
        item = await session.get(ProfilePhoto, row_id)
    path = cached_file(request.app["config"].data_dir, item.path) if item else None
    if not path:
        raise web.HTTPNotFound()
    response = web.FileResponse(path)
    response.headers["Content-Type"] = "image/jpeg"
    return response
