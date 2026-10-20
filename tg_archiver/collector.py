import asyncio
from datetime import datetime, timedelta, timezone
import logging
import mimetypes
from pathlib import PureWindowsPath
import re
import time

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from telethon import TelegramClient, errors, events, types

from .archive import media_info, save_deletions, save_message
from .avatars import cached_file, photo_id
from .config import credentials
from .db import Attachment, Avatar, Chat, Message, Setting, Version, VersionAttachment, now


log = logging.getLogger(__name__)
STORAGE_ERRNOS = {1, 5, 13, 28, 30, 122} # permissions, I/O, full/read-only disk, quota
LARGE_ATTACHMENT_BYTES = 10 * 1024 * 1024


def client_for(config):
    api_id, api_hash = credentials()
    return TelegramClient(str(config.data_dir / "telegram"), api_id, api_hash,
                          sequential_updates=True, catch_up=True, flood_sleep_threshold=0,
                          auto_reconnect=True, request_retries=3)


class Collector:
    def __init__(self, db, config):
        self.db, self.config = db, config
        self.client = client_for(config)
        self.ready = asyncio.Event()
        self.fatal = None
        self.tasks = []
        self.flood_until = 0.0
        self.history_requests = 0
        self.download_count = 0
        self.verified = False
        self.text_idle = False

    async def start(self):
        self.client.add_event_handler(self.on_message, events.NewMessage())
        self.client.add_event_handler(self.on_message, events.MessageEdited())
        self.client.add_event_handler(self.on_delete, events.MessageDeleted())
        self.tasks = [asyncio.create_task(self.guard(name, work), name=name) for name, work in [
            ("connection", self.connection), ("history", self.history), ("attachments", self.downloads),
            ("avatars", self.avatars)]]

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.client.disconnect()

    async def guard(self, name, work):
        while True:
            try:
                await work()
                return
            except asyncio.CancelledError:
                raise
            except (SQLAlchemyError, OSError) as error:
                if isinstance(error, SQLAlchemyError) or getattr(error, "errno", None) in STORAGE_ERRNOS:
                    self.fatal = f"{name}: storage failure ({type(error).__name__}); collection stopped"
                    self.ready.clear()
                    await self.client.disconnect()
                    log.error(self.fatal)
                    return
                await self.report(name, error)
                await asyncio.sleep(30)
            except Exception as error:
                await self.report(name, error)
                await asyncio.sleep(30)

    async def report(self, operation, error):
        # Exception text can contain Telegram message bodies or credentials.
        description = f"{operation}: {type(error).__name__}"
        log.warning(description)
        try:
            await self.db.set_status("last_error", f"{now()} {description}")
        except (SQLAlchemyError, OSError):
            self.fatal = "Cannot persist collection status; collection stopped"
            self.ready.clear()
            log.error(self.fatal)
            await self.client.disconnect()

    async def wait_api(self):
        if self.fatal:
            await asyncio.Event().wait()
        await self.ready.wait()
        while time.time() < self.flood_until:
            await asyncio.sleep(min(30, self.flood_until - time.time()))

    async def flood(self, error):
        self.flood_until = max(self.flood_until, time.time() + error.seconds + 1)
        await self.db.set_status("flood_until", datetime.fromtimestamp(self.flood_until, timezone.utc).isoformat())

    async def connection(self):
        while not self.fatal:
            try:
                while time.time() < self.flood_until:
                    await asyncio.sleep(min(30, self.flood_until - time.time()))
                # Snapshot before receiving updates, so new live messages cannot hide an offline gap.
                async with self.db.write() as session:
                    for chat in (await session.scalars(select(Chat).where(Chat.enabled.is_(True)))).all():
                        if not chat.catchup_pending:
                            latest = await session.scalar(select(func.max(Message.telegram_id)).where(Message.chat_id == chat.id))
                            chat.catchup_floor = latest or 0
                        chat.catchup_cursor = 0
                        chat.catchup_pending = True
                await self.client.connect()
                if not await self.client.is_user_authorized():
                    self.ready.clear()
                    await self.db.set_status("connection", "Authorization required: stop and run login")
                    await self.client.disconnect()
                    await asyncio.sleep(60)
                    continue
                me = await self.client.get_me()
                if not me or me.bot:
                    self.fatal = "A personal Telegram account is required"
                    await self.client.disconnect()
                    return
                async with self.db.write() as session:
                    owner = await session.get(Setting, "account_id")
                    if owner and owner.value != str(me.id):
                        self.fatal = "Data directory belongs to a different Telegram account"
                        await self.client.disconnect()
                        return
                    if not owner:
                        session.add(Setting(key="account_id", value=str(me.id)))
                self.ready.set()
                self.verified = True
                await self.client.catch_up()
                await self.db.set_status("connection", "Connected")
                await self.client.run_until_disconnected()
            except errors.FloodWaitError as error:
                await self.flood(error)
            except (errors.UnauthorizedError, errors.AuthKeyError) as error:
                await self.report("authorization; stop and run login", error)
            finally:
                self.verified = False
                self.ready.clear()
            await self.db.set_status("connection", "Disconnected; reconnecting")
            await asyncio.sleep(max(5, min(60, self.flood_until - time.time())))

    async def on_message(self, event):
        await self.ready.wait()
        if self.fatal or not self.verified or not self.config.included(event.chat_id):
            return
        try:
            # Cached entities only: network requests in handlers delay update persistence.
            chat = event.chat
            await save_message(self.db, self.config, event.message, live=True,
                               protected=bool(getattr(chat, "noforwards", False)))
        except (SQLAlchemyError, OSError) as error:
            self.fatal = f"Live capture storage failure ({type(error).__name__}); collection stopped"
            self.ready.clear()
            log.error(self.fatal)
            await self.client.disconnect()
        except Exception as error:
            await self.report("live capture", error)

    async def on_delete(self, event):
        await self.ready.wait()
        if self.fatal or not self.verified:
            return
        try:
            await save_deletions(self.db, event.deleted_ids, event.chat_id, self.config)
        except (SQLAlchemyError, OSError) as error:
            self.fatal = f"Deletion storage failure ({type(error).__name__}); collection stopped"
            self.ready.clear()
            await self.client.disconnect()

    async def discover(self):
        async for dialog in self.client.iter_dialogs():
            fingerprint = self.config.fingerprint(dialog.id)
            async with self.db.write() as session:
                chat = await session.get(Chat, dialog.id)
                if not chat:
                    chat = Chat(id=dialog.id, kind=type(dialog.entity).__name__, title=dialog.name or str(dialog.id))
                    session.add(chat)
                    await session.flush()
                chat.title = dialog.name or str(dialog.id)
                chat.kind = type(dialog.entity).__name__
                chat.enabled = self.config.included(dialog.id)
                if chat.config_hash != fingerprint:
                    chat.cursor, chat.done, chat.error = 0, False, ""
                    chat.config_hash = fingerprint
                if chat.enabled:
                    await self.queue_avatar(session, chat.id, dialog.entity)
        await self.db.set_status("discovered", now())

    async def queue_avatar(self, session, chat_id, entity):
        current_id = photo_id(entity)
        item = await session.get(Avatar, chat_id)
        if not item:
            if current_id:
                session.add(Avatar(chat_id=chat_id, photo_id=current_id))
            return
        if current_id != item.photo_id:
            item.photo_id, item.retry_at, item.attempts, item.error = current_id, 0, 0, ""
        if not current_id or (item.path and not cached_file(self.config.data_dir, item.path)):
            item.path, item.downloaded_photo_id = None, ""

    async def avatars(self):
        while True:
            await self.wait_api()
            async with self.db.session() as session:
                item = await session.scalar(select(Avatar).join(Chat).where(
                    Chat.enabled.is_(True), Avatar.photo_id != "", Avatar.retry_at <= time.time(),
                    (Avatar.path.is_(None) | (Avatar.downloaded_photo_id != Avatar.photo_id)))
                    .order_by((Chat.id > 0).desc(), Avatar.chat_id).limit(1))
            if not item:
                await asyncio.sleep(5)
                continue
            if self.config.included(item.chat_id):
                await self.download_avatar(item)
            else:
                async with self.db.write() as session:
                    (await session.get(Chat, item.chat_id)).enabled = False
            await asyncio.sleep(1)

    async def download_avatar(self, item):
        if not self.config.included(item.chat_id):
            return
        relative = f"{item.chat_id}.jpg"
        target = self.config.data_dir / "avatars" / relative
        partial = target.with_name(target.stem + ".part.jpg")
        try:
            await self.wait_api()
            entity = await self.client.get_entity(item.chat_id)
            async with self.db.write() as session:
                current = await session.get(Avatar, item.chat_id)
                if current.photo_id != item.photo_id:
                    return
                await self.queue_avatar(session, item.chat_id, entity)
            if photo_id(entity) != item.photo_id:
                return
            await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
            result = await self.client.download_profile_photo(entity, file=str(partial), download_big=False)
            if not result or not partial.is_file() or not partial.stat().st_size:
                raise OSError("Profile photo unavailable")
            async with self.db.write() as session:
                current = await session.get(Avatar, item.chat_id)
                # Discovery may have observed a changed/removed photo during the download.
                if current.photo_id != item.photo_id or not self.config.included(item.chat_id):
                    return
                await asyncio.to_thread(partial.replace, target)
                current.path, current.downloaded_photo_id = relative, item.photo_id
                current.retry_at, current.attempts, current.error = 0, 0, ""
        except errors.FloodWaitError as error:
            await self.flood(error)
            await self.retry_avatar(item, "Telegram flood wait", self.flood_until)
        except (SQLAlchemyError, OSError) as error:
            if isinstance(error, SQLAlchemyError) or getattr(error, "errno", None) in STORAGE_ERRNOS:
                raise
            await self.retry_avatar(item, type(error).__name__)
        except Exception as error:
            await self.retry_avatar(item, type(error).__name__)
        finally:
            await asyncio.to_thread(partial.unlink, missing_ok=True)

    async def retry_avatar(self, item, error, retry_at=None):
        async with self.db.write() as session:
            current = await session.get(Avatar, item.chat_id)
            if current.photo_id != item.photo_id:
                return
            current.attempts += 1
            current.error = error
            current.retry_at = retry_at if retry_at is not None else time.time() + min(3600, 2 ** min(current.attempts + 2, 12))
        await self.db.set_status("last_error", f"{now()} avatar {item.chat_id}: {error}")

    async def history(self):
        discovered = reconciled = float("-inf")
        while True:
            self.text_idle = False
            await self.wait_api()
            try:
                if time.monotonic() - discovered >= self.config.discovery_seconds:
                    await self.discover()
                    discovered = time.monotonic()
                async with self.db.session() as session:
                    chats = (await session.scalars(select(Chat).where(Chat.enabled.is_(True))
                             .order_by((Chat.id > 0).desc(), Chat.id))).all()
                pending = False
                for chat in chats:
                    if chat.catchup_pending and not chat.error and self.config.included(chat.id):
                        pending = True
                        await self.catchup_page(chat)
                    if chat.done or chat.error or not self.config.included(chat.id):
                        continue
                    pending = True
                    await self.page(chat)
                if time.monotonic() - reconciled >= self.config.reconcile_seconds:
                    for chat in chats:
                        if self.config.included(chat.id):
                            await self.reconcile(chat)
                    reconciled = time.monotonic()
                self.text_idle = True
                await asyncio.sleep(1 if pending else 10)
            except errors.FloodWaitError as error:
                await self.flood(error)

    async def catchup_page(self, chat):
        try:
            await self.history_pause()
            entity = await self.client.get_entity(chat.id)
            lower = None
            if isinstance(entity, types.Channel):
                upper = chat.catchup_cursor
                if not upper:
                    latest = await self.client.get_messages(entity, limit=1)
                    upper = latest[0].id + 1 if latest else chat.catchup_floor + 1
                    await self.history_pause()
                lower = max(chat.catchup_floor + 1, upper - 100)
                messages = await self.client.get_messages(entity, ids=list(range(upper - 1, lower - 1, -1))) if upper > lower else []
            else:
                messages = await self.client.get_messages(entity, limit=100, offset_id=chat.catchup_cursor,
                                                          min_id=chat.catchup_floor)
            for message in messages:
                if message and getattr(message, "date", None):
                    await save_message(self.db, self.config, message, protected=bool(getattr(entity, "noforwards", False)))
            async with self.db.write() as session:
                current = await session.get(Chat, chat.id)
                if lower is not None:
                    current.catchup_cursor = lower
                    current.catchup_pending = lower > chat.catchup_floor + 1
                elif messages:
                    current.catchup_cursor = min(message.id for message in messages)
                else:
                    current.catchup_pending = False
        except (errors.ForbiddenError, errors.BadRequestError, ValueError) as error:
            async with self.db.write() as session:
                current = await session.get(Chat, chat.id)
                current.error = f"Offline catch-up unavailable: {type(error).__name__}"

    async def history_pause(self):
        await self.wait_api()
        # Telegram publishes no universal history quota; conservative pacing plus FLOOD_WAIT.
        if self.history_requests:
            await asyncio.sleep(3)
        self.history_requests += 1

    async def page(self, chat):
        try:
            await self.history_pause()
            entity = await self.client.get_entity(chat.id)
            lower = None
            if isinstance(entity, types.Channel):
                # getHistory can omit old channel content; explicit IDs avoid that message-box limit.
                upper = chat.cursor
                if not upper:
                    latest = await self.client.get_messages(entity, limit=1)
                    upper = latest[0].id + 1 if latest else 1
                    await self.history_pause()
                lower = max(1, upper - 100)
                messages = await self.client.get_messages(entity, ids=list(range(upper - 1, lower - 1, -1))) if upper > 1 else []
            else:
                messages = await self.client.get_messages(entity, limit=100, offset_id=chat.cursor)
            for message in messages:
                if message and getattr(message, "date", None):
                    await save_message(self.db, self.config, message, protected=bool(getattr(entity, "noforwards", False)))
            async with self.db.write() as session:
                current = await session.get(Chat, chat.id)
                if lower is not None:
                    current.cursor, current.done = lower, lower == 1
                elif messages:
                    current.cursor = min(message.id for message in messages)
                else:
                    current.done = True
        except (errors.ForbiddenError, errors.BadRequestError, ValueError) as error:
            if isinstance(error, errors.FloodWaitError):
                raise
            async with self.db.write() as session:
                current = await session.get(Chat, chat.id)
                current.error = f"History unavailable: {type(error).__name__}"

    async def reconcile(self, chat):
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.config.reconcile_days)
        cursor = 0
        try:
            entity = await self.client.get_entity(chat.id)
            while True:
                await self.history_pause()
                page = await self.client.get_messages(entity, limit=100, offset_id=cursor)
                reached_cutoff = False
                for message in page:
                    if not message.date:
                        continue
                    if message.date < cutoff:
                        reached_cutoff = True
                        continue
                    await save_message(self.db, self.config, message, protected=bool(getattr(entity, "noforwards", False)))
                if not page or reached_cutoff:
                    break
                cursor = min(message.id for message in page)
            async with self.db.write() as session:
                current = await session.get(Chat, chat.id)
                current.reconciled = now()
        except (errors.ForbiddenError, errors.BadRequestError, ValueError) as error:
            await self.report(f"reconciliation {chat.id}", error)

    async def attachment_sources(self, attachment_id):
        async with self.db.session() as session:
            query = (select(Message).join(Version, Version.message_id == Message.id)
                     .join(VersionAttachment, VersionAttachment.version_id == Version.id)
                     .where(VersionAttachment.attachment_id == attachment_id).distinct())
            return (await session.scalars(query)).all()

    async def downloads(self):
        while True:
            await self.wait_api()
            async with self.db.session() as session:
                query = select(Attachment).where(Attachment.state.in_(["pending", "error"]), Attachment.retry_at <= time.time())
                unfinished = await session.scalar(select(Chat.id).where(
                    Chat.enabled.is_(True), Chat.error == "",
                    (Chat.done.is_(False) | Chat.catchup_pending.is_(True))).limit(1))
                if not getattr(self, "text_idle", False) or unfinished is not None:
                    query = query.where(Attachment.size <= LARGE_ATTACHMENT_BYTES)
                # Large files come after all eligible smaller files, even from private chats.
                query = query.order_by((Attachment.size > LARGE_ATTACHMENT_BYTES))
                # Positive marked IDs identify private chats, including Saved Messages.
                private_source = (select(VersionAttachment.attachment_id)
                                  .join(Version, Version.id == VersionAttachment.version_id)
                                  .join(Message, Message.id == Version.message_id)
                                  .join(Chat, Chat.id == Message.chat_id)
                                  .where(VersionAttachment.attachment_id == Attachment.id,
                                         Chat.id > 0, Chat.enabled.is_(True)).exists())
                query = query.order_by(private_source.desc())
                # Within each chat category, every tenth item prefers old history.
                if self.download_count % 10:
                    query = query.order_by(Attachment.priority.desc(), Attachment.id)
                else:
                    query = query.order_by(Attachment.id)
                item = await session.scalar(query.limit(1))
            if not item:
                await asyncio.sleep(2)
                continue
            self.download_count += 1
            await self.download(item)

    async def download(self, item):
        sources = await self.attachment_sources(item.id)
        allowed = [m for m in sources if self.config.allows(m.chat_id, item.category, item.size)]
        if not allowed:
            await self.finish(item.id, "skipped")
            return
        extension = PureWindowsPath(item.name).suffix
        if not re.fullmatch(r"\.[A-Za-z0-9_-]{1,32}", extension):
            extension = mimetypes.guess_extension(item.mime) or ".bin"
        await self.finish(item.id, "downloading")
        try:
            found = False
            for source in allowed:
                await self.wait_api()
                try:
                    entity = await self.client.get_entity(source.chat_id)
                    message = await self.client.get_messages(entity, ids=source.telegram_id)
                    info = media_info(message) if message else None
                    if not info or info["media_key"] != item.media_key:
                        continue
                    if getattr(entity, "noforwards", False) or getattr(message, "noforwards", False):
                        continue
                    relative = f"{source.chat_id}/{item.id}{extension}"
                    target = self.config.data_dir / "media" / relative
                    partial = target.with_name(target.name + ".part")
                    await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
                    # Refetching the message also refreshes expired file references.
                    result = await self.client.download_media(message, file=str(partial))
                    if not result:
                        continue
                    if item.size and partial.stat().st_size != item.size:
                        raise OSError("Incomplete attachment download")
                    await asyncio.to_thread(partial.replace, target)
                    async with self.db.write() as session:
                        attachment = await session.get(Attachment, item.id)
                        attachment.path, attachment.state, attachment.error = relative, "complete", ""
                    found = True
                    break
                except (errors.ForbiddenError, errors.BadRequestError, ValueError):
                    continue
            if not found:
                await self.finish(item.id, "unavailable", "Source missing, changed, protected, or inaccessible")
        except errors.FloodWaitError as error:
            await self.flood(error)
            await self.finish(item.id, "error", "Telegram flood wait", self.flood_until)
        except (SQLAlchemyError, OSError) as error:
            if isinstance(error, SQLAlchemyError) or getattr(error, "errno", None) in STORAGE_ERRNOS:
                raise
            await self.finish(item.id, "error", type(error).__name__, time.time() + min(3600, 2 ** min(item.attempts + 2, 12)))
        except Exception as error:
            await self.finish(item.id, "error", type(error).__name__, time.time() + min(3600, 2 ** min(item.attempts + 2, 12)))

    async def finish(self, attachment_id, state, error="", retry_at=0):
        async with self.db.write() as session:
            item = await session.get(Attachment, attachment_id)
            item.state, item.error, item.retry_at = state, error, retry_at
            if state == "error":
                item.attempts += 1
