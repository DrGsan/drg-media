"""One persistent MTProto reader, bounded chronological backfill and reconciliation."""
import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import subprocess
import tempfile
import time
import uuid
from contextlib import suppress, ExitStack
from contextvars import ContextVar
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

import httpx
from sqlalchemy import select, and_
from telethon import TelegramClient, events, utils, errors, functions, types
from .db import groups, messages, media, outbox, people, emoji_assets

log = logging.getLogger("family_photo.worker")
media_transfer = ContextVar("media_transfer", default=False)
history_group = ContextVar("history_group", default=None)
download_item = ContextVar("download_item", default=None)
MAX_FILE_FAILURES = 5
HISTORY_CHECK_SECONDS = 300
PROFILE_CHECK_SECONDS = 300


class DownloadDeferred(Exception):
    """An untouched file gets another turn after other queued media."""
    def __init__(self, reason='TelegramRateLimit'):
        self.reason=reason


class DownloadStopped(Exception):
    """A file reached its persistent failure limit; do not retry it."""


def transport_failure(error):
    return isinstance(error, (ConnectionError, OSError, TimeoutError, httpx.TransportError,
        errors.ServerError, errors.RpcCallFailError, errors.TimedOutError))


def custom_emoji_id(message):
    if not message or getattr(message,'document',None) or getattr(message,'photo',None):return None
    text=getattr(message,'raw_text','') or ''
    if not text:return None
    entities=[entity for entity in getattr(message,'entities',None) or [] if isinstance(entity,types.MessageEntityCustomEmoji) and not isinstance(message,EmojiMessage)]
    if len(entities)==1 and entities[0].offset==0 and entities[0].length==len(text.encode('utf-16-le'))//2:
        return entities[0].document_id


class EmojiMessage:
    def __init__(self,message,document):
        self.source=message;self.document=self.sticker=document;self.photo=self.voice=None;self.raw_text=''
        extension={'application/x-tgsticker':'.tgs','video/webm':'.webm','image/webp':'.webp'}.get(document.mime_type,'')
        self.file=SimpleNamespace(mime_type=document.mime_type,size=document.size,name='emoji'+extension,ext=extension)

    def __getattr__(self,name):return getattr(self.source,name)


def telegram_client(config):
    if not config.api_id or not config.api_hash:
        raise ValueError("FP_TELEGRAM_API_ID and FP_TELEGRAM_API_HASH are required")
    from .limits import LimitedTelegramClient
    return LimitedTelegramClient(config)


async def normalize(message):
    sender = await message.get_sender()
    profile = {key: getattr(sender, key, '') or '' for key in ('first_name', 'last_name', 'username')}
    # A contact entity may contain this account's own saved alias. Do not publish it.
    if getattr(sender, 'contact', False):
        profile['first_name'] = profile['last_name'] = ''
    name = utils.get_display_name(sender) if sender else "Неизвестный отправитель"
    edited = getattr(message, "edit_date", None)
    album = getattr(message, "grouped_id", None)
    result = {"id": message.id, "created": int(message.date.timestamp()),
        "edited": int(edited.timestamp()) if edited else 0,
        "sender_id": str(getattr(message, "sender_id", None) or ""), "sender": name or "Неизвестный отправитель",
        "text": getattr(message, "raw_text", "") or "", "album_id": str(album) if album else None,
        "reply_to": getattr(message, "reply_to_msg_id", None)}
    if any(profile.values()):
        name = ' '.join(profile[k].strip() for k in ('first_name', 'last_name') if profile[k].strip())
        username = profile['username'].lstrip('@')
        result['sender'] = (name + (' (@' + username + ')' if name else '@' + username) if username else name)
    elif getattr(sender, 'contact', False):
        result['sender'] = 'Автор не уточнён'
    result['custom_emojis']=[{'offset':entity.offset,'length':entity.length,'document_id':str(entity.document_id)} for entity in getattr(message,'entities',None) or [] if isinstance(entity,types.MessageEntityCustomEmoji) and not isinstance(message,EmojiMessage)]
    reactions=getattr(message,'reactions',None);result['reactions']=[]
    def reaction_key(value):
        return ('emoji',value.emoticon) if isinstance(value,types.ReactionEmoji) else ('custom',str(value.document_id)) if isinstance(value,types.ReactionCustomEmoji) else ('paid','⭐')
    for count in getattr(reactions,'results',None) or []:
        kind,value=reaction_key(count.reaction)
        recent=[{'id':str(utils.get_peer_id(item.peer_id))} for item in getattr(reactions,'recent_reactions',None) or [] if reaction_key(item.reaction)==(kind,value)]
        result['reactions'].append({('document_id' if kind=='custom' else 'emoji'):value,'count':count.count,'people':recent,'complete':len(recent)>=count.count})
    photo_object, document, file = (getattr(message, field, None) for field in ("photo", "document", "file"))
    sticker = getattr(message, 'sticker', None)
    voice = getattr(message, 'voice', None)
    video_note = bool(getattr(message,'video_note',None)) or any(isinstance(a,types.DocumentAttributeVideo) and getattr(a,'round_message',False) for a in getattr(document,'attributes',[]) or [])
    animated = not sticker and any(isinstance(attr, types.DocumentAttributeAnimated)
        for attr in getattr(document, 'attributes', []) or [])
    if animated: result['animation_verified'] = True
    animation = animated or not sticker and getattr(file, 'mime_type', None) == 'image/gif'
    if voice or video_note:
        result['content_type'] = 'video_note' if video_note else 'voice'
        result['duration'] = int(getattr(file, 'duration', 0) or 0)
    if voice:
        from .recordings import telegram_waveform
        raw=next((getattr(a,'waveform',None) for a in getattr(document,'attributes',[]) if isinstance(a,types.DocumentAttributeAudio)),None)
        wave=telegram_waveform(raw)
        if wave:result['waveform']=wave
    if sticker:
        result['content_type'] = 'sticker'
        if isinstance(message,EmojiMessage):result['custom_emoji']=True
        result['sticker_emoji'] = next((getattr(a, 'alt', '') for a in getattr(document, 'attributes', [])
                                       if hasattr(a, 'alt')), '')
    if photo_object or (document and file and (sticker or voice or (file.mime_type or "").startswith(("image/", "video/", "audio/")))):
        photo = bool(photo_object)
        identity = str((photo_object or document).id)
        mime = "image/jpeg" if photo else (file.mime_type or "application/octet-stream")
        result["media"] = {"identity": identity, "kind": "video_note" if video_note else "voice" if voice else "audio" if mime.startswith("audio/") else "sticker" if sticker else "animation" if animation else "photo" if mime.startswith("image/") else "video",
            "mime": mime, "name": (file.name or f"{message.id}{file.ext or ''}")[:255], "size": file.size or 0}
    return result


