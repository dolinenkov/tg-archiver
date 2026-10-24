import argparse
import asyncio
from contextlib import contextmanager
import logging
import os
from pathlib import Path
import sys

from aiohttp import web

from .collector import Collector, client_for
from .config import load
from .db import Database
from .web import make_app


@contextmanager
def data_lock(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = (directory / ".lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            if not handle.read(1):
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError("Another archiver process owns this data directory") from None
    try:
        yield
    finally:
        handle.close()


async def command(config, name):
    if name == "retry":
        db = Database(config.data_dir / "archive.sqlite3")
        try:
            await db.initialize()
            await db.retry()
        finally:
            await db.close()
        print("Failed work queued for retry.")
        return
    client = client_for(config)
    try:
        if name == "login":
            await client.start()
            print("Telegram session authorized.")
        else:
            await client.connect()
            if not await client.is_user_authorized():
                raise RuntimeError("Run login first")
            async for dialog in client.iter_dialogs():
                print(f"{dialog.id}\t{dialog.name}")
    finally:
        await client.disconnect()


async def application(config):
    db = Database(config.data_dir / "archive.sqlite3")
    try:
        await db.initialize()
        collector = Collector(db, config)
    except BaseException:
        await db.close()
        raise
    app = make_app(db, config, collector)

    async def lifecycle(app):
        await collector.start()
        try:
            yield
        finally:
            await collector.stop()
            await db.close()

    app.cleanup_ctx.append(lifecycle)
    return app


def main():
    parser = argparse.ArgumentParser(description="Personal Telegram history archive")
    parser.add_argument("command", choices=["login", "chats", "run", "retry", "import"], default="run", nargs="?")
    parser.add_argument("export", type=Path, nargs="?", help="Telegram Desktop result.json (import only)")
    parser.add_argument("--dry-run", action="store_true", help="Validate and summarize an import without writing archive data")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    args = parser.parse_args()
    if (args.command == "import") != (args.export is not None) or (args.dry_run and args.command != "import"):
        parser.error("import requires an export path; export paths and --dry-run apply only to import")
    # Newly created session/database files contain private authorization and messages.
    if os.name != "nt":
        os.umask(0o077)
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("telethon").setLevel(logging.CRITICAL)
    try:
        config = load(args.config)
        with data_lock(config.data_dir):
            if args.command == "import":
                from .importer import import_export
                counts = asyncio.run(import_export(config, args.export, args.dry_run))
                print(("Dry run: " if args.dry_run else "Imported: ") + ", ".join(f"{key}={value}" for key, value in counts.items()))
            elif args.command == "run":
                web.run_app(application(config), host=config.host, port=config.port, access_log=None)
            else:
                asyncio.run(command(config, args.command))
    except (ValueError, RuntimeError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
