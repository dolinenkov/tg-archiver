import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

try:
    from sqlalchemy import func, select, text
    from telethon import errors, types
    from tg_archiver.archive import save_deletions, save_message
    from tg_archiver.collector import Collector
    from tg_archiver.config import Config
    from tg_archiver.db import Attachment, Chat, Database, Message, Version
    from tg_archiver.web import make_app
    from aiohttp.test_utils import TestClient, TestServer
    AVAILABLE = True
except ModuleNotFoundError:
    AVAILABLE = False


@unittest.skipUnless(AVAILABLE, "Install project dependencies to run integration tests")
class ArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.config = Config(data_dir=Path(self.directory.name))
        self.db = Database(self.config.data_dir / "archive.sqlite3")
        await self.db.initialize()
        self.date = datetime(2026, 1, 1, tzinfo=timezone.utc)

    async def asyncTearDown(self):
        await self.db.close()
        self.directory.cleanup()

    def message(self, body, edited=0, message_id=1, peer=None, media=None):
        return types.Message(id=message_id, peer_id=peer or types.PeerUser(42), date=self.date,
                             message=body, edit_date=self.date + timedelta(seconds=edited) if edited else None,
                             media=media)

    async def test_versions_replay_order_and_deletion(self):
        await save_message(self.db, self.config, self.message("original"))
        await save_message(self.db, self.config, self.message("изменено", 20), live=True)
        await save_message(self.db, self.config, self.message("изменено", 20))
        await save_message(self.db, self.config, self.message("older edit", 10))
        await save_deletions(self.db, [1])
        async with self.db.session() as session:
            self.assertEqual(await session.scalar(select(func.count()).select_from(Version)), 3)
            message = await session.scalar(select(Message))
            latest = await session.get(Version, message.latest_id)
            self.assertEqual(latest.body, "изменено")
            self.assertIsNotNone(message.deleted_at)
            result = await session.execute(text("SELECT rowid FROM search WHERE search MATCH :q"), {"q": "ИЗМЕНЕНО"})
            self.assertEqual(len(result.all()), 1)

    async def test_unchanged_newer_observation_advances_watermark(self):
        await save_message(self.db, self.config, self.message("A"))
        await save_message(self.db, self.config, self.message("A", 30))
        await save_message(self.db, self.config, self.message("B", 20))
        async with self.db.session() as session:
            message = await session.scalar(select(Message))
            self.assertEqual((await session.get(Version, message.latest_id)).body, "A")

    async def test_edited_events_preserve_reverts_and_history_view_opens_selected_version(self):
        collector = Collector.__new__(Collector)
        collector.db, collector.config, collector.fatal, collector.verified = self.db, self.config, None, True
        collector.ready = asyncio.Event()
        collector.ready.set()
        for body, edited in [('Original', 0), ('Changed', 20), ('Original', 40), ('Late older edit', 10), ('Original', 40)]:
            await collector.on_message(SimpleNamespace(chat_id=42, chat=None, message=self.message(body, edited)))
        async with self.db.session() as session:
            message = await session.scalar(select(Message))
            versions = (await session.scalars(select(Version).order_by(Version.source_at.desc()))).all()
            self.assertEqual(len(versions), 4)
            self.assertEqual((await session.get(Version, message.latest_id)).body, 'Original')
            selected = next(version for version in versions if version.body == 'Changed')
        async with TestClient(TestServer(make_app(self.db, self.config, SimpleNamespace(fatal=None)))) as client:
            response = await client.get(f'/message/42/1?version={selected.id}')
            body = await response.text()
            self.assertIn('Version history · 4 saved versions', body)
            self.assertIn('class="message-history" open', body)
            self.assertIn(f'data-version-id="{selected.id}"', body)
            self.assertIn('selected-version', body)
            self.assertLess(body.index('Changed'), body.index('Late older edit'))

    async def test_deletion_ids_do_not_cross_channel_boundaries(self):
        await save_message(self.db, self.config, self.message("private"))
        await save_message(self.db, self.config, self.message("channel", peer=types.PeerChannel(77)))
        await save_deletions(self.db, [1])
        async with self.db.session() as session:
            channel = await session.scalar(select(Message).where(Message.chat_id == -1000000000077))
            self.assertIsNone(channel.deleted_at)

    async def test_exclusion_never_erases_saved_content(self):
        await save_message(self.db, self.config, self.message("kept"))
        self.config.excluded_chats.add(42)
        await save_message(self.db, self.config, self.message("ignored", message_id=2))
        async with self.db.session() as session:
            self.assertEqual(await session.scalar(select(func.count()).select_from(Message)), 1)

    def photo(self):
        photo = types.Photo(id=123, access_hash=456, file_reference=b"ref", date=self.date,
                            sizes=[types.PhotoSize(type="x", w=10, h=10, size=3)], dc_id=2)
        return types.MessageMediaPhoto(photo=photo)

    async def test_disabled_media_reenable_and_interrupted_download(self):
        self.config.media["photos"] = False
        message = self.message("photo", media=self.photo())
        await save_message(self.db, self.config, message)
        async with self.db.session() as session:
            self.assertEqual((await session.scalar(select(Attachment))).state, "skipped")
        self.config.media["photos"] = True
        await save_message(self.db, self.config, message)
        async with self.db.write() as session:
            item = await session.scalar(select(Attachment))
            self.assertEqual(item.state, "pending")
            item.state = "downloading"
        await self.db.close()
        self.db = Database(self.config.data_dir / "archive.sqlite3")
        await self.db.initialize()
        async with self.db.session() as session:
            self.assertEqual((await session.scalar(select(Attachment))).state, "pending")
            self.assertEqual(await session.scalar(select(func.count()).select_from(Version)), 1)

    async def test_protected_media_is_not_queued(self):
        await save_message(self.db, self.config, self.message("protected", media=self.photo()), protected=True)
        async with self.db.session() as session:
            self.assertEqual((await session.scalar(select(Attachment))).state, "unavailable")

    async def test_history_checkpoint_and_config_change(self):
        # No account or network: exercise the real page/discovery logic with a fake API boundary.
        collector = Collector.__new__(Collector)
        collector.db, collector.config = self.db, self.config
        collector.history_requests = 0
        async def no_wait():
            pass
        collector.history_pause = no_wait
        entity = types.User(id=42, first_name="Example")
        async def dialogs():
            yield SimpleNamespace(id=42, entity=entity, name="Example")
        async def get_entity(chat_id):
            return entity
        calls = []
        async def get_messages(entity, limit, offset_id):
            calls.append(offset_id)
            return [self.message("history", message_id=5)] if offset_id == 0 else []
        collector.client = SimpleNamespace(iter_dialogs=dialogs, get_entity=get_entity, get_messages=get_messages)
        await collector.discover()
        async with self.db.session() as session:
            chat = await session.get(Chat, 42)
        await collector.page(chat)
        async with self.db.session() as session:
            chat = await session.get(Chat, 42)
            self.assertEqual(chat.cursor, 5)
        await collector.page(chat)
        async with self.db.session() as session:
            self.assertTrue((await session.get(Chat, 42)).done)
        self.config.media["photos"] = False
        await collector.discover()
        async with self.db.session() as session:
            chat = await session.get(Chat, 42)
            self.assertFalse(chat.done)
            self.assertEqual(chat.cursor, 0)
        self.assertEqual(calls, [0, 5])

    async def test_channel_scan_advances_through_empty_id_ranges(self):
        collector = Collector.__new__(Collector)
        collector.db, collector.config = self.db, self.config
        async def no_wait():
            pass
        collector.history_pause = no_wait
        entity = types.Channel(id=77, title="Channel", photo=types.ChatPhotoEmpty(), date=self.date)
        async def get_entity(chat_id):
            return entity
        ranges = []
        async def get_messages(entity, limit=None, ids=None):
            if ids is None:
                return [self.message("newest", message_id=205, peer=types.PeerChannel(77))]
            ranges.append(ids)
            return [None] * len(ids)
        collector.client = SimpleNamespace(get_entity=get_entity, get_messages=get_messages)
        chat_id = -1000000000077
        async with self.db.write() as session:
            session.add(Chat(id=chat_id, kind="Channel", title="Channel"))
        for _ in range(3):
            async with self.db.session() as session:
                chat = await session.get(Chat, chat_id)
            await collector.page(chat)
        async with self.db.session() as session:
            chat = await session.get(Chat, chat_id)
            self.assertTrue(chat.done)
            self.assertEqual(chat.cursor, 1)
        self.assertEqual(ranges[0], list(range(205, 105, -1)))
        self.assertEqual(ranges[-1], [5, 4, 3, 2, 1])

    async def test_private_chats_lead_each_history_pass(self):
        async with self.db.write() as session:
            for chat_id, kind in [(-1000000000077, "Channel"), (-7, "Chat"), (42, "User"), (99, "User")]:
                session.add(Chat(id=chat_id, kind=kind, title=str(chat_id), catchup_pending=True))
            session.add(Chat(id=1, kind="User", title="Excluded", enabled=False))
        collector = Collector.__new__(Collector)
        collector.db, collector.config = self.db, self.config
        collector.wait_api = AsyncMock()
        collector.discover = AsyncMock()
        collector.catchup_page = AsyncMock()
        collector.page = AsyncMock()
        collector.reconcile = AsyncMock()
        with patch("tg_archiver.collector.asyncio.sleep", side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await collector.history()
        for operation in (collector.catchup_page, collector.page, collector.reconcile):
            self.assertEqual([call.args[0].id for call in operation.await_args_list],
                             [42, 99, -1000000000077, -7])

    async def test_private_attachments_outrank_group_live_and_old_downloads(self):
        for photo_id, peer, live in [(123, types.PeerChat(7), False),
                                     (456, types.PeerUser(42), False),
                                     (789, types.PeerChannel(77), True)]:
            media = self.photo()
            media.photo.id = photo_id
            await save_message(self.db, self.config, self.message("photo", peer=peer, media=media), live=live)
        collector = Collector.__new__(Collector)
        collector.db, collector.config = self.db, self.config
        collector.wait_api = AsyncMock()
        collector.download = AsyncMock(side_effect=asyncio.CancelledError)
        for count in (0, 1):
            collector.download_count = count
            with self.assertRaises(asyncio.CancelledError):
                await collector.downloads()
            item = collector.download.await_args.args[0]
            sources = await collector.attachment_sources(item.id)
            self.assertEqual([source.chat_id for source in sources], [42])
        await collector.finish(item.id, "complete")
        for count, expected_chat in [(0, -7), (1, -1000000000077)]:
            collector.download_count = count
            with self.assertRaises(asyncio.CancelledError):
                await collector.downloads()
            sources = await collector.attachment_sources(collector.download.await_args.args[0].id)
            self.assertEqual([source.chat_id for source in sources], [expected_chat])

    async def test_attachment_atomic_completion_and_flood_retry(self):
        await save_message(self.db, self.config, self.message("photo", media=self.photo()))
        collector = Collector.__new__(Collector)
        collector.db, collector.config = self.db, self.config
        collector.flood_until = 0
        async def no_wait():
            pass
        collector.wait_api = no_wait
        async def get_entity(chat_id):
            return types.User(id=42, first_name="Example")
        async def get_messages(entity, ids):
            return self.message("photo", media=self.photo())
        async def download_media(message, file):
            Path(file).write_bytes(b"123")
            return file
        collector.client = SimpleNamespace(get_entity=get_entity, get_messages=get_messages, download_media=download_media)
        async with self.db.session() as session:
            item = await session.scalar(select(Attachment))
        await collector.download(item)
        async with self.db.session() as session:
            item = await session.get(Attachment, item.id)
            self.assertEqual(item.state, "complete")
            self.assertEqual(item.path, f"42/{item.id}.jpg")
            target = self.config.data_dir / "media" / item.path
            self.assertEqual(target.read_bytes(), b"123")
            self.assertFalse(target.with_name(target.name + ".part").exists())
        async def flood_download(message, file):
            raise errors.FloodWaitError(request=None, capture=20)
        collector.client.download_media = flood_download
        await collector.download(item)
        async with self.db.session() as session:
            item = await session.get(Attachment, item.id)
            self.assertEqual(item.state, "error")
            self.assertGreater(item.retry_at, datetime.now(timezone.utc).timestamp())
            self.assertEqual(target.read_bytes(), b"123")

    async def test_download_preserves_extensions_in_source_chat_folder(self):
        collector = Collector.__new__(Collector)
        collector.db, collector.config = self.db, self.config
        collector.wait_api = AsyncMock()
        for index, (name, mime, extension) in enumerate([
            ("report.PDF", "application/pdf", ".PDF"),
            (r"C:\private\archive.tar.gz", "application/gzip", ".gz"),
            ("no-extension", "image/png", ".png"),
            ("bad.unsafe:extension", "application/octet-stream", ".bin"),
            ("original.part", "application/octet-stream", ".part"),
        ], start=1):
            with self.subTest(name=name):
                message = self.message("document", message_id=index, peer=types.PeerChat(7),
                                       media=types.MessageMediaDocument(document=types.Document(
                                           id=index, access_hash=456, file_reference=b"ref", date=self.date,
                                           mime_type=mime, size=3, dc_id=2,
                                           attributes=[types.DocumentAttributeFilename(file_name=name)])))
                await save_message(self.db, self.config, message)
                collector.client = SimpleNamespace(
                    get_entity=AsyncMock(return_value=types.PeerChat(7)),
                    get_messages=AsyncMock(return_value=message))
                async def download_media(message, file):
                    Path(file).write_bytes(b"123")
                    return file
                collector.client.download_media = download_media
                async with self.db.session() as session:
                    item = await session.scalar(select(Attachment).where(Attachment.name == name))
                await collector.download(item)
                async with self.db.session() as session:
                    saved = await session.get(Attachment, item.id)
                    self.assertEqual(saved.state, "complete")
                    self.assertEqual(saved.path, f"-7/{item.id}{extension}")
                    target = self.config.data_dir / "media" / saved.path
                    self.assertEqual(target.read_bytes(), b"123")
                    self.assertFalse(target.with_name(target.name + ".part").exists())

    async def test_large_downloads_wait_for_text_and_smaller_files(self):
        await save_message(self.db, self.config, self.message("large private", media=self.photo()), live=True)
        media = self.photo()
        media.photo.id = 456
        await save_message(self.db, self.config, self.message("small group", peer=types.PeerChat(7), media=media))
        async with self.db.write() as session:
            large = await session.scalar(select(Attachment).where(Attachment.media_key == "Photo:123"))
            small = await session.scalar(select(Attachment).where(Attachment.media_key == "Photo:456"))
            large.size, small.size = 10 * 1024 * 1024 + 1, 10 * 1024 * 1024
            large_id, small_id = large.id, small.id
            for chat in (await session.scalars(select(Chat))).all():
                chat.done = True
        collector = Collector.__new__(Collector)
        collector.db, collector.config = self.db, self.config
        collector.wait_api = AsyncMock()
        collector.download = AsyncMock(side_effect=asyncio.CancelledError)
        collector.text_idle = True
        for count in (0, 1):
            collector.download_count = count
            with self.assertRaises(asyncio.CancelledError):
                await collector.downloads()
            self.assertEqual(collector.download.await_args.args[0].id, small_id)
        await collector.finish(small_id, "complete")
        for idle, done, catchup, error, enabled, expected in [
            (False, True, False, "", True, None),
            (True, False, False, "", True, None),
            (True, True, True, "", True, None),
            (True, True, False, "", True, large_id),
            (True, False, True, "Unavailable history", True, large_id),
            (True, False, True, "", False, large_id),
        ]:
            with self.subTest(idle=idle, done=done, catchup=catchup, error=error, enabled=enabled):
                collector.text_idle = idle
                collector.download.reset_mock()
                async with self.db.write() as session:
                    chat = await session.get(Chat, 42)
                    chat.done, chat.catchup_pending, chat.error, chat.enabled = done, catchup, error, enabled
                with patch("tg_archiver.collector.asyncio.sleep", side_effect=asyncio.CancelledError):
                    with self.assertRaises(asyncio.CancelledError):
                        await collector.downloads()
                if expected is None:
                    collector.download.assert_not_awaited()
                else:
                    self.assertEqual(collector.download.await_args.args[0].id, expected)

    async def test_web_chats_recent_first_and_messages_chronological_across_batches(self):
        for index in range(1, 54):
            message = self.message(f"message {index}", message_id=index)
            message.date = self.date + timedelta(seconds=min(index, 52))
            await save_message(self.db, self.config, message)
        message = self.message("newer chat", peer=types.PeerUser(99))
        message.date = self.date + timedelta(days=1)
        await save_message(self.db, self.config, message)
        async with self.db.write() as session:
            (await session.get(Chat, 42)).title = "Alphabetically first"
            (await session.get(Chat, 99)).title = "Z newest"
            session.add(Chat(id=100, kind="User", title="Empty chat"))
        collector = SimpleNamespace(fatal=None)
        async with TestClient(TestServer(make_app(self.db, self.config, collector))) as client:
            body = await (await client.get('/')).text()
            self.assertLess(body.index('href="/chat/99"'), body.index('href="/chat/42"'))
            self.assertLess(body.index('href="/chat/42"'), body.index('href="/chat/100"'))
            for path, expected in [('/chat/42', list(range(4, 54))),
                                   ('/chat/42?offset=50', [1, 2, 3]),
                                   ('/message/42/51', list(range(48, 54)))]:
                body = await (await client.get(path)).text()
                self.assertEqual([int(value) for value in re.findall(r'<article id="m(\d+)"', body)], expected)
                if path.startswith('/chat'):
                    self.assertIn('data-scroll-start="latest"', body)
                    self.assertLess(body.index('<div class="scroll-footer">', body.index('data-scroll-region="messages"')),
                                    body.index('class="message-list"'))
                else:
                    self.assertIn('data-scroll-target="m51"', body)

    async def test_conversation_order_uses_timestamp_before_message_id(self):
        for message_id, seconds in [(700, 1), (10, 3), (400, 2)]:
            message = self.message(str(message_id), message_id=message_id)
            message.date = self.date + timedelta(seconds=seconds)
            await save_message(self.db, self.config, message)
        async with TestClient(TestServer(make_app(self.db, self.config, SimpleNamespace(fatal=None)))) as client:
            body = await (await client.get('/chat/42')).text()
            self.assertEqual([int(value) for value in re.findall(r'<article id="m(\d+)"', body)], [700, 400, 10])

    async def test_web_escaping_search_and_download_path(self):
        await save_message(self.db, self.config, self.message('<script>alert(1)</script> Привет hello'))
        collector = SimpleNamespace(fatal=None, client=SimpleNamespace(is_connected=lambda: False))
        async with TestClient(TestServer(make_app(self.db, self.config, collector))) as client:
            response = await client.get('/chat/42')
            body = await response.text()
            self.assertEqual(response.status, 200)
            self.assertIn('&lt;script&gt;', body)
            self.assertNotIn('<script>', body)
            response = await client.get('/search', params={"q": "ПРИВЕТ"})
            self.assertEqual(response.status, 200)
            self.assertIn('hello', await response.text())
            response = await client.get('/search', params={"q": '"'})
            self.assertEqual(response.status, 400)
            response = await client.get('/search', params={"q": "hello", "from": "invalid"})
            self.assertEqual(response.status, 400)
            legacy = self.config.data_dir / "media" / "0" / "legacy.bin"
            legacy.parent.mkdir(parents=True)
            legacy.write_bytes(b"legacy")
            async with self.db.write() as session:
                item = Attachment(media_key="legacy", category="files", name="legacy.txt", mime="text/plain",
                                  size=6, state="complete", path="0/legacy.bin")
                session.add(item)
                await session.flush()
                item_id = item.id
            response = await client.get(f'/attachment/{item_id}')
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.read(), b"legacy")
            async with self.db.write() as session:
                item = Attachment(media_key="test", category="files", name="bad.html", mime="text/html",
                                  size=1, state="complete", path="../../archive.sqlite3")
                session.add(item)
                await session.flush()
                item_id = item.id
            response = await client.get(f'/attachment/{item_id}?inline=1')
            self.assertEqual(response.status, 404)


    async def test_scroll_continuations_keep_search_filters_and_stop_at_end(self):
        for index in range(1, 54):
            await save_message(self.db, self.config, self.message("needle", message_id=index))
        async with self.db.write() as session:
            for index in range(100, 152):
                session.add(Chat(id=index, kind="User", title=f"Chat {index}"))
        collector = SimpleNamespace(fatal=None)
        from html import unescape
        async with TestClient(TestServer(make_app(self.db, self.config, collector))) as client:
            script = await client.get('/static/app.js')
            self.assertEqual(script.status, 200)
            self.assertIn(script.content_type, {"application/javascript", "text/javascript"})
            for path in ['/', '/chat/42', '/search?q=needle&chat=42&from=2026-01-01&to=2026-01-01']:
                response = await client.get(path)
                body = await response.text()
                region = 'search' if path.startswith('/search') else ('messages' if path.startswith('/chat') else 'chats')
                body = re.search(rf'data-scroll-region="{region}"(.*?)(?:</aside>|</main>)', body, re.S).group(1)
                self.assertIn("script-src 'self'", response.headers['Content-Security-Policy'])
                self.assertIn("connect-src 'self'", response.headers['Content-Security-Policy'])
                self.assertIn('data-scroll-list', body)
                self.assertNotIn('aria-label="Pagination"', body)
                self.assertEqual(len(re.findall('data-scroll-key=', body)), 50)
                continuation = unescape(re.search(r'data-scroll-next href="([^"]+)"', body).group(1))
                if path.startswith('/search'):
                    for parameter in ('q=needle', 'chat=42', 'from=2026-01-01', 'to=2026-01-01'):
                        self.assertIn(parameter, continuation)
                body = await (await client.get(continuation)).text()
                body = re.search(rf'data-scroll-region="{region}"(.*?)(?:</aside>|</main>)', body, re.S).group(1)
                self.assertEqual(len(re.findall('data-scroll-key=', body)), 3)
                self.assertNotIn('data-scroll-next', body)
                self.assertIn('Beginning of conversation.' if region == 'messages' else 'All items loaded.', body)

    async def test_ui_exposes_utc_instants_for_browser_time_formatting(self):
        await save_message(self.db, self.config, self.message("original"))
        await save_message(self.db, self.config, self.message("edited", 20))
        await self.db.set_status("discovered", self.date.isoformat())
        await self.db.set_status("last_error", self.date.isoformat() + " ExampleError")
        async with self.db.write() as session:
            (await session.get(Chat, 42)).reconciled = self.date.isoformat()
        async with TestClient(TestServer(make_app(self.db, self.config, SimpleNamespace(fatal=None)))) as client:
            for path in ('/chat/42', '/search?q=edited', '/status'):
                body = await (await client.get(path)).text()
                self.assertIn('datetime="2026-01-01T00:00:00+00:00"', body)
                self.assertIn('data-local-time="full"', body)
            body = await (await client.get('/chat/42')).text()
            self.assertIn('data-sent-at="2026-01-01T00:00:00+00:00"', body)
            self.assertIn('data-source-at="2026-01-01T00:00:20+00:00"', body)
            self.assertIn('data-local-time="short"', body)
            self.assertIn('data-browser-zone', body)

    async def test_jinja_layout_escapes_metadata_and_marks_own_messages(self):
        from tg_archiver.db import Setting
        message = self.message('hello <script>alert(1)</script> https://example.com/?a=1&b=2')
        message.from_id = types.PeerUser(42)
        await save_message(self.db, self.config, message)
        async with self.db.write() as session:
            session.add(Setting(key="account_id", value="42"))
            (await session.get(Chat, 42)).title = '<img src=x onerror=alert(1)>'
        async with TestClient(TestServer(make_app(self.db, self.config, SimpleNamespace(fatal=None)))) as client:
            response = await client.get('/chat/42')
            body = await response.text()
            self.assertEqual(response.status, 200)
            self.assertIn('class="message outgoing"', body)
            self.assertIn('&lt;img src=x onerror=alert(1)&gt;', body)
            self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', body)
            self.assertNotIn('<img src=x', body)
            self.assertIn('href="https://example.com/?a=1&amp;b=2"', body)
            self.assertIn('data-scroll-region="chats"', body)
            self.assertIn('data-scroll-region="messages"', body)
            self.assertIn('Read-only archive', body)
            self.assertNotIn("unsafe-inline", response.headers['Content-Security-Policy'])
            self.assertEqual((await client.get('/static/app.css')).status, 200)
            self.assertEqual((await client.get('/static/icon.svg')).status, 200)
            self.assertEqual((await client.get('/status')).status, 200)


if __name__ == "__main__":
    unittest.main()
