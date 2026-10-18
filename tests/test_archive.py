from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

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
            target = self.config.data_dir / "media" / item.path
            self.assertEqual(target.read_bytes(), b"123")
            self.assertFalse(target.with_suffix(".part").exists())
        async def flood_download(message, file):
            raise errors.FloodWaitError(request=None, capture=20)
        collector.client.download_media = flood_download
        await collector.download(item)
        async with self.db.session() as session:
            item = await session.get(Attachment, item.id)
            self.assertEqual(item.state, "error")
            self.assertGreater(item.retry_at, datetime.now(timezone.utc).timestamp())
            self.assertEqual(target.read_bytes(), b"123")

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
            async with self.db.write() as session:
                item = Attachment(media_key="test", category="files", name="bad.html", mime="text/html",
                                  size=1, state="complete", path="../../archive.sqlite3")
                session.add(item)
                await session.flush()
                item_id = item.id
            response = await client.get(f'/attachment/{item_id}?inline=1')
            self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
