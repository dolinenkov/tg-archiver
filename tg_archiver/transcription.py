"""Telegram-native speech transcription, cached by stable media identity."""
import asyncio
import time

from sqlalchemy import exists, select, text
from sqlalchemy.exc import SQLAlchemyError
from telethon import errors, functions, utils

from .db import Attachment, Chat, Message, Setting, Transcription, Version, VersionAttachment, now


CATEGORIES = {'voice', 'video_notes', 'audio', 'videos'}


async def queue_transcription(session, attachment, protected=False):
    if attachment.category not in CATEGORIES:
        return None
    item = await session.get(Transcription, attachment.id)
    if not item:
        blocked = protected or attachment.error == 'Protected content' or attachment.media_key.startswith('export:')
        item = Transcription(attachment_id=attachment.id, state='unavailable' if blocked else 'pending', body='',
            error='Protected content' if protected or attachment.error == 'Protected content' else
                  'Imported media has no Telegram identity' if attachment.media_key.startswith('export:') else '')
        session.add(item)
        await session.flush()
    return item


async def index_versions(session, version_ids):
    for version_id in set(version_ids):
        version = await session.get(Version, version_id)
        transcripts = (await session.scalars(select(Transcription.body).join(VersionAttachment,
            VersionAttachment.attachment_id == Transcription.attachment_id).where(
            VersionAttachment.version_id == version_id, Transcription.body != ''))).all()
        await session.execute(text('DELETE FROM search WHERE rowid=:id'), {'id': version_id})
        await session.execute(text('INSERT INTO search(rowid,body) VALUES (:id,:body)'),
            {'id': version_id, 'body': '\n'.join([version.body, *transcripts])})


async def store_result(db, attachment_id, result, allow_rebind=False):
    async with db.write() as session:
        item = await session.get(Transcription, attachment_id)
        if not item or (item.telegram_id and item.telegram_id != str(result.transcription_id) and not allow_rebind):
            return
        # A final raw update can arrive before the initiating RPC returns.
        if item.state == 'complete':
            return
        item.telegram_id = str(result.transcription_id)
        item.body = result.text or ''
        item.state = 'processing' if result.pending else 'complete'
        item.retry_at = time.time() + 60 if result.pending else 0
        item.error, item.updated_at = '', now()
        await session.flush()
        ids = (await session.scalars(select(VersionAttachment.version_id).where(
            VersionAttachment.attachment_id == attachment_id))).all()
        await index_versions(session, ids)


async def save_update(collector, update):
    chat_id = utils.get_peer_id(update.peer)
    if not collector.config.transcribe_audio or not collector.config.included(chat_id):
        return
    async with collector.db.session() as session:
        items = (await session.scalars(select(Transcription).where(Transcription.source_chat_id == chat_id,
            Transcription.source_message_id == update.msg_id,
            Transcription.state.in_(['requesting', 'processing', 'error', 'pending', 'unavailable'])))).all()
    for item in items:
        if item.telegram_id == str(update.transcription_id):
            await store_result(collector.db, item.attachment_id, update)


async def retry(collector, item, state, error, retry_at=0):
    async with collector.db.write() as session:
        current = await session.get(Transcription, item.attachment_id)
        if current.state == 'complete':
            return
        current.state, current.error, current.retry_at = state, error, retry_at
        current.attempts += 1
        current.updated_at = now()


