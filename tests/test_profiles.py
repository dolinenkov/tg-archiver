import asyncio
from datetime import datetime, timezone
from html import unescape
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import func, select
from telethon import errors, types

from tg_archiver.archive import save_message
from tg_archiver.config import Config
from tg_archiver.db import Attachment, Chat, Database, Message, Profile, ProfilePhoto, Sender, Version, VersionAttachment
from tg_archiver.profiles import collect_profiles, queue_profile, refresh_profile, save_photo
from tg_archiver.web import make_app


class ProfilesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.config = Config(data_dir=Path(self.directory.name))
        self.db = Database(self.config.data_dir / 'archive.sqlite3')
        await self.db.initialize()
        self.date = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.entity = types.User(id=42, first_name='Ada', last_name='Lovelace', username='ada', phone='123456',
            access_hash=987654321, photo=types.UserProfilePhoto(photo_id=100, dc_id=2))
        self.photos = [types.Photo(id=100+i, access_hash=123, file_reference=b'secret', date=self.date,
            sizes=[types.PhotoSize(type='x', w=100, h=100, size=5)], dc_id=2) for i in range(23)]
        self.full = SimpleNamespace(about='Bio <script>unsafe</script>', birthday=SimpleNamespace(day=10, month=12, year=None),
                                   common_chats_count=2, profile_photo=self.photos[0])
        self.calls = []
        async def iterate(entity, limit, offset):
            self.calls.append(offset)
            for photo in self.photos[offset:offset+limit]:
                yield photo
        async def download(photo, file, thumb):
            self.assertIs(thumb, photo.sizes[-1])
            Path(file).write_bytes(b'photo')
            return file
        self.telegram = AsyncMock(return_value=SimpleNamespace(full_user=self.full))
        self.telegram.is_connected = Mock(return_value=False)
        self.telegram.get_entity = AsyncMock(return_value=self.entity)
        self.telegram.iter_profile_photos = iterate
        self.telegram.download_media = AsyncMock(side_effect=download)
        self.collector = SimpleNamespace(db=self.db, config=self.config, client=self.telegram,
            wait_api=AsyncMock(), flood=AsyncMock(), flood_until=12345, fatal=None)
        async with self.db.write() as session:
            session.add(Chat(id=42, kind='User', title='Ada'))
            await queue_profile(session, 42, self.entity)

    async def asyncTearDown(self):
        await self.db.close()
        self.directory.cleanup()

    async def profile(self):
        async with self.db.session() as session:
            return await session.get(Profile, 42)

    async def refresh(self):
        with patch('tg_archiver.profiles.asyncio.sleep', new=AsyncMock()):
            await refresh_profile(self.collector, await self.profile())

    async def test_details_and_full_photo_history_are_cached_resumably(self):
        await self.refresh()
        self.assertEqual((await self.profile()).photo_offset, 20)
        await self.refresh()
        profile = await self.profile()
        self.assertEqual(profile.photo_offset, 0)
        self.assertEqual(self.calls, [0, 20])
        details = json.loads(profile.details)
        self.assertEqual(details['Phone number'], '123456')
        self.assertEqual(details['Birthday'], '10/12')
        self.assertEqual(details['Bio / description'], self.full.about)
        self.assertNotIn('access_hash', profile.details)
        self.assertNotIn('987654321', profile.details)
        async with self.db.session() as session:
            photos = (await session.scalars(select(ProfilePhoto))).all()
            self.assertEqual(len(photos), 23)
            self.assertTrue(all((self.config.data_dir/'avatars'/photo.path).is_file() for photo in photos))
        self.assertEqual(self.telegram.download_media.await_count, 23)
        await self.refresh()
        self.assertEqual(self.telegram.download_media.await_count, 23)
        self.assertFalse(list(self.config.data_dir.rglob('*.part.jpg')))

    async def test_missing_photo_redownload_and_removed_current_keeps_history(self):
        await self.refresh()
        async with self.db.session() as session:
            item = await session.scalar(select(ProfilePhoto).where(ProfilePhoto.telegram_id == '100'))
            (self.config.data_dir/'avatars'/item.path).unlink()
        await save_photo(self.collector, 42, self.photos[0])
        self.assertTrue((self.config.data_dir/'avatars'/item.path).exists())
        self.entity.photo = None
        async with self.db.write() as session:
            await queue_profile(session, 42, self.entity)
        self.assertEqual((await self.profile()).photo_id, '')
        async with self.db.session() as session:
            self.assertEqual(await session.scalar(select(func.count()).select_from(ProfilePhoto)), 20)

    async def test_partial_download_cancellation_and_flood_are_not_published(self):
        async def interrupt(photo, file, thumb):
            Path(file).write_bytes(b'partial')
            raise asyncio.CancelledError()
        self.telegram.download_media.side_effect = interrupt
        with self.assertRaises(asyncio.CancelledError):
            await save_photo(self.collector, 42, self.photos[0])
        async with self.db.session() as session:
            self.assertIsNone((await session.scalar(select(ProfilePhoto))).path)
        self.assertFalse(list(self.config.data_dir.rglob('*.part.jpg')))
        self.telegram.download_media.side_effect = errors.FloodWaitError(request=None, capture=30)
        with patch('tg_archiver.profiles.asyncio.sleep', side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await collect_profiles(self.collector)
        self.collector.flood.assert_awaited_once()
        profile = await self.profile()
        self.assertEqual((profile.next_at, profile.error), (12345, 'Telegram flood wait'))
        await self.db.retry()
        self.assertEqual((await self.profile()).next_at, 0)

    async def test_excluded_profiles_are_not_fetched(self):
        self.config.excluded_chats.add(42)
        await self.refresh()
        self.telegram.get_entity.assert_not_awaited()
        await save_photo(self.collector, 42, self.photos[0])
        self.telegram.download_media.assert_not_awaited()

    async def test_one_unavailable_photo_does_not_block_the_rest(self):
        original = self.telegram.download_media.side_effect
        async def unavailable(photo, file, thumb):
            if photo.id == 101:
                return None
            return await original(photo, file, thumb)
        self.telegram.download_media.side_effect = unavailable
        await self.refresh()
        await self.refresh()
        async with self.db.session() as session:
            photos = (await session.scalars(select(ProfilePhoto))).all()
            self.assertEqual(sum(photo.path is not None for photo in photos), 22)
            self.assertEqual(next(photo for photo in photos if photo.telegram_id == '101').error, 'Profile photo unavailable')
        self.assertIn('1 profile photos unavailable', (await self.profile()).error)
        self.telegram.download_media.side_effect = original
        await self.refresh()
        async with self.db.session() as session:
            photo = await session.scalar(select(ProfilePhoto).where(ProfilePhoto.telegram_id == '101'))
            self.assertIsNotNone(photo.path)
            self.assertEqual(photo.error, '')

    async def test_stale_entity_cannot_restore_a_removed_photo(self):
        async def stale(peer_id):
            async with self.db.write() as session:
                profile = await session.get(Profile, peer_id)
                profile.photo_id = ''
            return self.entity
        self.telegram.get_entity.side_effect = stale
        await self.refresh()
        self.assertEqual((await self.profile()).photo_id, '')
        self.telegram.download_media.assert_not_awaited()

    async def test_sender_from_an_excluded_source_is_not_fetched(self):
        message = types.Message(id=1, peer_id=types.PeerChat(7), from_id=types.PeerUser(99), date=self.date, message='Hello')
        await save_message(self.db, self.config, message)
        self.config.excluded_chats.add(-7)
        async with self.db.session() as session:
            profile = await session.get(Profile, 99)
        await refresh_profile(self.collector, profile)
        self.telegram.get_entity.assert_not_awaited()

    async def test_observed_group_senders_get_profiles_without_a_private_chat(self):
        message = types.Message(id=1, peer_id=types.PeerChat(7), from_id=types.PeerUser(99), date=self.date, message='Hello')
        await save_message(self.db, self.config, message)
        async with self.db.session() as session:
            self.assertIsNotNone(await session.get(Profile, 99))
            self.assertIsNone(await session.get(Chat, 99))
        async with TestClient(TestServer(make_app(self.db, self.config, self.collector))) as client:
            page = await (await client.get('/chat/-7')).text()
            self.assertIn('href="/profile/99"', page)
            response = await client.get('/profile/99')
            self.assertEqual(response.status, 200)
            self.assertNotIn('Open conversation →', await response.text())

    async def test_additive_upgrade_backfills_profiles_without_losing_messages(self):
        await save_message(self.db, self.config, types.Message(id=1, peer_id=types.PeerUser(42),
            from_id=types.PeerUser(99), date=self.date, message='Kept'))
        async with self.db.engine.begin() as connection:
            await connection.run_sync(ProfilePhoto.__table__.drop)
            await connection.run_sync(Profile.__table__.drop)
        await self.db.initialize()
        async with self.db.session() as session:
            self.assertEqual(await session.scalar(select(func.count()).select_from(Message)), 1)
            self.assertIsNotNone(await session.get(Profile, 42))
            self.assertIsNotNone(await session.get(Profile, 99))

    async def test_offline_profile_photo_serving_metadata_and_safe_paths(self):
        await self.refresh()
        async with TestClient(TestServer(make_app(self.db, self.config, self.collector))) as client:
            body = await (await client.get('/profile/42')).text()
            self.assertIn('Ada Lovelace', body)
            self.assertIn('123456', body)
            self.assertIn('&lt;script&gt;', body)
            self.assertNotIn('<script>unsafe', body)
            self.assertIn('data-profile-photo', body)
            self.assertEqual(await (await client.get('/avatars?ids=42')).json(), {'42': '100'})
            self.assertEqual(await (await client.get('/avatar/42')).read(), b'photo')
            async with self.db.write() as session:
                photo = await session.scalar(select(ProfilePhoto).where(ProfilePhoto.telegram_id == '100'))
                photo_id = photo.id
                photo.path = '../archive.sqlite3'
            self.assertEqual((await client.get(f'/profile-photo/{photo_id}')).status, 404)
            self.assertEqual((await client.get('/profile/999')).status, 404)
            self.assertEqual((await client.get('/profile/'+'9'*30)).status, 404)
            self.assertEqual((await client.get('/profile-photo/'+'9'*30)).status, 404)
            self.assertEqual((await client.get('/profile/42?tab=invalid')).status, 400)

    async def test_photo_history_has_its_own_scroll_cursor(self):
        path = self.config.data_dir / 'avatars' / 'fixture.jpg'
        path.parent.mkdir()
        path.write_bytes(b'photo')
        async with self.db.write() as session:
            for index in range(51):
                session.add(ProfilePhoto(profile_id=42, telegram_id=str(index), path='fixture.jpg', date=self.date.isoformat()))
        async with TestClient(TestServer(make_app(self.db, self.config, self.collector))) as client:
            body = await (await client.get('/profile/42?tab=files&offset=100')).text()
            self.assertEqual(body.count('data-scroll-key="profile-photo-'), 50)
            url = unescape(re.search('data-scroll-next href="([^"]+)"', body).group(1))
            self.assertIn('photo_offset=50', url)
            self.assertIn('offset=100', url)
            self.assertIn('tab=files', url)
            body = await (await client.get(url)).text()
            self.assertEqual(body.count('data-scroll-key="profile-photo-'), 1)

    async def test_shared_media_is_unique_filtered_and_endlessly_scrollable(self):
        async with self.db.write() as session:
            session.add(Chat(id=-7, kind='Chat', title='Group'))
            session.add(Chat(id=99, kind='User', title='Other'))
            await session.flush()
            for index in range(54):
                message = Message(chat_id=-7 if index < 52 else 99, sender_id=42 if index < 52 else 99,
                    telegram_id=index+1, sent_at=self.date.isoformat())
                session.add(message)
                await session.flush()
                version = Version(message_id=message.id, source_at=self.date.isoformat(), fingerprint=str(index), body='', raw='{}')
                session.add(version)
                await session.flush()
                attachment = Attachment(media_key=str(index), category='files', name=f'file-{index}.pdf', mime='application/pdf',
                    size=3, state='complete' if index != 0 else 'pending', path=f'42/{index}.pdf')
                session.add(attachment)
                await session.flush()
                session.add(VersionAttachment(version_id=version.id, attachment_id=attachment.id))
                if index == 1:
                    old = Version(message_id=message.id, source_at=self.date.isoformat(), fingerprint='older', body='', raw='{}')
                    session.add(old)
                    await session.flush()
                    session.add(VersionAttachment(version_id=old.id, attachment_id=attachment.id))
        async with TestClient(TestServer(make_app(self.db, self.config, self.collector))) as client:
            body = await (await client.get('/profile/42?tab=files')).text()
            self.assertEqual(body.count('data-scroll-key="profile-file-'), 50)
            self.assertNotIn('file-0.pdf', body)
            self.assertNotIn('file-52.pdf', body)
            url = unescape(re.search('data-scroll-next href="([^"]+)"', body).group(1))
            body = await (await client.get(url)).text()
            self.assertEqual(body.count('data-scroll-key="profile-file-'), 1)
            body = await (await client.get('/profile/42?tab=audio')).text()
            self.assertNotIn('data-scroll-key="profile-file-', body)
