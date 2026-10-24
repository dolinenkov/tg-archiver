import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select
from telethon import errors

from tg_archiver.config import Config
from tg_archiver.collector import Collector
from tg_archiver.db import Database, Message, Setting
from tg_archiver.web import make_app


class SetupWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.config = Config(data_dir=Path(self.directory.name))
        self.db = Database(self.config.data_dir / "archive.sqlite3")
        await self.db.initialize()
        self.telegram = SimpleNamespace(
            is_connected=Mock(return_value=False), connect=AsyncMock(), is_user_authorized=AsyncMock(return_value=False),
            send_code_request=AsyncMock(return_value=SimpleNamespace(phone_code_hash="secret-hash")),
            sign_in=AsyncMock(), get_me=AsyncMock(return_value=SimpleNamespace(id=42, bot=False)))
        self.collector = SimpleNamespace(client=self.telegram, stop=AsyncMock(), start=AsyncMock(), fatal=None)
        self.app = make_app(self.db, self.config, self.collector)
        self.setup = self.app["setup"]
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.db.close()
        self.directory.cleanup()

    async def post(self, action, **values):
        response = await self.client.post('/settings', data=dict(csrf=self.setup.csrf, action=action, **values),
                                          headers={'Origin': str(self.client.make_url('/')).rstrip('/')}, allow_redirects=False)
        self.assertEqual(response.status, 303, await response.text())
        await self.setup.task
        return await (await self.client.get('/settings')).text()

    async def test_phone_code_and_password_login(self):
        page = await self.post('phone', phone='+123456789')
        self.assertIn('Telegram login code', page)
        self.assertNotIn('secret-hash', page)
        self.collector.stop.assert_awaited_once()
        self.telegram.sign_in.side_effect = errors.SessionPasswordNeededError(request=None)
        page = await self.post('code', code='12345')
        self.assertIn('Two-step verification password', page)
        self.assertNotIn('12345', page)
        self.telegram.sign_in.side_effect = None
        page = await self.post('password', password='supersecret')
        self.assertIn('Signed in.', page)
        self.assertNotIn('supersecret', page)
        self.assertIsNone(self.setup.phone)
        self.assertIsNone(self.setup.code_hash)
        self.assertFalse(self.setup.paused)
        self.collector.start.assert_awaited_once()
        async with self.db.session() as session:
            self.assertEqual((await session.get(Setting, 'account_id')).value, '42')

    async def test_invalid_code_cancel_and_expiry(self):
        await self.post('phone', phone='+123')
        self.telegram.sign_in.side_effect = errors.PhoneCodeInvalidError(request=None)
        page = await self.post('code', code='999')
        self.assertIn('Incorrect code', page)
        self.assertEqual(self.setup.step, 'code')
        self.assertTrue(self.setup.paused)
        await self.post('cancel')
        self.assertFalse(self.setup.paused)
        await self.post('phone', phone='+123')
        self.setup.expired()
        await self.setup.task
        self.assertFalse(self.setup.paused)
        self.assertIn('expired', self.setup.notice)

    async def test_import_preview_and_confirmation_share_database(self):
        export = self.config.data_dir / 'export'
        export.mkdir()
        (export / 'photo.jpg').write_bytes(b'photo')
        path = export / 'result.json'
        path.write_text(json.dumps(dict(id=42, type='personal_chat', name='Alice', messages=[dict(
            id=1, type='message', date_unixtime='1767225600', text='Imported through web', photo='photo.jpg')])) )
        page = await self.post('preview', path=str(path))
        self.assertIn('Import preview', page)
        self.assertEqual(self.setup.preview['messages'], 1)
        async with self.db.session() as session:
            self.assertIsNone(await session.scalar(select(Message)))
        with patch.object(self.db, 'initialize', side_effect=AssertionError('Must reuse initialized database')):
            page = await self.post('import')
        self.assertIn('Import complete', page)
        self.assertEqual(self.setup.result['attachments'], 1)
        self.assertFalse(self.setup.paused)
        self.assertEqual(self.collector.stop.await_count, 2)
        self.assertEqual(self.collector.start.await_count, 2)
        async with self.db.session() as session:
            self.assertIsNotNone(await session.scalar(select(Message)))
        page = await (await self.client.get('/chat/42')).text()
        self.assertIn('Imported through web', page)

    async def test_import_failure_resumes_and_csrf_is_required(self):
        response = await self.client.post('/settings', data={'action': 'phone', 'phone': '+123'})
        self.assertEqual(response.status, 403)
        self.collector.stop.assert_not_awaited()
        response = await self.client.post('/settings', data={'csrf': self.setup.csrf, 'action': 'cancel'},
                                          headers={'Origin': 'https://attacker.example'})
        self.assertEqual(response.status, 403)
        page = await self.post('preview', path=str(self.config.data_dir / 'absent.json'))
        self.assertIn('not found or unreadable', page)
        self.assertFalse(self.setup.paused)
        self.collector.start.assert_awaited_once()

    async def test_concurrent_operations_and_import_during_login_are_rejected(self):
        gate = asyncio.Event()
        self.telegram.send_code_request.side_effect = lambda phone: None
        async def wait_for_code(phone):
            await gate.wait()
            return SimpleNamespace(phone_code_hash='hash')
        self.telegram.send_code_request.side_effect = wait_for_code
        response = await self.client.post('/settings', data={'csrf': self.setup.csrf, 'action': 'phone', 'phone': '+123'}, allow_redirects=False)
        self.assertEqual(response.status, 303)
        response = await self.client.post('/settings', data={'csrf': self.setup.csrf, 'action': 'preview', 'path': 'test'})
        self.assertEqual(response.status, 409)
        gate.set()
        await self.setup.task
        response = await self.client.post('/settings', data={'csrf': self.setup.csrf, 'action': 'preview', 'path': 'test'})
        self.assertEqual(response.status, 409)
        await self.post('cancel')

    async def test_account_mismatch_does_not_replace_owner(self):
        await self.db.set_status('account_id', '99')
        await self.post('phone', phone='+123')
        page = await self.post('code', code='12345')
        self.assertIn('different Telegram account', page)
        self.assertFalse(self.setup.paused)
        async with self.db.session() as session:
            self.assertEqual((await session.get(Setting, 'account_id')).value, '99')

    async def test_collector_restart_does_not_duplicate_update_handlers(self):
        collector = Collector.__new__(Collector)
        handlers = []
        def remove(callback):
            handlers[:] = [item for item in handlers if item[0] != callback]
        collector.client = SimpleNamespace(add_event_handler=lambda callback, event: handlers.append((callback, event)),
                                           remove_event_handler=remove, disconnect=AsyncMock())
        collector.ready = asyncio.Event()
        collector.verified = False
        async def idle(*args):
            await asyncio.Event().wait()
        collector.guard = idle
        for _ in range(2):
            await collector.start()
            self.assertEqual(len(handlers), 4)
            await collector.stop()
            self.assertEqual(handlers, [])

    async def test_flood_wait_allows_cancel_and_shutdown_waits_for_work(self):
        self.telegram.send_code_request.side_effect = errors.FloodWaitError(request=None, capture=20)
        page = await self.post('phone', phone='+123')
        self.assertIn('wait of 20 seconds', page)
        await self.post('cancel')
        self.assertFalse(self.setup.paused)
        gate = asyncio.Event()
        self.setup.task = asyncio.create_task(gate.wait())
        cleanup = asyncio.create_task(self.setup.cleanup(self.app))
        await asyncio.sleep(0)
        self.assertFalse(cleanup.done())
        gate.set()
        await cleanup
