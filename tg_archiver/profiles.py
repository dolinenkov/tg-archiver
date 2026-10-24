"""Cached contact details and resumable full-size profile-photo history."""
import asyncio
from datetime import timezone
import json
import time

from sqlalchemy import exists, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from telethon import errors, functions, types, utils

from .avatars import cached_file, photo_id
from .db import Chat, Message, Profile, ProfilePhoto, now


def details_for(entity, full=None):
    """Keep displayable contact fields, never transport credentials/access hashes."""
    details = {}
    fields = {"first_name": "First name", "last_name": "Last name", "phone": "Phone number",
              "username": "Username", "title": "Name", "participants_count": "Members"}
    for key, label in fields.items():
        value = getattr(entity, key, None)
        if value is not None and value != "":
            details[label] = str(value)
    usernames = [item.username for item in getattr(entity, "usernames", []) or [] if getattr(item, "active", False)]
    if usernames:
        details["Usernames"] = ", ".join("@" + name for name in usernames)
    badges = [label for key, label in {"bot": "Bot", "verified": "Verified", "premium": "Premium",
              "contact": "Contact", "mutual_contact": "Mutual contact", "deleted": "Deleted account",
              "scam": "Scam warning", "fake": "Fake account warning"}.items() if getattr(entity, key, False)]
    if badges:
        details["Account"] = ", ".join(badges)
    status = getattr(entity, "status", None)
    if status:
        details["Last seen"] = {"UserStatusOnline": "Online when checked", "UserStatusOffline": "Offline when checked",
            "UserStatusRecently": "Recently", "UserStatusLastWeek": "Within a week", "UserStatusLastMonth": "Within a month"}.get(type(status).__name__, "Not available")
        if getattr(status, "was_online", None):
            details["Last seen at"] = status.was_online.astimezone(timezone.utc).isoformat()
    if full:
        for key, label in {"about": "Bio / description", "common_chats_count": "Groups in common",
                           "participants_count": "Members", "personal_channel_id": "Personal channel ID"}.items():
            value = getattr(full, key, None)
            if value is not None and value != "":
                details[label] = str(value)
        birthday = getattr(full, "birthday", None)
        if birthday:
            details["Birthday"] = f"{birthday.day:02d}/{birthday.month:02d}" + (f"/{birthday.year}" if birthday.year else "")
        location = getattr(full, "business_location", None)
        if location and location.address:
            details["Business address"] = location.address
        intro = getattr(full, "business_intro", None)
        if intro:
            details["Business introduction"] = "\n".join(value for value in [intro.title, intro.description] if value)
        hours = getattr(full, "business_work_hours", None)
        if hours:
            days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            def minute(value):
                day, mins = divmod(value, 1440)
                return f"{days[day % 7]} {mins // 60:02d}:{mins % 60:02d}"
            details["Business hours"] = hours.timezone_id + "\n" + "\n".join(
                f"{minute(item.start_minute)} – {minute(item.end_minute)}" for item in hours.weekly_open)
    return details


async def queue_profile(session, peer_id, entity=None, name=""):
    profile = await session.get(Profile, peer_id)
    if not profile:
        profile = Profile(id=peer_id, name=name or str(peer_id), details="{}", photo_id="", photo_offset=0, next_at=0)
        session.add(profile)
        await session.flush()
    if entity is not None and not getattr(entity, "min", False):
        profile.name = utils.get_display_name(entity) or name or profile.name
        # Full details are refreshed by the worker; discovery must not erase bio etc.
        details = json.loads(profile.details)
        details.update(details_for(entity))
        profile.details = json.dumps(details, ensure_ascii=False)
        current = photo_id(entity)
        if current != profile.photo_id:
            profile.photo_id = current
            profile.photo_offset, profile.next_at = 0, 0
    return profile


async def collect_profiles(collector):
    while True:
        await collector.wait_api()
        visible_chat = exists(select(Chat.id).where(Chat.id == Profile.id, Chat.enabled.is_(True)))
        visible_sender = exists(select(Message.id).join(Chat).where(Message.sender_id == Profile.id, Chat.enabled.is_(True)))
        async with collector.db.session() as session:
            profile = await session.scalar(select(Profile).where(Profile.next_at <= time.time(), or_(visible_chat, visible_sender))
                .order_by((Profile.id > 0).desc(), Profile.next_at, Profile.id).limit(1))
        if profile is None:
            await asyncio.sleep(5)
            continue
        if not await profile_included(collector, profile.id):
            async with collector.db.write() as session:
                (await session.get(Profile, profile.id)).next_at = time.time() + 3600
            continue
        try:
            await refresh_profile(collector, profile)
        except errors.FloodWaitError as error:
            await collector.flood(error)
            await retry_profile(collector, profile.id, "Telegram flood wait", collector.flood_until)
        except (SQLAlchemyError, OSError) as error:
            if isinstance(error, SQLAlchemyError) or getattr(error, "errno", None) in {1, 5, 13, 28, 30, 122}:
                raise
            await retry_profile(collector, profile.id, type(error).__name__, time.time() + 300)
        except Exception as error:
            await retry_profile(collector, profile.id, type(error).__name__, time.time() + 3600)
        await asyncio.sleep(2)


