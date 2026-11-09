import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import text

from tg_archiver.config import Config, load
from tg_archiver.db import Attachment, Chat, Database, Message, Version, VersionAttachment
from tg_archiver.mcp import make_mcp_app


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(data_dir=Path(self.temp.name))
        self.db = Database(self.config.data_dir / 'archive.sqlite3')
        await self.db.initialize()
        async with self.db.write() as session:
            session.add(Chat(id=1, title='Family', kind='user'))
            session.add(Message(id=1, chat_id=1, telegram_id=12, sent_at='2025-01-01', latest_id=1))
            session.add(Version(id=1, message_id=1, source_at='', fingerprint='x', body='hello world', raw='{}'))
            session.add(Attachment(id=1, media_key='one', category='voice', name='note.ogg', mime='audio/ogg', size=3, path='1.ogg', state='complete'))
            session.add(VersionAttachment(version_id=1, attachment_id=1))
            await session.execute(text("INSERT INTO search(rowid,body) VALUES(1,'hello world')"))
        (self.config.data_dir / 'media').mkdir()
        (self.config.data_dir / 'media' / '1.ogg').write_bytes(b'abc')
        self.client = TestClient(TestServer(make_mcp_app(self.db, self.config)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.db.close()
        self.temp.cleanup()

    async def rpc(self, method, params=None, headers=None):
        return await self.client.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}}, headers=headers)

    async def tool(self, name, args):
        response = await self.rpc('tools/call', {'name': name, 'arguments': args})
        result = (await response.json())['result']
        self.assertNotIn('isError', result)
        return json.loads(result['content'][0]['text'])

    async def test_search_and_export(self):
        self.assertEqual((await self.tool('find_chats', {'query': 'Fam'}))['items'][0]['id'], 1)
        results = await self.tool('search_messages', {'query': '"hello world"', 'chat_id': 1})
        self.assertEqual(results['items'][0]['message_id'], 12)
        exported = await self.tool('export_attachments', {'chat_id': 1, 'categories': ['voice']})
        self.assertEqual(exported['attachments'], 1)
        with zipfile.ZipFile(exported['path']) as archive:
            self.assertEqual(archive.read('media/1-12-note.ogg'), b'abc')
            self.assertEqual(len(json.loads(archive.read('manifest.json'))), 1)

    async def test_protocol_and_origin(self):
        response = await self.rpc('initialize', {'protocolVersion': '2025-03-26'})
        self.assertEqual((await response.json())['result']['protocolVersion'], '2025-03-26')
        response = await self.rpc('tools/list', headers={'Origin': 'https://evil.example'})
        self.assertEqual(response.status, 403)
        response = await self.rpc('tools/call', {'name': 'list_attachments', 'arguments': {'chat_id': 1, 'offset': -1}})
        self.assertTrue((await response.json())['result']['isError'])


class MCPConfigTests(unittest.TestCase):
    def test_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            path.write_text('[mcp]\nenabled=true\nhost="127.0.0.1"\nport=9090\n')
            config = load(path)
            self.assertTrue(config.mcp_enabled)
            self.assertEqual(config.mcp_port, 9090)
            path.write_text('[mcp]\nport=true\n')
            with self.assertRaises(ValueError):
                load(path)
