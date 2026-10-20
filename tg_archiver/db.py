import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint, event, select, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now():
    return datetime.now(timezone.utc).isoformat()


class Base(DeclarativeBase):
    pass


class Chat(Base):
    __tablename__ = "chats"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    kind: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(Text)
    cursor: Mapped[int] = mapped_column(default=0)
    done: Mapped[bool] = mapped_column(default=False)
    enabled: Mapped[bool] = mapped_column(default=True)
    config_hash: Mapped[str] = mapped_column(default="")
    error: Mapped[str] = mapped_column(default="")
    reconciled: Mapped[str] = mapped_column(default="")
    migrated_to: Mapped[int | None] = mapped_column()
    catchup_floor: Mapped[int] = mapped_column(default=0)
    catchup_cursor: Mapped[int] = mapped_column(default=0)
    catchup_pending: Mapped[bool] = mapped_column(default=False)


class Avatar(Base):
    __tablename__ = "avatars"
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id"), primary_key=True)
    photo_id: Mapped[str] = mapped_column(default="")
    downloaded_photo_id: Mapped[str] = mapped_column(default="")
    path: Mapped[str | None] = mapped_column()
    retry_at: Mapped[float] = mapped_column(default=0.0)
    attempts: Mapped[int] = mapped_column(default=0)
    error: Mapped[str] = mapped_column(default="")


class Sender(Base):
    __tablename__ = "senders"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(Text)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("chat_id", "telegram_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id"), index=True)
    telegram_id: Mapped[int] = mapped_column()
    sent_at: Mapped[str] = mapped_column(index=True)
    sender_id: Mapped[int | None] = mapped_column()
    reply_to: Mapped[int | None] = mapped_column()
    album: Mapped[str | None] = mapped_column()
    latest_id: Mapped[int | None] = mapped_column()
    latest_source_at: Mapped[str] = mapped_column(default="")
    deleted_at: Mapped[str | None] = mapped_column()


class Version(Base):
    __tablename__ = "versions"
    __table_args__ = (UniqueConstraint("message_id", "fingerprint", "source_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"), index=True)
    source_at: Mapped[str] = mapped_column()
    observed_at: Mapped[str] = mapped_column(default=now)
    fingerprint: Mapped[str] = mapped_column()
    body: Mapped[str] = mapped_column(Text)
    raw: Mapped[str] = mapped_column(Text)


class Attachment(Base):
    __tablename__ = "attachments"
    id: Mapped[int] = mapped_column(primary_key=True)
    media_key: Mapped[str] = mapped_column(unique=True)
    category: Mapped[str] = mapped_column()
    name: Mapped[str] = mapped_column(Text)
    mime: Mapped[str] = mapped_column()
    size: Mapped[int] = mapped_column()
    path: Mapped[str | None] = mapped_column()
    state: Mapped[str] = mapped_column(default="pending", index=True)
    attempts: Mapped[int] = mapped_column(default=0)
    retry_at: Mapped[float] = mapped_column(default=0.0)
    priority: Mapped[int] = mapped_column(default=0)
    error: Mapped[str] = mapped_column(default="")


class VersionAttachment(Base):
    __tablename__ = "version_attachments"
    version_id: Mapped[int] = mapped_column(ForeignKey("versions.id"), primary_key=True)
    attachment_id: Mapped[int] = mapped_column(ForeignKey("attachments.id"), primary_key=True)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class Database:
    def __init__(self, path):
        self.engine = create_async_engine(URL.create("sqlite+aiosqlite", database=str(path)), hide_parameters=True)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.write_lock = asyncio.Lock()

        @event.listens_for(self.engine.sync_engine, "connect")
        def pragmas(connection, _):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    async def initialize(self):
        async with self.engine.begin() as connection:
            await connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            await connection.run_sync(Base.metadata.create_all)
            await connection.exec_driver_sql("CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(body, tokenize='unicode61')")
        async with self.write() as session:
            schema = await session.get(Setting, "schema")
            if schema and schema.value != "1":
                raise RuntimeError("Unsupported archive schema; use a compatible application version")
            if not schema:
                session.add(Setting(key="schema", value="1"))
            await session.execute(text("UPDATE attachments SET state='pending' WHERE state='downloading'"))

    @asynccontextmanager
    async def write(self):
        async with self.write_lock, self.session() as session, session.begin():
            yield session

    async def close(self):
        await self.engine.dispose()

    async def set_status(self, key, value):
        async with self.write() as session:
            item = await session.get(Setting, key)
            if item:
                item.value = value
            else:
                session.add(Setting(key=key, value=value))

    async def retry(self):
        async with self.write() as session:
            await session.execute(text("UPDATE attachments SET state='pending', attempts=0, retry_at=0, error='' WHERE state IN ('error','unavailable')"))
            await session.execute(text("UPDATE avatars SET attempts=0, retry_at=0, error='' WHERE error != ''"))
            for chat in (await session.scalars(select(Chat).where(Chat.error != ""))).all():
                chat.error = ""
