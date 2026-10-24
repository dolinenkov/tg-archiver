import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import func, select, text
from telethon import errors, types

from tg_archiver.archive import save_message
from tg_archiver.config import Config
from tg_archiver.db import Attachment, Chat, Database, Message, Transcription, Version
from tg_archiver.transcription import collect_transcriptions, save_update, store_result, transcribe
from tg_archiver.web import make_app


class TranscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.config = Config(data_dir=Path(self.directory.name))
        self.db = Database(self.config.data_dir/'archive.sqlite3')
        await self.db.initialize()
        self.date = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.message = self.recording()
        self.result = types.messages.TranscribedAudio(transcription_id=999, text='Recognized привет world')
        self.client = AsyncMock(return_value=self.result)
        self.client.is_connected = Mock(return_value=False)
        self.client.get_entity = AsyncMock(return_value=types.User(id=42, first_name='Example'))
        self.client.get_messages = AsyncMock(return_value=self.message)
        self.collector = SimpleNamespace(db=self.db, config=self.config, client=self.client,
            wait_api=AsyncMock(), flood=AsyncMock(), flood_until=12345, fatal=None)
        await save_message(self.db, self.config, self.message)

    def recording(self, media_id=123, category='voice', peer=None, message_id=1, caption='Caption', edited=0):
        if category in ('voice', 'audio'):
            attribute = types.DocumentAttributeAudio(duration=3, voice=category=='voice')
            mime = 'audio/ogg'
        else:
            attribute = types.DocumentAttributeVideo(duration=3, w=64, h=64, round_message=category=='video_notes')
            mime = 'video/mp4'
        media = types.MessageMediaDocument(document=types.Document(id=media_id, access_hash=456, file_reference=b'ref',
            date=self.date, mime_type=mime, size=3, dc_id=2, attributes=[attribute]))
        return types.Message(id=message_id, peer_id=peer or types.PeerUser(42), date=self.date, message=caption,
            media=media, edit_date=self.date+timedelta(seconds=edited) if edited else None)

    async def asyncTearDown(self):
        await self.db.close()
        self.directory.cleanup()

    async def job(self, media_id=123):
        async with self.db.session() as session:
            return await session.scalar(select(Transcription).join(Attachment).where(Attachment.media_key==f'Document:{media_id}'))

    async def hits(self, query):
        async with self.db.session() as session:
            return (await session.execute(text('SELECT rowid FROM search WHERE search MATCH :q'), {'q': query})).scalars().all()

    async def test_final_result_is_saved_searchable_and_does_not_edit_the_message(self):
        await transcribe(self.collector, await self.job())
        job = await self.job()
        self.assertEqual((job.state, job.body, job.telegram_id), ('complete', self.result.text, '999'))
        self.assertEqual((job.source_chat_id, job.source_message_id), (42, 1))
        self.assertEqual(len(await self.hits('ПРИВЕТ')), 1)
        async with self.db.session() as session:
            self.assertEqual(await session.scalar(select(func.count()).select_from(Version)), 1)
            version = await session.scalar(select(Version))
            self.assertEqual(version.body, 'Caption')
        request = self.client.await_args.args[0]
        self.assertEqual(request.msg_id, 1)
        # Speech recognition is server-side: no attachment download or upload.
        self.client.download_media.assert_not_awaited()
        self.client.upload_file.assert_not_awaited()

    async def test_partial_and_raw_final_results_replace_index_without_regression(self):
        self.result.pending, self.result.text = True, 'Obsoletepartial'
        await transcribe(self.collector, await self.job())
        self.assertEqual((await self.job()).state, 'processing')
        self.assertEqual(len(await self.hits('Obsoletepartial')), 1)
        wrong = types.UpdateTranscribedAudio(peer=types.PeerUser(42), msg_id=1, transcription_id=111, text='Wrong result')
        await save_update(self.collector, wrong)
        self.assertEqual((await self.job()).body, 'Obsoletepartial')
        update = types.UpdateTranscribedAudio(peer=types.PeerUser(42), msg_id=1, transcription_id=999, text='Final speech')
        await save_update(self.collector, update)
        self.assertEqual((await self.job()).state, 'complete')
        self.assertEqual(await self.hits('Obsoletepartial'), [])
        self.assertEqual(len(await self.hits('Final')), 1)
        await store_result(self.db, (await self.job()).attachment_id, self.result)
        self.assertEqual((await self.job()).body, 'Final speech')

    async def test_caption_edits_share_transcript_but_replaced_recordings_do_not(self):
        await transcribe(self.collector, await self.job())
        await save_message(self.db, self.config, self.recording(caption='Newcaption', edited=20))
        self.assertEqual(len(await self.hits('Newcaption привет')), 1)
        self.assertEqual(len(await self.hits('Caption привет')), 1)
        await save_message(self.db, self.config, self.recording(media_id=456, caption='Replacement', edited=40))
        self.assertEqual(await self.hits('Replacement привет'), [])
        self.assertEqual((await self.job(456)).state, 'pending')
        async with self.db.session() as session:
            self.assertEqual(await session.scalar(select(func.count()).select_from(Version)), 3)
            self.assertEqual(await session.scalar(select(func.count()).select_from(Transcription)), 2)

    async def test_recording_changed_during_rpc_is_not_misattributed(self):
        self.client.get_messages.side_effect = [self.message, self.recording(media_id=456)]
        await transcribe(self.collector, await self.job())
        job = await self.job()
        self.assertEqual(job.state, 'unavailable')
        self.assertEqual(job.body, '')
        self.assertEqual(await self.hits('Recognized'), [])

    async def test_early_raw_update_is_ignored_and_pending_poll_recovers(self):
        async def rpc(request):
            await save_update(self.collector, types.UpdateTranscribedAudio(peer=types.PeerUser(42),
                msg_id=1, transcription_id=999, text='Early final'))
            self.assertEqual((await self.job()).body, '')
            return types.messages.TranscribedAudio(transcription_id=999, text='', pending=True)
        self.client.side_effect = rpc
        await transcribe(self.collector, await self.job())
        self.assertEqual((await self.job()).state, 'processing')
        self.client.side_effect = None
        await transcribe(self.collector, await self.job())
        self.assertEqual((await self.job()).state, 'complete')

    async def test_existing_archive_upgrade_and_interrupted_request_recovery(self):
        async with self.db.engine.begin() as connection:
            await connection.run_sync(Transcription.__table__.drop)
        await self.db.initialize()
        self.assertEqual((await self.job()).state, 'pending')
        async with self.db.write() as session:
            (await session.get(Transcription, (await self.job()).attachment_id)).state = 'requesting'
        await self.db.initialize()
        self.assertEqual((await self.job()).state, 'pending')

    async def test_trial_exhaustion_suspends_requests_until_reset(self):
        reset = datetime.now(timezone.utc)+timedelta(hours=1)
        self.result.trial_remains_num, self.result.trial_remains_until_date = 0, reset
        await transcribe(self.collector, await self.job())
        async with self.db.session() as session:
            from tg_archiver.db import Setting
            setting = await session.get(Setting, 'transcription_retry_at')
            self.assertAlmostEqual(float(setting.value), reset.timestamp(), places=2)
        self.client.reset_mock()
        with patch('tg_archiver.transcription.asyncio.sleep', side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await collect_transcriptions(self.collector)
        self.client.assert_not_awaited()

    async def test_flood_premium_and_unsupported_media_are_reported(self):
        cases = [(errors.FloodWaitError(request=None, capture=20), 'error'),
                 (errors.FloodPremiumWaitError(request=None, capture=30), 'error'),
                 (errors.PremiumAccountRequiredError(request=None), 'error'),
                 (errors.BadRequestError(request=None, message='MSG_VOICE_MISSING'), 'unavailable')]
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                await self.db.retry()
                self.client.side_effect = error
                with patch('tg_archiver.transcription.asyncio.sleep', side_effect=asyncio.CancelledError):
                    with self.assertRaises(asyncio.CancelledError):
                        await collect_transcriptions(self.collector)
                self.assertEqual((await self.job()).state, expected)
        self.assertEqual(self.collector.flood.await_count, 2)

    async def test_quota_pause_still_allows_recovery_of_an_inflight_result(self):
        self.result.pending, self.result.trial_remains_num = True, 0
        self.result.trial_remains_until_date = datetime.now(timezone.utc)+timedelta(days=7)
        await transcribe(self.collector, await self.job())
        async with self.db.write() as session:
            (await session.get(Transcription, (await self.job()).attachment_id)).retry_at = 0
        async def recover(collector, item):
            self.assertEqual(item.telegram_id, '999')
            raise asyncio.CancelledError()
        with patch('tg_archiver.transcription.transcribe', side_effect=recover):
            with self.assertRaises(asyncio.CancelledError):
                await collect_transcriptions(self.collector)

    async def test_private_media_gets_priority_and_excluded_chats_are_not_used(self):
        await save_message(self.db, self.config, self.recording(media_id=456, peer=types.PeerChat(7), message_id=2))
        async def halt(collector, item):
            self.assertEqual(item.attachment_id, (await self.job()).attachment_id)
            raise asyncio.CancelledError()
        with patch('tg_archiver.transcription.transcribe', side_effect=halt):
            with self.assertRaises(asyncio.CancelledError):
                await collect_transcriptions(self.collector)
        self.config.excluded_chats = {42, -7}
        with patch('tg_archiver.transcription.asyncio.sleep', side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await collect_transcriptions(self.collector)
        self.client.assert_not_awaited()

    async def test_configuration_switch_and_protected_recordings(self):
        self.config.transcribe_audio = False
        with patch('tg_archiver.transcription.asyncio.sleep', side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await collect_transcriptions(self.collector)
        self.client.assert_not_awaited()
        await save_message(self.db, self.config, self.recording(media_id=456, message_id=2), protected=True)
        self.assertEqual((await self.job(456)).state, 'unavailable')
        await transcribe(self.collector, await self.job(456))
        self.client.assert_not_awaited()

    async def test_web_search_and_status_include_transcripts_and_version_links(self):
        self.result.text = 'Speech <script>alert(1)</script> secretword'
        await transcribe(self.collector, await self.job())
        await save_message(self.db, self.config, self.recording(caption='Edited caption', edited=20))
        async with TestClient(TestServer(make_app(self.db, self.config, self.collector))) as client:
            body = await (await client.get('/chat/42')).text()
            self.assertIn('Version history · 2 saved versions', body)
            self.assertIn('Speech &lt;script&gt;', body)
            self.assertNotIn('Speech <script>', body)
            body = await (await client.get('/search?q=secretword')).text()
            self.assertIn('Telegram transcription', body)
            self.assertIn('Speech &lt;script&gt;', body)
            self.assertIn('?version=', body)
            job = await self.job()
            data = await (await client.get(f'/transcriptions?ids={job.attachment_id}')).json()
            self.assertEqual(data[str(job.attachment_id)]['body'], self.result.text)
            for ids in ['abc', '-1', '9'*30, ','.join(map(str, range(101)))]:
                self.assertEqual((await client.get('/transcriptions?ids='+ids)).status, 400)
            self.assertIn('Speech recognition', await (await client.get('/status')).text())

    async def test_round_video_and_audio_are_queued(self):
        for index, category in enumerate(('video_notes', 'audio', 'videos'), 1):
            await save_message(self.db, self.config, self.recording(media_id=123+index, message_id=1+index, category=category))
            self.assertEqual((await self.job(123+index)).state, 'pending')
