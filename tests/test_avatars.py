import asyncio
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

try:
    from aiohttp.test_utils import TestClient, TestServer
    from sqlalchemy import func, select
    from telethon import errors, types
    from tg_archiver.archive import save_message
    from tg_archiver.collector import Collector
    from tg_archiver.config import Config
    from tg_archiver.db import Avatar, Chat, Database, Message
    from tg_archiver.web import make_app
    AVAILABLE = True
except ModuleNotFoundError:
    AVAILABLE = False


@unittest.skipUnless(AVAILABLE, "Install project dependencies to run integration tests")
class AvatarTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.config = Config(data_dir=Path(self.directory.name))
        self.db = Database(self.config.data_dir / "archive.sqlite3")
        await self.db.initialize()
        self.entity = types.User(id=42, first_name="Ada", photo=types.UserProfilePhoto(photo_id=123, dc_id=2))
        self.entities = {42: self.entity}
        self.collector = Collector.__new__(Collector)
        self.collector.db, self.collector.config = self.db, self.config
        self.collector.wait_api = AsyncMock()
        self.collector.flood_until = 0
        async def dialogs():
            for chat_id, entity in self.entities.items():
                yield SimpleNamespace(id=chat_id, name="Ada" if chat_id == 42 else "Other chat", entity=entity)
        async def download(entity, file, download_big):
            Path(file).write_bytes(b"\xff\xd8avatar\xff\xd9")
            return file
        self.collector.client = SimpleNamespace(
            iter_dialogs=dialogs, get_entity=AsyncMock(side_effect=lambda chat_id: self.entities[chat_id]),
            download_profile_photo=AsyncMock(side_effect=download))

    async def asyncTearDown(self):
        await self.db.close()
        self.directory.cleanup()

    async def item(self, chat_id=42):
        async with self.db.session() as session:
            return await session.get(Avatar, chat_id)

    async def test_discovery_queues_small_avatars_and_reuses_cached_photos(self):
        await self.collector.discover()
        item = await self.item()
        self.assertEqual(item.photo_id, "123")
        self.assertIsNone(item.path)
        self.collector.client.download_profile_photo.assert_not_awaited()
        await self.collector.download_avatar(item)
        saved = await self.item()
        self.assertEqual(saved.path, "42.jpg")
        self.assertEqual(saved.downloaded_photo_id, "123")
        self.assertEqual((self.config.data_dir / "avatars" / saved.path).read_bytes(), b"\xff\xd8avatar\xff\xd9")
        self.assertFalse((self.config.data_dir / "avatars" / "42.part.jpg").exists())
        call = self.collector.client.download_profile_photo.await_args
        self.assertFalse(call.kwargs["download_big"])
        await self.collector.discover()
        with patch("tg_archiver.collector.asyncio.sleep", side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await self.collector.avatars()
        self.collector.client.download_profile_photo.assert_awaited_once()

    async def test_changed_and_removed_photos_refresh_current_cache(self):
        await self.collector.discover()
        await self.collector.download_avatar(await self.item())
        self.entity.photo.photo_id = 456
        await self.collector.discover()
        item = await self.item()
        self.assertEqual(item.photo_id, "456")
        self.assertEqual(item.downloaded_photo_id, "123")
        self.assertEqual(item.path, "42.jpg")
        await self.collector.download_avatar(item)
        self.assertEqual((await self.item()).downloaded_photo_id, "456")
        self.entity.photo = None
        await self.collector.discover()
        item = await self.item()
        self.assertEqual(item.photo_id, "")
        self.assertIsNone(item.path)
        self.assertEqual(item.downloaded_photo_id, "")

    async def test_missing_cached_file_is_queued_again(self):
        await self.collector.discover()
        await self.collector.download_avatar(await self.item())
        (self.config.data_dir / "avatars" / "42.jpg").unlink()
        await self.collector.discover()
        item = await self.item()
        self.assertIsNone(item.path)
        await self.collector.download_avatar(item)
        self.assertEqual((await self.item()).downloaded_photo_id, "123")
        self.assertTrue((self.config.data_dir / "avatars" / "42.jpg").is_file())

    async def test_avatar_worker_prioritizes_private_chats_and_respects_exclusions(self):
        self.entities[-7] = types.Chat(id=7, title="Group", photo=types.ChatPhoto(photo_id=789, dc_id=2),
                                      participants_count=2, date=datetime.now(timezone.utc), version=1)
        self.entities[99] = types.User(id=99, first_name="Excluded", photo=types.UserProfilePhoto(photo_id=999, dc_id=2))
        self.config.excluded_chats.add(99)
        await self.collector.discover()
        self.assertIsNone(await self.item(99))
        self.collector.download_avatar = AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            await self.collector.avatars()
        self.assertEqual(self.collector.download_avatar.await_args.args[0].chat_id, 42)
        self.config.excluded_chats.add(42)
        with patch("tg_archiver.collector.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(asyncio.CancelledError):
                await self.collector.avatars()
        self.assertEqual(self.collector.download_avatar.await_args.args[0].chat_id, -7)

    async def test_flood_wait_and_retry_preserve_old_photo(self):
        await self.collector.discover()
        await self.collector.download_avatar(await self.item())
        target = self.config.data_dir / "avatars" / "42.jpg"
        original = target.read_bytes()
        self.entity.photo.photo_id = 456
        await self.collector.discover()
        async def flood(entity, file, download_big):
            Path(file).write_bytes(b"partial")
            raise errors.FloodWaitError(request=None, capture=20)
        self.collector.client.download_profile_photo.side_effect = flood
        await self.collector.download_avatar(await self.item())
        item = await self.item()
        self.assertEqual(item.downloaded_photo_id, "123")
        self.assertEqual(item.error, "Telegram flood wait")
        self.assertEqual(item.attempts, 1)
        self.assertEqual(item.retry_at, self.collector.flood_until)
        self.assertEqual(target.read_bytes(), original)
        self.assertFalse(target.with_name("42.part.jpg").exists())
        await self.db.retry()
        item = await self.item()
        self.assertEqual((item.retry_at, item.attempts, item.error), (0, 0, ""))

    async def test_unavailable_and_interrupted_downloads_do_not_publish_partial_photos(self):
        await self.collector.discover()
        item = await self.item()
        self.collector.client.download_profile_photo.side_effect = None
        self.collector.client.download_profile_photo.return_value = None
        await self.collector.download_avatar(item)
        saved = await self.item()
        self.assertIsNone(saved.path)
        self.assertGreater(saved.retry_at, datetime.now(timezone.utc).timestamp())
        async def interrupted(entity, file, download_big):
            Path(file).write_bytes(b"partial")
            raise asyncio.CancelledError
        self.collector.client.download_profile_photo.side_effect = interrupted
        with self.assertRaises(asyncio.CancelledError):
            await self.collector.download_avatar(item)
        self.assertIsNone((await self.item()).path)
        self.assertFalse((self.config.data_dir / "avatars" / "42.part.jpg").exists())

    async def test_photo_removed_during_download_is_not_resurrected(self):
        await self.collector.discover()
        async def removed(entity, file, download_big):
            Path(file).write_bytes(b"photo")
            self.entity.photo = None
            await self.collector.discover()
            return file
        self.collector.client.download_profile_photo.side_effect = removed
        await self.collector.download_avatar(await self.item())
        self.assertIsNone((await self.item()).path)
        self.assertFalse((self.config.data_dir / "avatars" / "42.jpg").exists())

    async def test_existing_schema_adds_avatar_table_without_changing_messages(self):
        await save_message(self.db, self.config, types.Message(
            id=1, peer_id=types.PeerUser(42), date=datetime.now(timezone.utc), message="kept"))
        async with self.db.engine.begin() as connection:
            await connection.run_sync(Avatar.__table__.drop)
        await self.db.initialize()
        async with self.db.session() as session:
            self.assertEqual(await session.scalar(select(func.count()).select_from(Message)), 1)
            self.assertEqual(await session.scalar(select(func.count()).select_from(Avatar)), 0)

    async def test_stale_entity_lookup_does_not_requeue_a_removed_photo(self):
        await self.collector.discover()
        old_entity = types.User(id=42, first_name="Ada", photo=types.UserProfilePhoto(photo_id=123, dc_id=2))
        async def stale_lookup(chat_id):
            self.entity.photo = None
            await self.collector.discover()
            return old_entity
        self.collector.client.get_entity.side_effect = stale_lookup
        await self.collector.download_avatar(await self.item())
        self.assertEqual((await self.item()).photo_id, "")
        self.collector.client.download_profile_photo.assert_not_awaited()

    async def test_web_serves_only_cached_photos_and_keeps_initials_fallback(self):
        await self.collector.discover()
        await self.collector.download_avatar(await self.item())
        async with self.db.write() as session:
            session.add(Chat(id=-7, kind="Chat", title="Group"))
        async with TestClient(TestServer(make_app(self.db, self.config, SimpleNamespace(fatal=None)))) as client:
            response = await client.get('/avatar/42?v=123')
            self.assertEqual(response.status, 200)
            self.assertEqual(response.content_type, "image/jpeg")
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertEqual(await response.read(), b"\xff\xd8avatar\xff\xd9")
            body = await (await client.get('/chat/42')).text()
            self.assertEqual(body.count('src="/avatar/42?v=123"'), 2)
            self.assertIn('data-avatar-chat="-7" data-avatar-version=""', body)
            self.assertEqual(await (await client.get('/avatars?ids=42,-7,999')).json(), {"42": "123", "-7": "", "999": ""})
            for path in ['/avatars?ids=abc', '/avatars?ids=' + ','.join(str(i) for i in range(101)), '/avatars?ids=' + '9' * 30]:
                self.assertEqual((await client.get(path)).status, 400)
            self.assertEqual((await client.get('/avatar/' + '9' * 30)).status, 404)
            async with self.db.write() as session:
                (await session.get(Avatar, 42)).path = "../archive.sqlite3"
            self.assertEqual((await client.get('/avatar/42')).status, 404)
            self.assertEqual(await (await client.get('/avatars?ids=42')).json(), {"42": ""})
            body = await (await client.get('/')).text()
            self.assertNotIn('src="/avatar/', body)


if __name__ == "__main__":
    unittest.main()