def ephemeral(message):
    return bool(getattr(getattr(message, "media", None), "ttl_seconds", None))


from .telegram_details import TelegramDetails
from .group_management import GroupInactive

class Synchronizer(TelegramDetails):
    def __init__(self, config, archive, client):
        self.config, self.archive, self.client = config, archive, client
        if not getattr(archive,'site_messaging',None):
            from .site_messaging import SiteMessaging
            SiteMessaging(archive,config)
        from .progress import Progress
        self.progress = Progress(config, archive)
        self.missing = {}
        self.sources = OrderedDict()
        self.emoji_documents = OrderedDict()
        self.details_checked={};self.people_checked={}
        self.catching_up = set()
        from .downloads import DownloadCache
        self.download_cache = DownloadCache(archive.objects)

    async def preload_emoji(self,items):
        ids=sorted({eid for item in items if (eid:=custom_emoji_id(item)) is not None and eid not in self.emoji_documents})
        for start in range(0,len(ids),100):
            batch=ids[start:start+100]
            result=await self.client(functions.messages.GetCustomEmojiDocumentsRequest(document_id=batch))
            found={document.id:document for document in result if isinstance(document,types.Document)}
            for eid in batch:self.emoji_documents[eid]=found.get(eid)
            while len(self.emoji_documents)>200:self.emoji_documents.popitem(last=False)

    async def prepare_source(self,message):
        eid=custom_emoji_id(message)
        if eid is not None:
            if eid not in self.emoji_documents:await self.preload_emoji([message])
            document=self.emoji_documents.get(eid)
            if document:return EmojiMessage(message,document)
        return message

    async def download_media(self, source, target):
        """Resume checked chunks after a pause, disconnect or worker restart."""
        location = getattr(source, 'document', None) or getattr(source, 'photo', None)
        if location is None:
            raise ValueError('media_location_unavailable')
        expected = getattr(getattr(source, 'file', None), 'size', None) or None
        if isinstance(location,types.Photo) and location.sizes:
            # Message.file.size is the heaviest thumbnail, while iter_download
            # selects the last photo variant. Their byte lengths can differ.
            size=location.sizes[-1]
            expected=getattr(size,'size',None) or max(getattr(size,'sizes',[]) or [0]) or len(getattr(size,'bytes',b'')) or None
        if media_transfer.get(): self.progress.stage('download', expected or 0)
        with self.download_cache.open(location, expected, Path(target)) as output:
            from .limits import CHUNK_SIZE
            request_size = CHUNK_SIZE
            if media_transfer.get(): self.progress.transfer(output.offset, expected)
            if expected is not None and output.offset == expected:
                return self.download_cache.materialize(output, Path(target))
            stream = self.client.iter_download(location, file_size=expected, request_size=request_size,
                                               offset=output.offset).__aiter__()
            pauses = 0
            try:
                while expected is None or output.offset < expected:
                    current = download_item.get()
                    gid = current.get('group_id') if isinstance(current, dict) else history_group.get()
                    if gid: self.ensure_active(gid)
                    try:
                        chunk = await anext(stream)
                    except StopAsyncIteration:
                        break
                    except (errors.FloodWaitError, errors.FloodPremiumWaitError) as error:
                        item=download_item.get()
                        if item and self.record_file_failure(item,error):
                            raise DownloadStopped() from error
                        # Repeated rejections need a quieter connection, not a
                        # tight loop at the minimum server-provided interval.
                        await self.wait_flood(max(error.seconds, min(300, max(1, error.seconds)*2**min(pauses,8))))
                        pauses += 1
                        if not output.offset and pauses>=3:raise DownloadDeferred(type(error).__name__) from None
                        continue
                    output.write(chunk)
                    item=download_item.get()
                    if item and item['attempts']:
                        with self.archive.transaction() as c:
                            self.archive.require_active(c, item["group_id"])
                            c.execute(media.update().where(media.c.id==item['id'],media.c.identity==item['identity'],
                                media.c.state!='paused').values(attempts=0))
                        item['attempts']=0
                    if media_transfer.get(): self.progress.transfer(output.offset, expected)
                    pauses = 0
                if expected is not None and output.offset != expected:
                    raise ValueError('incomplete_media_download')
                return self.download_cache.materialize(output, Path(target))
            finally:
                close = getattr(stream, 'close', None) or getattr(stream, 'aclose', None)
                if close:
                    await close()

    async def sync_group_profile(self, gid):
        self.ensure_active(gid)
        profile = self.archive.group_profile(gid)
        if profile.get('checked_at', 0) > time.time() - PROFILE_CHECK_SECONDS:
            return
        self.archive.save_group_profile(gid, checked_at=int(time.time()))
        from .connection import record
        try:
            entity = await self.client.get_entity(int(self.config.telegram_id(gid)))
        except (errors.FloodWaitError,errors.FloodPremiumWaitError):raise
        except (errors.ChatAdminRequiredError,errors.ChannelPrivateError,errors.ChatWriteForbiddenError):
            record(self.archive,gid,'access_lost');raise
        except Exception:
            record(self.archive,gid,'unavailable');raise
        migrated=getattr(entity,'migrated_to',None)
        if migrated:
            record(self.archive,gid,'migrated',migrated_to=utils.get_peer_id(types.PeerChannel(migrated.channel_id)))
            self.archive.update_group(gid,state='migrated')
            return
        if getattr(entity,'left',False) or getattr(entity,'deactivated',False) or isinstance(entity,(types.ChatForbidden,types.ChannelForbidden)):
            record(self.archive,gid,'access_lost');self.archive.update_group(gid,state='access_lost');return
        record(self.archive,gid,'working')
        title = getattr(entity, 'title', None)
        if title:
            self.archive.add_group(gid, title)
        identity = str(getattr(getattr(entity, 'photo', None), 'photo_id', '') or '')
        if not identity:
            self.archive.save_group_profile(gid, avatar_key=None, avatar_identity=None, checked_at=int(time.time()))
            return
        if identity != profile.get('avatar_identity') or not profile.get('avatar_key'):
            from .limits import read_limits
            if read_limits(self.config).get('pauses',{}).get('media',{}).get('until',0)>time.time():
                return  # A paused avatar must not block incoming text/history.
            with self.archive.objects.workspace() as temporary:
                path = await self.client.download_profile_photo(entity, file=str(Path(temporary) / 'avatar.jpg'), download_big=False)
                if not path:
                    raise ValueError('avatar_unavailable')
                self.archive.store_avatar(gid, path, identity)
        self.archive.save_group_profile(gid, checked_at=int(time.time()))

    async def apply(self, gid, message, reaction_people=None):
        self.ensure_active(gid)
        if isinstance(getattr(message,'action',None),types.MessageActionChatMigrateTo):
            from .connection import record
            record(self.archive,gid,'migrated',migrated_to=utils.get_peer_id(types.PeerChannel(message.action.channel_id)))
            self.archive.update_group(gid,state='migrated')
            return
        if not message or not getattr(message, "date", None):
            return
        if getattr(message, 'action', None):
            self.archive.delete_messages(gid, [message.id])
            return
        with self.archive.engine.connect() as c:
            previous = c.execute(select(messages).where(and_(messages.c.group_id == gid,
                messages.c.id == message.id))).mappings().first()
            item = c.execute(select(media).where(and_(media.c.group_id == gid,
                media.c.message_id == message.id))).mappings().first()
        # A binding is an explicit operator decision, never a title-based guess.
        # Refuse an inconsistent source before changing the imported archive.
        if previous and str(previous['sender_id']).startswith('export:') and not previous['deleted']:
            if previous['created'] != int(message.date.timestamp()):
                raise ValueError('archive_binding_message_date_mismatch')
        if ephemeral(message):
            # Never turn self-destructing content into a permanent archive.
            self.archive.delete_messages(gid, [message.id])
            return
        message = await self.prepare_source(message)
        data = await normalize(message)
        ttl = getattr(message, "ttl_period", None)
        if ttl:
            data["expires"] = int(message.date.timestamp()) + ttl
            if data["expires"] <= time.time():
                self.archive.delete_messages(gid, [message.id])
                return
        if reaction_people is not None:
            for reaction in data.get('reactions',[]):
                key='custom:'+reaction['document_id'] if reaction.get('document_id') else reaction.get('emoji')
                if key in reaction_people:
                    reaction['people']=reaction_people[key];reaction['complete']=len(reaction['people'])>=reaction['count']
        if previous and reaction_people is None and previous.get('content_key'):
            retained=self.archive.objects.read_json(previous['content_key']).get('reactions',[])
            for reaction in data.get('reactions',[]):
                old=next((r for r in retained if r.get('emoji')==reaction.get('emoji') and r.get('document_id')==reaction.get('document_id') and r.get('count')==reaction.get('count') and r.get('complete')),None)
                if old and {p['id'] for p in reaction['people']}<={p['id'] for p in old['people']}:reaction.update(people=old['people'],complete=True)
        incoming = data.get('media')
        if incoming and incoming['mime']=='image/gif' and not data.get('animation_verified') and item and item['state']=='ready' and item['object_key'] and item['identity']==incoming['identity']:
            from .animations import verify_gif
            header=b''.join(self.archive.objects.read(item['object_key'],0,15))
            if verified:=verify_gif(self.archive,dict(item),header):
                incoming.update(verified)
                data['animation_verified']=verified['kind']=='animation'
        if item and item['identity'].startswith('export:') and item['state'] == 'ready' and incoming:
            # Telegram marks media replacement as an edit. An unedited message
            # with the same ID/date/type/size still contains the exported file.
            same = (not getattr(message, 'edit_date', None) and previous
                    and previous['created'] == data['created']
                    and item['kind'] == incoming['kind'] and item['size'] == incoming['size'])
            if not same:
                # Edited or differently encoded exports need a byte comparison.
                # Never equate two files merely because their names match.
                with self.archive.objects.workspace('verify-export-') as temporary:
                    path = await self.download_media(message, Path(temporary) / 'original')
                    if not path:
                        raise ValueError('export_verification_download_unavailable')
                    with open(path, 'rb') as source:
                        same = item['identity'] == 'export:' + hashlib.file_digest(source, 'sha256').hexdigest()
            if same:
                with self.archive.transaction() as c:
                    self.archive.require_active(c, gid)
                    c.execute(media.update().where(and_(media.c.id == item['id'],
                        media.c.identity == item['identity'])).values(identity=incoming['identity']))
        self.archive.ingest(gid, data, confirmed_ttl=bool(ttl))
        self.archive.queue_emoji(gid,[entry['document_id'] for entry in data.get('custom_emojis',[])]+[entry['document_id'] for entry in data.get('reactions',[]) if entry.get('document_id')])
        source_key = (gid, message.id)
        self.sources.pop(source_key, None)
        if incoming:
            self.sources[source_key] = message
            while len(self.sources) > 100:
                self.sources.popitem(last=False)

    async def scan(self, gid, batch=100):
        self.ensure_active(gid)
        state = self.archive.group(gid)
        if state["retry_at"] > time.time() or (gid not in self.catching_up and state["history_complete"] and state["last_sync"] > time.time() - HISTORY_CHECK_SECONDS):
            return 0
        latest = await self.client.get_messages(int(self.config.telegram_id(gid)), limit=1)
        if not latest or latest[0].id <= state["cursor"]:
            self.archive.update_group(gid, history_complete=True, last_sync=int(time.time()), state="online")
            self.catching_up.discard(gid)
            return
        count, last = 0, state["cursor"]
        async for message in self.client.iter_messages(int(self.config.telegram_id(gid)), min_id=state["cursor"],
                max_id=latest[0].id + 1, reverse=True, limit=batch, wait_time=self.config.page_delay):
            self.ensure_active(gid)
            await self.apply(gid, message)
            # Advance only after durable text and media work records exist.
            last = message.id
            self.archive.update_group(gid, cursor=last, state="backfilling")
            count += 1
        complete = count < batch
        self.archive.update_group(gid, history_complete=complete, retry_at=0,
            last_sync=int(time.time()), state="online" if complete else "backfilling")
        if complete: self.catching_up.discard(gid)
        return count

    async def can_read(self, gid):
        self.ensure_active(gid)
        entity = await self.client.get_entity(int(self.config.telegram_id(gid)))
        if getattr(entity, "left", False) or getattr(entity, "deactivated", False):
            from .connection import record
            migrated=getattr(entity,'migrated_to',None)
            record(self.archive,gid,'migrated',migrated_to=utils.get_peer_id(types.PeerChannel(migrated.channel_id))) if migrated else record(self.archive,gid,'access_lost')
            return False
        permission = await self.client.get_permissions(entity, "me")
        participant = getattr(permission, "participant", None)
        readable=not (getattr(permission, "has_left", False) or
                    getattr(getattr(participant, "banned_rights", None), "view_messages", False))
        if not readable:
            from .connection import record
            record(self.archive,gid,'access_lost')
        return readable

    async def reconcile(self, gid, batch=100):
        self.ensure_active(gid)
        state = self.archive.group(gid)
        if state["retry_at"] > time.time():
            return
        if not state["history_complete"] or (state["last_reconcile"] and not state["reconcile_cursor"] and
                state["last_reconcile"] > time.time() - self.config.reconcile_seconds):
            return
        if not await self.can_read(gid):
            self.archive.update_group(gid, state="access_lost", retry_at=int(time.time()) + 300)
            return
        registry = getattr(self.archive, "registry", None)
        repair = bool(registry and registry.read()["groups"].get(gid, {}).get("ttl_repair"))
        with self.archive.engine.connect() as c:
            rows = list(c.execute(select(messages).where(and_(messages.c.group_id == gid,
                messages.c.id > state["reconcile_cursor"], (messages.c.deleted == False) | ((messages.c.created == 0) if repair else False))
                ).order_by(messages.c.id).limit(batch)).mappings())
        if not rows:
            if repair:
                with registry.edit() as (_, control): control["groups"].get(gid, {}).pop("ttl_repair", None)
            self.archive.update_group(gid, reconcile_cursor=0, last_reconcile=int(time.time()))
            return
        fetched = await self.client.get_messages(int(self.config.telegram_id(gid)), ids=[row["id"] for row in rows])
        await self.preload_emoji(fetched)
        existing = {m.id: m for m in fetched if m and getattr(m, "date", None)}
        # Request failures escape before any missing messages are changed.
        if not await self.can_read(gid):
            self.archive.update_group(gid, state="access_lost", retry_at=int(time.time()) + 300)
            return
        for row in rows:
            if row["id"] in existing:
                await self.apply(gid, existing[row["id"]])
                with self.archive.transaction() as c:
                    self.archive.require_active(c, gid)
                    c.execute(messages.update().where(and_(messages.c.group_id == gid,
                        messages.c.id == row["id"])).values(missing_since=0))
            elif row["deleted"]:
                continue  # A missing tombstone remains deleted; only live TTL content can restore it.
            elif row["missing_since"] and row["missing_since"] < time.time() - 60:
                self.archive.delete_messages(gid, [row["id"]])
            else:
                with self.archive.transaction() as c:
                    self.archive.require_active(c, gid)
                    c.execute(messages.update().where(and_(messages.c.group_id == gid,
                        messages.c.id == row["id"])).values(missing_since=int(time.time())))
        self.archive.update_group(gid, reconcile_cursor=rows[-1]["id"])

    def record_file_failure(self,item,error,*,count=True):
        registry = getattr(self.archive, "registry", None)
        if registry and not registry.active(item["group_id"]): return True
        counted=count and not transport_failure(error)
        reason=str(error) if type(error) is ValueError and str(error) in {'source_unavailable','media_location_unavailable','incomplete_media_download','download_unavailable'} else type(error).__name__
        with self.archive.transaction() as c:
            current=c.execute(select(media).where(media.c.id==item['id'])).mappings().first()
            if not current or current['identity']!=item['identity'] or current['state']=='paused':return True
            failures=current['attempts']+int(counted)
            stopped=counted and failures>=MAX_FILE_FAILURES
            flood=isinstance(error,(errors.FloodWaitError,errors.FloodPremiumWaitError))
            delay=error.seconds+5 if flood else min(3600,30*2**min(failures,7))
            c.execute(media.update().where(media.c.id==item['id']).values(
                state='paused' if stopped else 'pending' if flood else 'failed',attempts=failures,
                error=('auto:' if stopped else '')+reason,retry_at=0 if stopped else int(time.time())+delay))
        item['attempts']=failures
        if stopped:log.warning('File download stopped after %s failures (media %s)',failures,item['id'])
        return stopped

    async def downloads(self, limit=20):
        with self.archive.engine.connect() as c:
            queue = [dict(r) for r in c.execute(select(media).join(groups, groups.c.id == media.c.group_id).where(
                and_(media.c.group_id.in_(self.config.archive_group_ids), media.c.state.not_in(("ready", "paused")), media.c.retry_at <= int(time.time()),
                     groups.c.retry_at <= int(time.time()))).order_by(media.c.attempts,media.c.created.desc(), media.c.message_id.desc()).limit(limit)).mappings()]
        for item in queue:
            registry = getattr(self.archive, "registry", None)
            if registry and not registry.active(item["group_id"]): continue
            token = media_transfer.set(True)
            item_token = download_item.set(item)
            stage='source'
            self.progress.begin(item)
            try:
                source = self.sources.pop((item['group_id'], item['message_id']), None)
                if source is None:
                    # A bounded history read also works after restart. Reuse the
                    # event/backfill object when available instead of asking for
                    # the same per-file metadata again.
                    async for candidate in self.client.iter_messages(int(self.config.telegram_id(item['group_id'])),
                            min_id=item['message_id']-1, max_id=item['message_id']+1, limit=1):
                        source = candidate
                        break
                if not source:
                    # A single missing file is not proof of deletion. Reconciliation confirms it.
                    raise ValueError("source_unavailable")
                source = await self.prepare_source(source)
                normalized = await normalize(source)
                if normalized.get("media", {}).get("identity") != item["identity"]:
                    await self.apply(item["group_id"], source)
                    continue
                if ephemeral(source):
                    self.archive.delete_messages(item["group_id"], [source.id])
                    continue
                with self.archive.objects.workspace("download-") as temporary, ExitStack() as uploads:
                    if item.get('object_key'):
                        stage='storage'
                        self.progress.stage('restoring', item['size'])
                        path = Path(temporary) / 'original'
                        with path.open('wb') as output:
                            for block in self.archive.objects.read(item['object_key']):
                                output.write(block)
                                self.progress.transfer(output.tell())
                    else:
                        stage='telegram'
                        path = await self.download_media(source, Path(temporary) / 'original')
                    if not path:
                        raise ValueError("download_unavailable")
                    gif_header=None;gif_item=None
                    if normalized.get('media',{}).get('mime')=='image/gif' and not normalized.get('animation_verified'):
                        from .animations import image_type
                        with Path(path).open('rb') as stream:gif_header=stream.read(16)
                        gif_item=dict(item)
                        if verified:=image_type(gif_header):item.update(verified)
                    key, thumb_key = item.get('object_key') or uploads.enter_context(self.archive.upload('media')), None
                    stage='storage'
                    if not item.get('object_key'):
                        self.progress.stage('upload', Path(path).stat().st_size)
                        await asyncio.to_thread(self.archive.objects.put_file, key, path, item['mime'],
                                                lambda size: self.progress.transfer(size, delta=True))
                    # Persist the original before a video-sticker conversion;
                    # a failed preview retries locally, without another TG download.
                    if item['kind']=='sticker' and item['mime']=='video/webm':
                        if not self.archive.attach_download(item,key,None,Path(path).stat().st_size):continue
                    thumbnail = Path(temporary) / ("preview.m4a" if item["kind"] in {"voice", "audio"} else "preview.webp" if item['kind']=='sticker' else "preview.jpg")
                    stage='preview'
                    self.progress.stage('preview')
                    try:
                        await asyncio.to_thread(make_thumbnail, Path(path), thumbnail, item["kind"])
                        if thumbnail.exists():
                            candidate = uploads.enter_context(self.archive.upload('thumbnails'))
                            await asyncio.to_thread(self.archive.objects.put_file, candidate, thumbnail, "audio/mp4" if item["kind"] in {"voice", "audio"} else "image/webp" if item['kind']=='sticker' else "image/jpeg")
                            thumb_key = candidate
                    except Exception:
                        if item['kind']=='sticker' and item['mime']=='video/webm':raise
                        pass  # Browser can still open/download the original.
                    attached=self.archive.attach_download(item, key, thumb_key, Path(path).stat().st_size)
                    if attached and gif_item:
                        from .animations import verify_gif
                        verify_gif(self.archive,gif_item|{'object_key':key},gif_header)
            except GroupInactive:
                pass
            except DownloadStopped:
                pass
            except DownloadDeferred as deferred:
                with self.archive.transaction() as c:
                    c.execute(media.update().where(media.c.id==item['id']).values(state='pending',
                        attempts=item['attempts'],error=deferred.reason,retry_at=int(time.time())+300))
            except (errors.FloodWaitError, errors.FloodPremiumWaitError) as error:
                # A shared metadata/connection pause is not a failed file fetch.
                self.record_file_failure(item,error,count=False)
                await self.wait_flood(error.seconds)
            except Exception as error:
                if isinstance(error,(errors.FileReferenceExpiredError,errors.FilerefUpgradeNeededError)):
                    eid=custom_emoji_id(getattr(source,'source',source))
                    self.emoji_documents.pop(eid,None)
                self.record_file_failure(item,error,count=stage in {'source','telegram'})
            finally:
                self.progress.finish()
                download_item.reset(item_token)
                media_transfer.reset(token)
            await asyncio.sleep(self.config.page_delay)

    def register(self):
        if getattr(self, '_registered', False): return

        @self.client.on(events.NewMessage())
        async def new(event):
            gid = self.config.archive_id(event.chat_id)
            if gid not in self.config.archive_group_ids: return
            state = self.archive.group(gid)
            if state and state["history_complete"] and gid not in self.catching_up:
                await self.apply(gid, event.message)
            # New messages never advance the chronological scan cursor.

        @self.client.on(events.MessageEdited())
        async def edited(event):
            gid = self.config.archive_id(event.chat_id)
            if gid not in self.config.archive_group_ids: return
            state = self.archive.group(gid)
            with self.archive.engine.connect() as c:
                known = c.execute(select(messages.c.id).where(and_(messages.c.group_id == gid,
                    messages.c.id == event.message.id))).first()
            if known or (state and state["history_complete"] and gid not in self.catching_up):
                await self.apply(gid, event.message)

        @self.client.on(events.MessageDeleted())
        async def deleted(event):
            gid = self.config.archive_id(event.chat_id)
            if gid in self.config.archive_group_ids:
                self.archive.delete_messages(gid, event.deleted_ids)
            # Ambiguous basic-group deletes are confirmed by reconciliation.
        @self.client.on(events.Raw(types=types.UpdateMessageReactions))
        async def reacted(update):
            gid=self.config.archive_id(utils.get_peer_id(update.peer))
            if gid in self.config.archive_group_ids:await self.sync_recent_details(gid,[update.msg_id])
        self._registered = True

    async def run(self):
        self.state = "connecting"
        self.progress.cleaned(self.archive.objects.clean_temporary(startup=True))
        maintenance = asyncio.create_task(self.maintenance_loop(), name='drg-media-cleanup')
        async def heartbeat():
            while True:
                self.archive.heartbeat(self.state)
                self.progress.save(True)
                await asyncio.sleep(20)
        pulse = asyncio.create_task(heartbeat())
        try:
            while True:
                try:
                    await self._run()
                    break
                except errors.FloodWaitError as error:
                    await self.wait_flood(error.seconds)
        finally:
            pulse.cancel()
            maintenance.cancel()
            with suppress(asyncio.CancelledError): await maintenance
            with suppress(asyncio.CancelledError):
                await pulse

    async def _run(self):
        for _, target in self.config.group_bindings:
            if not self.archive.group(target):
                raise ValueError('bound_archive_does_not_exist')
        # Catch up without erasing the persisted completeness status on restart.
        # New live IDs wait for the chronological scan, so gaps are not skipped.
        self.catching_up = set(self.config.archive_group_ids)
        self.register()
        from .limits import read_limits
        until=read_limits(self.config).get('pauses',{}).get('all',{}).get('until',0)
        if until>time.time():await self.wait_flood(until-time.time())
        await self.client.connect()
        if not await self.client.is_user_authorized():
            self.state = "login_required"
            self.archive.heartbeat("login_required")
            raise RuntimeError("Telegram login required; run drg-media login")
        me = await self.client.get_me()
        with tempfile.NamedTemporaryFile(mode='w', dir=self.config.data_dir, prefix='.telegram-account-', delete=False) as temporary:
            json.dump({'id': me.id}, temporary)
            account_path = Path(temporary.name)
        account_path.replace(self.config.data_dir / '.telegram-account.json')
        self.state = "online"
        await self.resolve_groups()
        for gid in self.config.archive_group_ids:
            if not self.archive.group(gid):
                self.archive.add_group(gid, gid)
        await self.client.catch_up()
        with self.archive.transaction() as c:
            c.execute(outbox.update().where(outbox.c.state == "sending").values(state="uncertain",
                error="worker_interrupted"))
        # Catch-up events repair edits; the persisted reconciliation cursor
        # repairs older feature data. Restart must not reread the latest 50.
        downloads = asyncio.create_task(self.media_loop(),name='telegram-media-downloads')
        try:
            await self.sync_loop()
        finally:
            downloads.cancel()
            with suppress(asyncio.CancelledError):await downloads

    async def resolve_groups(self):
        missing = set()
        for gid in self.config.archive_group_ids:
            try:
                # Telethon's persisted session already knows these access hashes.
                await self.client.get_input_entity(int(self.config.telegram_id(gid)))
            except ValueError:
                missing.add(gid)
        if missing:
            # One initial discovery when the session lacks an access hash.
            # Stop as soon as all requested groups are found; no message history.
            async for dialog in self.client.iter_dialogs():
                gid = self.config.archive_id(dialog.id)
                if gid in missing:
                    kind = 'supergroup' if getattr(dialog.entity, 'megagroup', False) else 'basic'
                    self.archive.add_group(gid, dialog.name, kind=kind)
                    missing.remove(gid)
                    if not missing: break
            if missing: raise ValueError('configured_telegram_group_unavailable')

    def clean_downloads(self):
        with self.archive.engine.connect() as c:
            ready = set(c.execute(select(media.c.identity).where(and_(
                media.c.state == 'ready', media.c.object_key.is_not(None)))).scalars())
        return self.download_cache.clean(ready)

    def ensure_active(self, gid):
        registry = getattr(self.archive, 'registry', None)
        if registry and not registry.active(gid):
            from .group_management import GroupInactive
            raise GroupInactive('group_inactive')

    async def discover_groups(self):
        registry = getattr(self.archive, 'registry', None)
        if not registry: return
        discovery = registry.read()['discovery']
        if discovery['requested'] <= discovery['checked']: return
        from .limits import read_limits
        if read_limits(self.config).get('pauses', {}).get('all', {}).get('until', 0) > time.time(): return
        items = []
        try:
            async for dialog in self.client.iter_dialogs():
                if dialog.is_group and not getattr(dialog.entity, 'left', False) and not getattr(dialog.entity, 'deactivated', False):
                    items.append({'id': str(dialog.id), 'title': dialog.name,
                                  'kind': 'supergroup' if getattr(dialog.entity, 'megagroup', False) else 'basic'})
        except (errors.FloodWaitError, errors.FloodPremiumWaitError) as error:
            registry.publish_discovery([], 'telegram_rate_limited')
            await self.wait_flood(error.seconds)
            return
        except Exception:
            registry.publish_discovery([], 'telegram_unavailable')
            return
        registry.publish_discovery(items)

    async def maintenance_loop(self):
        while True:
            try:
                await asyncio.to_thread(self.archive.expire_messages)
                registry = getattr(self.archive, 'registry', None)
                if registry:
                    active=set(self.config.archive_group_ids)
                    for key in list(self.sources):
                        if key[0] not in active:self.sources.pop(key,None)
                    await asyncio.to_thread(registry.maintenance)
                await self.discover_groups()
                result = await asyncio.to_thread(self.archive.collect_garbage)
                result.update(await asyncio.to_thread(self.archive.objects.clean_temporary))
                checkpoints = await asyncio.to_thread(self.clean_downloads)
                for field, value in checkpoints.items(): result[field] = result.get(field, 0) + value
                self.progress.cleaned(result)
            except Exception as error:
                self.progress.cleaned({'errors': 1})
                log.warning('Cleanup postponed (%s)', type(error).__name__)
            await asyncio.sleep(5)

    async def media_loop(self):
        while True:
            try:
                await self.downloads()
                await self.download_details()
            except (errors.FloodWaitError,errors.FloodPremiumWaitError) as error:
                await self.wait_flood(error.seconds)
            except Exception as error:
                log.warning('Media work paused (%s)',type(error).__name__)
            await asyncio.sleep(self.config.page_delay)

    async def sync_loop(self):
        while True:
            self.archive.heartbeat(self.state)
            for gid in self.config.archive_group_ids:
                context = history_group.set(gid)
                try:
                    try:
                        await self.sync_group_profile(gid)
                    except errors.FloodWaitError:
                        raise
                    except Exception as error:
                        log.warning("Group profile refresh paused (%s)", type(error).__name__)
                    registry=getattr(self.archive,'registry',None)
                    connection=registry.read()['groups'].get(gid,{}).get('connection',{}) if registry else {}
                    if connection.get('state') in {'migrated','access_lost'}:continue
                    await self.scan(gid)
                    for enrich in (self.sync_people,self.sync_recent_details):
                        try:await enrich(gid)
                        except (errors.FloodWaitError,errors.FloodPremiumWaitError,errors.AuthKeyError,errors.UnauthorizedError):raise
                        except Exception as error:log.warning('Message details refresh paused (%s)',type(error).__name__)
                    await self.reconcile(gid)
                except errors.FloodWaitError as error:
                    await self.wait_flood(error.seconds)
                except GroupInactive:
                    continue
                except (errors.AuthKeyError, errors.UnauthorizedError):
                    self.state = "login_required"
                    self.archive.heartbeat("login_required")
                    raise
                except Exception as error:
                    from .connection import record
                    access=isinstance(error,(errors.ChatAdminRequiredError,errors.ChannelPrivateError,errors.ChatWriteForbiddenError))
                    record(self.archive,gid,'access_lost' if access else 'unavailable')
                    self.archive.update_group(gid, state="access_lost" if access else "unavailable", retry_at=int(time.time()) + 60)
                    log.warning("Group sync paused (%s)", type(error).__name__)
                finally:
                    history_group.reset(context)
            try:
                await send_outbox(self.config, self.archive)
            except errors.FloodWaitError as error:
                await self.wait_flood(error.seconds)
            except Exception as error:
                log.warning("Background work paused (%s)", type(error).__name__)
            self.archive.heartbeat(self.state)
            await asyncio.sleep(self.config.page_delay)

    async def wait_flood(self, seconds):
        from .limits import read_limits
        limits=read_limits(self.config)
        transfer = media_transfer.get()
        pauses=limits.get('pauses',{})
        until=max(pauses.get('all',{}).get('until',0),pauses.get('media',{}).get('until',0) if transfer else 0)
        seconds=max(seconds,until-time.time())
        if transfer: self.progress.wait(seconds)
        elif history_group.get(): self.progress.history(history_group.get(), time.time()+seconds+1)
        self.flood_waits=getattr(self,'flood_waits',0)+1
        self.state = "rate_limited"
        self.archive.heartbeat("rate_limited")
        end = time.monotonic() + seconds + 1
        try:
            while time.monotonic() < end:
                await asyncio.sleep(min(30, end - time.monotonic()))
                self.archive.heartbeat("rate_limited")
        finally:
            if transfer: self.progress.resume()
            elif history_group.get(): self.progress.history(history_group.get())
            self.flood_waits-=1
            if self.state!='login_required':self.state = "rate_limited" if self.flood_waits else "online"