async def transcribe(collector, item):
    from .archive import media_info
    async with collector.db.session() as session:
        attachment = await session.get(Attachment, item.attachment_id)
        sources = (await session.scalars(select(Message).join(Version, Message.latest_id == Version.id)
            .join(VersionAttachment, VersionAttachment.version_id == Version.id).join(Chat, Chat.id == Message.chat_id)
            .where(VersionAttachment.attachment_id == item.attachment_id, Chat.enabled.is_(True), Message.deleted_at.is_(None))
            .order_by((Message.chat_id > 0).desc(), Message.id))).all()
    if attachment.media_key.startswith('export:') or attachment.error == 'Protected content':
        await retry(collector, item, 'unavailable', 'Imported or protected media cannot be transcribed')
        return
    for source in sources:
        if not collector.config.included(source.chat_id):
            continue
        await collector.wait_api()
        try:
            entity = await collector.client.get_entity(source.chat_id)
        except (errors.ForbiddenError, errors.BadRequestError, ValueError):
            continue
        if getattr(entity, 'noforwards', False):
            continue
        await collector.wait_api()
        try:
            message = await collector.client.get_messages(entity, ids=source.telegram_id)
        except (errors.ForbiddenError, errors.BadRequestError, ValueError):
            continue
        info = media_info(message) if message else None
        if not info or info['media_key'] != attachment.media_key or getattr(message, 'noforwards', False):
            continue
        if not collector.config.included(source.chat_id) or not collector.config.transcribe_audio:
            return
        async with collector.db.write() as session:
            current = await session.get(Transcription, item.attachment_id)
            if current.state == 'complete':
                return
            current.state = 'requesting'
            current.source_chat_id, current.source_message_id = source.chat_id, source.telegram_id
        await collector.wait_api()
        result = await collector.client(functions.messages.TranscribeAudioRequest(entity, source.telegram_id))
        # The message may have been edited to replace its recording during the RPC.
        # Unknown raw updates are ignored until this media identity is verified.
        await collector.wait_api()
        checked = await collector.client.get_messages(entity, ids=source.telegram_id)
        checked_info = media_info(checked) if checked else None
        if not checked_info or checked_info['media_key'] != attachment.media_key:
            await retry(collector, item, 'unavailable', 'Recording changed during transcription')
            return
        await store_result(collector.db, item.attachment_id, result, allow_rebind=True)
        if getattr(result, 'trial_remains_num', None) == 0:
            reset = getattr(result, 'trial_remains_until_date', None)
            until = reset.timestamp() if reset else time.time() + 86400
            await collector.db.set_status('transcription_retry_at', str(max(until, time.time() + 60)))
            await collector.db.set_status('transcription_status', 'Telegram transcription quota exhausted; waiting for its reset')
        else:
            await collector.db.set_status('transcription_status', 'Telegram transcription active')
        return
    await retry(collector, item, 'unavailable', 'Original media is missing, changed, protected, or inaccessible')


async def collect_transcriptions(collector):
    while True:
        await collector.wait_api()
        if not collector.config.transcribe_audio:
            await asyncio.sleep(30)
            continue
        excluded = collector.config.excluded_chats | {chat_id for chat_id in collector.config.chats if not collector.config.included(chat_id)}
        async with collector.db.session() as session:
            cooldown = await session.get(Setting, 'transcription_retry_at')
            source = (select(Message.id).join(Version, Version.message_id == Message.id)
                .join(VersionAttachment, VersionAttachment.version_id == Version.id).join(Chat, Chat.id == Message.chat_id)
                .where(VersionAttachment.attachment_id == Transcription.attachment_id,
                       Chat.enabled.is_(True), Chat.id.not_in(excluded)))
            private = exists(source.where(Chat.id > 0))
            query = select(Transcription).join(Attachment).where(
                Transcription.state.in_(['pending', 'processing', 'error']), Transcription.retry_at <= time.time(), exists(source))
            if cooldown and float(cooldown.value) > time.time():
                # Finish already-initiated recognition after reconnect/missed updates,
                # without initiating transcription of any new recording.
                query = query.where(Transcription.telegram_id != '')
            item = await session.scalar(query.order_by(private.desc(), Attachment.priority.desc(), Transcription.attachment_id).limit(1))
        if item is None:
            await asyncio.sleep(5)
            continue
        try:
            await transcribe(collector, item)
        except (errors.FloodWaitError, errors.FloodPremiumWaitError) as error:
            await collector.flood(error)
            await retry(collector, item, 'error', 'Telegram flood wait', collector.flood_until)
        except errors.PremiumAccountRequiredError:
            until = time.time() + 86400
            await retry(collector, item, 'error', 'Telegram Premium or an available transcription trial is required', until)
            await collector.db.set_status('transcription_retry_at', str(until))
            await collector.db.set_status('transcription_status', 'Telegram Premium or an available transcription trial is required; retrying tomorrow')
        except (errors.BadRequestError, errors.ForbiddenError) as error:
            # RPC exception text can contain private data; expose only known reason codes.
            reasons = {'MSG_VOICE_MISSING': 'Telegram does not support transcription for this media',
                       'MSG_VOICE_TOO_LONG': 'Media exceeds Telegram transcription duration limits',
                       'MSG_ID_INVALID': 'The original message is no longer available',
                       'TRANSCRIPTION_FAILED': 'Telegram could not recognize this recording'}
            reason = reasons.get(getattr(error, 'message', ''), type(error).__name__)
            await retry(collector, item, 'unavailable', reason)
        except (SQLAlchemyError, OSError) as error:
            if isinstance(error, SQLAlchemyError) or getattr(error, 'errno', None) in {1, 5, 13, 28, 30, 122}:
                raise
            await retry(collector, item, 'error', type(error).__name__, time.time() + min(3600, 2 ** min(item.attempts + 2, 12)))
        except Exception as error:
            await retry(collector, item, 'error', type(error).__name__, time.time() + min(3600, 2 ** min(item.attempts + 2, 12)))
        await asyncio.sleep(2)
