import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sqlalchemy import func, select, text
from telethon import types
from datetime import datetime, timezone

from tg_archiver.archive import save_message
from tg_archiver.config import Config
from tg_archiver.db import Attachment, Chat, Database, Message, Setting, Version
from tg_archiver.importer import import_export
from tg_archiver.web import fallback


class ImportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.config = Config(data_dir=self.root / "data")
        self.config.data_dir.mkdir()
        self.export = self.root / "export"
        self.export.mkdir()
        self.path = self.export / "result.json"
        self.record = dict(id=1, type="message", date="2026-01-01T00:00:00", date_unixtime="1767225600",
                           from_id="user42", **{"from": "Alice"}, text=["Hello ", {"type": "bold", "text": "world"}])
        self.chat = dict(id=42, type="personal_chat", name="Alice", messages=[self.record])

    async def asyncTearDown(self):
        self.directory.cleanup()

    def write(self, data=None):
        self.path.write_text(json.dumps(data or self.chat), encoding="utf-8")

    async def inspect(self, callback):
        db = Database(self.config.data_dir / "archive.sqlite3")
        try:
            async with db.session() as session:
                return await callback(session)
        finally:
            await db.close()

    async def test_dry_run_and_idempotent_import_search_attachment(self):
        (self.export / "files").mkdir()
        (self.export / "files" / "report.PDF").write_bytes(b"content")
        self.record.update(file="files/report.PDF", file_name="report.PDF", mime_type="application/pdf")
        self.write()
        dry = await import_export(self.config, self.path, True)
        self.assertEqual((dry["messages"], dry["attachments"]), (1, 1))
        self.assertEqual(list(self.config.data_dir.iterdir()), [])
        self.assertEqual(await import_export(self.config, self.path), dry)
        again = await import_export(self.config, self.path)
        self.assertEqual((again["messages"], again["existing"]), (0, 1))
        async def check(session):
            message = await session.scalar(select(Message))
            self.assertEqual(message.sender_id, 42)
            version = await session.get(Version, message.latest_id)
            self.assertEqual(version.body, "Hello world")
            self.assertEqual(json.loads(version.raw)["export"], self.record)
            self.assertIn("Export metadata", str(fallback(version.raw, version.body)))
            self.assertEqual(await session.scalar(select(func.count()).select_from(Version)), 1)
            self.assertEqual(len((await session.execute(text("SELECT rowid FROM search WHERE search MATCH 'world'"))).all()), 1)
            attachment = await session.scalar(select(Attachment))
            self.assertEqual(attachment.state, "complete")
            self.assertTrue(attachment.path.startswith("42/"))
            self.assertTrue(attachment.path.endswith(".PDF"))
            self.assertEqual((self.config.data_dir / "media" / attachment.path).read_bytes(), b"content")
            chat = await session.get(Chat, 42)
            self.assertEqual((chat.cursor, chat.done, chat.catchup_pending), (0, False, False))
        await self.inspect(check)

    async def test_full_export_types_owner_missing_media_and_service(self):
        chats = []
        for index, kind in enumerate(["private_group", "private_supergroup", "public_channel", "saved_messages"], 1):
            record = dict(self.record, id=index, type="service", text="", actor_id="channel7", action="create_group")
            record.pop("from_id")
            record.update(photo="(Photo not included. Change data exporting settings.)")
            chats.append(dict(id=index, type=kind, messages=[record]))
        self.write(dict(personal_information=dict(user_id=4), chats=dict(list=chats[:2]), left_chats=dict(list=chats[2:])))
        result = await import_export(self.config, self.path)
        self.assertEqual((result["messages"], result["missing"]), (4, 4))
        async def check(session):
            self.assertEqual(set((await session.scalars(select(Chat.id))).all()), {-1, -1000000000002, -1000000000003, 4})
            self.assertEqual((await session.get(Setting, "account_id")).value, "4")
        await self.inspect(check)

    async def test_existing_content_checkpoints_and_deletions_are_preserved(self):
        db = Database(self.config.data_dir / "archive.sqlite3")
        await db.initialize()
        await save_message(db, self.config, types.Message(id=1, peer_id=types.PeerUser(42),
                           date=datetime(2026, 1, 1, tzinfo=timezone.utc), message="Live content"))
        async with db.write() as session:
            chat = await session.get(Chat, 42)
            chat.cursor, chat.done = 99, True
            message = await session.scalar(select(Message))
            message.deleted_at = "2026-01-02T00:00:00+00:00"
        await db.close()
        self.write()
        result = await import_export(self.config, self.path)
        self.assertEqual(result["existing"], 1)
        async def check(session):
            message = await session.scalar(select(Message))
            self.assertEqual((await session.get(Version, message.latest_id)).body, "Live content")
            self.assertIsNotNone(message.deleted_at)
            chat = await session.get(Chat, 42)
            self.assertEqual((chat.cursor, chat.done), (99, True))
        await self.inspect(check)

    async def test_invalid_export_preflight_writes_nothing(self):
        for changes in [dict(date_unixtime="bad"), dict(from_id="not-an-id"), dict(text=42),
                        dict(file="../outside.txt"), dict(file="/etc/passwd"), dict(file="C:\\secret.txt")]:
            with self.subTest(changes=changes):
                self.chat["messages"] = [self.record, dict(self.record, id=2, **changes)]
                self.write()
                with self.assertRaises(ValueError):
                    await import_export(self.config, self.path)
                self.assertEqual(list(self.config.data_dir.iterdir()), [])
        outside = self.root / "secret.txt"
        outside.write_text("private")
        (self.export / "link").symlink_to(outside)
        self.chat["messages"] = [dict(self.record, file="link")]
        self.write()
        with self.assertRaises(ValueError):
            await import_export(self.config, self.path)

    async def test_exclusions_media_filters_and_account_mismatch(self):
        self.record["photo"] = "missing.jpg"
        self.write()
        self.config.excluded_chats.add(42)
        self.assertEqual((await import_export(self.config, self.path, True))["excluded"], 1)
        self.config.excluded_chats.clear()
        self.config.media["photos"] = False
        self.assertEqual((await import_export(self.config, self.path))["skipped_media"], 1)
        self.write(dict(personal_information=dict(user_id=1), chats=dict(list=[])))
        await import_export(self.config, self.path)
        self.write(dict(personal_information=dict(user_id=2), chats=dict(list=[])))
        for dry in (True, False):
            with self.assertRaisesRegex(ValueError, "different Telegram account"):
                await import_export(self.config, self.path, dry)

    async def test_cli_offline_without_credentials(self):
        self.write()
        config = self.root / "config.toml"
        config.write_text('[archive]\ndata_dir="cli-data"\n')
        env = {key: value for key, value in os.environ.items() if key not in {"TG_API_ID", "TG_API_HASH"}}
        for extra in (["--dry-run"], []):
            result = subprocess.run([sys.executable, "-m", "tg_archiver", "import", str(self.path),
                                     "--config", str(config), *extra], capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("messages=1", result.stdout)
            if extra:
                self.assertFalse((self.root / "cli-data" / "archive.sqlite3").exists())

    async def test_interrupted_batches_resume_and_cross_chat_replies_stay_metadata(self):
        from tg_archiver.importer import save_imported
        self.chat["messages"] = [dict(self.record, id=index, reply_to_message_id=9,
                                       reply_to_peer_id="channel7") for index in range(1, 102)]
        self.write()

        async def interrupted(session, config, item):
            if item["telegram_id"] == 101:
                raise OSError("Simulated storage interruption")
            await save_imported(session, config, item)

        with patch("tg_archiver.importer.save_imported", interrupted):
            with self.assertRaises(OSError):
                await import_export(self.config, self.path)
        resumed = await import_export(self.config, self.path)
        self.assertEqual((resumed["existing"], resumed["messages"]), (100, 1))
        async def check(session):
            self.assertEqual(await session.scalar(select(func.count()).select_from(Message)), 101)
            self.assertIsNone((await session.scalar(select(Message))).reply_to)
        await self.inspect(check)