def make_thumbnail(source, target, kind):
    from PIL import Image, ImageOps
    with source.open("rb") as stream: header = stream.read(6)
    if kind in {'voice', 'audio'}:
        # iOS supports AAC/M4A consistently; keep Telegram's original Ogg/Opus
        # untouched and store only the playable derivative in the existing slot.
        subprocess.run(['ffmpeg', '-nostdin', '-loglevel', 'error', '-y', '-i', str(source),
                        '-vn', '-map_metadata', '-1', '-c:a', 'aac', '-b:a', '96k', '-ac', '1',
                        '-ar', '48000', '-movflags', '+faststart', '-f', 'mp4', str(target)],
                       check=True, timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif kind == 'sticker':
        with source.open('rb') as stream:
            if stream.read(2)==b'\x1f\x8b':return
        try:
            with Image.open(source) as image:
                image.thumbnail((512,512));image.save(target,'WEBP');return
        except OSError:pass
        raw=subprocess.run(['ffmpeg','-nostdin','-loglevel','error','-c:v','libvpx-vp9','-i',str(source),
            '-an','-t','3','-vf','fps=24,scale=256:256:force_original_aspect_ratio=decrease,format=rgba,pad=256:256:(ow-iw)/2:(oh-ih)/2:color=black@0',
            '-f','rawvideo','-pix_fmt','rgba','pipe:1'],check=True,timeout=30,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL).stdout
        stride=256*256*4
        if not raw or len(raw)%stride:raise ValueError('Invalid sticker frames')
        frames=[Image.frombytes('RGBA',(256,256),raw[offset:offset+stride]) for offset in range(0,len(raw),stride)]
        frames[0].save(target,'WEBP',save_all=True,append_images=frames[1:],duration=42,loop=0,quality=85)
    elif kind in {"video","video_note"} or kind == "animation" and header not in (b"GIF87a", b"GIF89a"):
        # Telegram animations are commonly MP4; GIF originals use Pillow below.
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(source),
            "-frames:v", "1", "-vf", "scale=640:640:force_original_aspect_ratio=decrease", str(target)],
            check=True, timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        with Image.open(source) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail((640, 640))
            image.convert("RGB").save(target, "JPEG", quality=82)