async def retry_profile(collector, peer_id, error, retry_at):
    async with collector.db.write() as session:
        item = await session.get(Profile, peer_id)
        item.error, item.next_at = error, retry_at


async def profile_included(collector, peer_id):
    if not collector.config.included(peer_id):
        return False
    async with collector.db.session() as session:
        sources = (await session.scalars(select(Chat.id).where(Chat.enabled.is_(True), or_(Chat.id == peer_id,
            exists(select(Message.id).where(Message.chat_id == Chat.id, Message.sender_id == peer_id)))))).all()
    return any(collector.config.included(chat_id) for chat_id in sources)


async def refresh_profile(collector, profile):
    if not await profile_included(collector, profile.id):
        return
    await collector.wait_api()
    entity = await collector.client.get_entity(profile.id)
    if isinstance(entity, types.User):
        request = functions.users.GetFullUserRequest(entity)
    elif isinstance(entity, types.Channel):
        request = functions.channels.GetFullChannelRequest(entity)
    elif isinstance(entity, types.Chat):
        request = functions.messages.GetFullChatRequest(entity.id)
    else:
        raise ValueError("Profile unavailable")
    await collector.wait_api()
    response = await collector.client(request)
    full = getattr(response, "full_user", None) or getattr(response, "full_chat", None)
    for candidate in list(getattr(response, "users", []) or []) + list(getattr(response, "chats", []) or []):
        if utils.get_peer_id(candidate) == profile.id and not getattr(candidate, "min", False):
            entity = candidate
            break
    async with collector.db.write() as session:
        current = await session.get(Profile, profile.id)
        if current.photo_id != profile.photo_id:
            return
        current = await queue_profile(session, profile.id, entity)
        current.details = json.dumps(details_for(entity, full), ensure_ascii=False)
        current.updated_at, current.error = now(), ""
        offset = current.photo_offset
    # Full user data may expose personal/public photos absent from the history list.
    for key, label in [("profile_photo", "Current photo"), ("personal_photo", "Personal photo"), ("fallback_photo", "Public photo")]:
        photo = getattr(full, key, None)
        if isinstance(photo, types.Photo):
            await save_photo(collector, profile.id, photo, label)
    count = 0
    await collector.wait_api()
    async for photo in collector.client.iter_profile_photos(entity, limit=20, offset=offset):
        if not await profile_included(collector, profile.id):
            return
        await save_photo(collector, profile.id, photo)
        count += 1
        async with collector.db.write() as session:
            current = await session.get(Profile, profile.id)
            # Discovery can queue a fresh scan while an older page is downloading.
            if current.photo_id != photo_id(entity):
                return
            current.photo_offset = offset + count
        await asyncio.sleep(1)
    async with collector.db.write() as session:
        current = await session.get(Profile, profile.id)
        if current.photo_id != photo_id(entity):
            return
        current.next_at = time.time() + (2 if count == 20 else 86400)
        unavailable = await session.scalar(select(func.count()).select_from(ProfilePhoto).where(
            ProfilePhoto.profile_id == profile.id, ProfilePhoto.error != ""))
        current.error = f"{unavailable} profile photos unavailable" if unavailable else ""
        if count < 20:
            current.photo_offset = 0


async def save_photo(collector, peer_id, photo, kind="Profile photo"):
    if not await profile_included(collector, peer_id):
        return
    telegram_id = str(photo.id)
    async with collector.db.write() as session:
        item = await session.scalar(select(ProfilePhoto).where(ProfilePhoto.profile_id == peer_id, ProfilePhoto.telegram_id == telegram_id))
        if item and cached_file(collector.config.data_dir, item.path):
            return
        if not item:
            item = ProfilePhoto(profile_id=peer_id, telegram_id=telegram_id,
                date=photo.date.astimezone(timezone.utc).isoformat() if photo.date else "", kind=kind)
            session.add(item)
            await session.flush()
        item_id = item.id
    relative = f"profiles/{peer_id}/{telegram_id}.jpg"
    target = collector.config.data_dir / "avatars" / relative
    partial = target.with_name(target.stem + ".part.jpg")
    try:
        await collector.wait_api()
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        if not photo.sizes:
            await photo_error(collector, item_id, "Profile image unavailable")
            return
        # Animated profile photos also carry video_sizes. Select the largest
        # still explicitly so MP4 bytes cannot be cached and served as JPEG.
        result = await collector.client.download_media(photo, file=str(partial), thumb=photo.sizes[-1])
        if not result or not partial.is_file() or not partial.stat().st_size:
            await photo_error(collector, item_id, "Profile photo unavailable")
            return
        if not await profile_included(collector, peer_id):
            return
        await asyncio.to_thread(partial.replace, target)
        async with collector.db.write() as session:
            saved = await session.get(ProfilePhoto, item_id)
            saved.path, saved.error = relative, ""
    except (errors.ForbiddenError, errors.BadRequestError) as error:
        # One removed/inaccessible photo must not block the rest of the history.
        await photo_error(collector, item_id, type(error).__name__)
    finally:
        await asyncio.to_thread(partial.unlink, missing_ok=True)


async def photo_error(collector, photo_id, error):
    async with collector.db.write() as session:
        (await session.get(ProfilePhoto, photo_id)).error = error
