import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import event
from aiohttp.test_utils import TestClient, TestServer

import test_archive
from tg_archiver.archive import save_message
from tg_archiver.web import make_app


class LoadingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_archive.ArchiveTests.asyncSetUp
    asyncTearDown = test_archive.ArchiveTests.asyncTearDown
    message = test_archive.ArchiveTests.message

    async def test_conversation_fragment_is_bounded_and_skips_sidebar(self):
        for number in range(1, 62):
            await save_message(self.db, self.config, self.message(f"body {number}", message_id=number))
        statements = []

        def record(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(self.db.engine.sync_engine, "before_cursor_execute", record)
        try:
            async with TestClient(TestServer(make_app(self.db, self.config, SimpleNamespace(fatal=None)))) as client:
                with patch("tg_archiver.web.sidebar_data", side_effect=AssertionError("sidebar rebuilt")):
                    response = await client.get("/chat/42", headers={"X-Archive-Fragment": "workspace"})
                    body = await response.text()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers["Vary"], "X-Archive-Fragment")
                    self.assertIn('id="workspace"', body)
                    self.assertNotIn('<aside', body)
                    self.assertNotIn('id="m11"', body)
                    self.assertIn('id="m12"', body)
                    self.assertIn('id="m61"', body)
                    self.assertIn('offset=50', body)
                    self.assertLess(len(statements), 25, statements)
                    response = await client.get("/chat/42?offset=50", headers={"X-Archive-Fragment": "workspace"})
                    body = await response.text()
                    self.assertIn('id="m1"', body)
                    self.assertNotIn('id="m12"', body)
                response = await client.get("/chat/42")
                self.assertIn('<aside', await response.text())
        finally:
            event.remove(self.db.engine.sync_engine, "before_cursor_execute", record)