async def send_outbox(config, archive, http=None):
    if not config.bot_token:
        return
    with archive.engine.connect() as c:
        row = c.execute(select(outbox).where(and_(outbox.c.state == "queued",
            outbox.c.retry_at <= int(time.time()), outbox.c.group_id.in_(config.archive_group_ids))).order_by(outbox.c.created).limit(1)).mappings().first()
    if not row:
        return
    # Persist intent before sending. An ambiguous outcome is never blindly retried.
    with archive.transaction() as c:
        archive.require_active(c, row["group_id"])
        result = c.execute(outbox.update().where(and_(outbox.c.id == row["id"],
            outbox.c.state == "queued")).values(state="sending"))
        if not result.rowcount:
            return
    state, error, mid, retry_at = "uncertain", None, None, 0
    try:
        from .site_messaging import render_message
        rendered,entities=render_message(archive.objects.read_json(row["content_key"])["text"],row["author"])
        body = {"chat_id": config.telegram_id(row["group_id"]), "text": rendered,"entities":entities}
        if row["reply_to"]:
            body["reply_parameters"] = {"message_id": row["reply_to"], "allow_sending_without_reply": False}
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as default:
            response = await (http or default).post(f"https://api.telegram.org/bot{config.bot_token}/sendMessage", json=body)
        data = response.json()
        if response.status_code == 200 and data.get("ok"):
            state, mid = "delivered", data["result"]["message_id"]
        elif response.status_code == 429:
            state, retry_at = "queued", int(time.time()) + max(1, int(data.get("parameters", {}).get("retry_after", 60)))
        elif 400 <= response.status_code < 500:
            state, error = "failed", "telegram_rejected"
        else:
            error = "delivery_unknown"
    except (httpx.ConnectError, httpx.ConnectTimeout):
        state, retry_at = "queued", int(time.time()) + 30
    except Exception:
        error = "delivery_unknown"
    with archive.transaction() as c:
        c.execute(outbox.update().where(outbox.c.id == row["id"]).values(
            state=state, message_id=mid, error=error, retry_at=retry_at))
