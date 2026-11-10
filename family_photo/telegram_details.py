"""Bounded enrichment alongside the existing resumable archive worker."""
import time
import logging
from pathlib import Path
from types import SimpleNamespace
from sqlalchemy import select
from telethon import functions,types,utils,errors
from .db import people,emoji_assets,messages

def public_name(entity):
    # The reader's saved contact names must never become shared author names.
    if getattr(entity,'contact',False):return '@'+entity.username if getattr(entity,'username',None) else 'Автор не уточнён'
    return utils.get_display_name(entity) or ('@'+entity.username if getattr(entity,'username',None) else 'Неизвестный отправитель')

class TelegramDetails:
    async def sync_people(self,gid):
        self.ensure_active(gid)
        stamp=int(time.time())
        if self.people_checked.get(gid,0)>stamp:return
        self.people_checked[gid]=stamp+300
        with self.archive.engine.connect() as c:
            rows=list(c.execute(select(messages.c.sender_id,messages.c.content_key).where(messages.c.group_id==gid,messages.c.deleted==False).order_by(messages.c.id.desc()).limit(50)).mappings())
            saved={row['sender_id']:dict(row) for row in c.execute(select(people).where(people.c.group_id==gid)).mappings()}
        needed={row['sender_id'] for row in rows if row['sender_id'].lstrip('-').isdigit()}
        for row in rows:
            content=self.archive.objects.read_json(row['content_key'])
            needed.update(str(person['id']) for reaction in content.get('reactions',[]) for person in reaction.get('people',[]))
        if not needed:return
        if not hasattr(self.client,'get_participants'):return
        entities=list(await self.client.get_participants(int(self.config.telegram_id(gid)),limit=100))
        found={str(utils.get_peer_id(entity)) for entity in entities}
        for sid in sorted(needed-found)[:5]:
            try:entities.append(await self.client.get_entity(int(sid)))
            except (errors.UserIdInvalidError,ValueError):continue
        downloads=0
        for entity in entities:
            self.ensure_active(gid)
            sid=str(utils.get_peer_id(entity))
            if sid not in needed:continue
            old=saved.get(sid,{});identity=str(getattr(getattr(entity,'photo',None),'photo_id','') or '')
            if identity==old.get('avatar_identity') and stamp-old.get('checked_at',0)<300:continue
            name=public_name(entity)
            if not identity:
                self.archive.save_person(gid,sid,name=name,avatar_key=None,avatar_identity='',checked_at=stamp);continue
            if identity==old.get('avatar_identity') and old.get('avatar_key'):
                self.archive.save_person(gid,sid,name=name,checked_at=stamp);continue
            if downloads>=5:continue
            downloads+=1
            with self.archive.objects.workspace() as temporary:
                path=await self.client.download_profile_photo(entity,file=str(Path(temporary)/'sender.jpg'),download_big=False)
                if path:self.archive.store_person_avatar(gid,sid,name,path,identity)
                else:self.archive.save_person(gid,sid,name=name,avatar_key=None,avatar_identity=identity,checked_at=stamp)

    async def sync_recent_details(self,gid,ids=None):
        self.ensure_active(gid)
        stamp=int(time.time())
        if ids is None and self.details_checked.get(gid,0)>stamp:return
        if ids is None:self.details_checked[gid]=stamp+60
        with self.archive.engine.connect() as c:
            query=select(messages.c.id).where(messages.c.group_id==gid,messages.c.deleted==False)
            if ids is not None:query=query.where(messages.c.id.in_(ids))
            known=[row[0] for row in c.execute(query.order_by(messages.c.id.desc()).limit(30))]
        if not known:return
        enriched=0
        for message in await self.client.get_messages(int(self.config.telegram_id(gid)),ids=known):
            if not message or not isinstance(message,types.Message):continue
            authors=None
            reactions=getattr(message,'reactions',None)
            total=sum(r.count for r in getattr(reactions,'results',None) or [])
            recent=len(getattr(reactions,'recent_reactions',None) or [])
            if getattr(reactions,'can_see_list',False) and total>recent and enriched<3:
                enriched+=1
                try:
                    response=await self.client(functions.messages.GetMessageReactionsListRequest(peer=int(self.config.telegram_id(gid)),id=message.id,limit=100))
                    profiles={str(utils.get_peer_id(person)):public_name(person) for person in [*response.users,*response.chats]}
                    authors={}
                    for item in response.reactions:
                        reaction=item.reaction;key=('custom:'+str(reaction.document_id)) if isinstance(reaction,types.ReactionCustomEmoji) else reaction.emoticon if isinstance(reaction,types.ReactionEmoji) else '⭐'
                        sid=str(utils.get_peer_id(item.peer_id));authors.setdefault(key,[]).append({'id':sid,'name':profiles.get(sid,'Участник Telegram')})
                except (errors.ChatAdminRequiredError,errors.MessageIdInvalidError):pass
            await self.apply(gid,message,reaction_people=authors)


    async def download_details(self):
        from .telegram import make_thumbnail
        with self.archive.engine.connect() as c:
            queue=[dict(row) for row in c.execute(select(emoji_assets).where(emoji_assets.c.group_id.in_(self.config.archive_group_ids),emoji_assets.c.state!='ready',emoji_assets.c.retry_at<=int(time.time())).limit(10)).mappings()]
        if not queue:return
        ids=sorted({int(row['document_id']) for row in queue})
        documents={document.id:document for document in await self.client(functions.messages.GetCustomEmojiDocumentsRequest(document_id=ids)) if isinstance(document,types.Document)}
        for row in queue:
            gid,eid=row['group_id'],row['document_id'];document=documents.get(int(eid))
            if gid not in self.config.archive_group_ids: continue
            if not document or document.size>4*1024*1024:
                self.archive.save_emoji(gid,eid,state='unavailable',retry_at=int(time.time())+21600);continue
            mime=document.mime_type
            if mime not in {'image/webp','image/png','application/x-tgsticker','video/webm'}:
                self.archive.save_emoji(gid,eid,state='unavailable',retry_at=int(time.time())+21600);continue
            # Defer failed assets; one bad expression must not block every other one.
            self.archive.save_emoji(gid,eid,retry_at=int(time.time())+300)
            try:
                with self.archive.objects.workspace() as temporary:
                    path=Path(temporary)/'emoji';source=SimpleNamespace(document=document,photo=None,file=SimpleNamespace(size=document.size))
                    await self.download_media(source,path)
                    if mime=='application/x-tgsticker':
                        from .stickers import read_animation
                        # Validation after storing below uses the same bounded decoder as serving.
                    thumb=Path(temporary)/'thumb.webp'
                    if mime=='video/webm':make_thumbnail(path,thumb,'sticker')
                    from contextlib import ExitStack
                    with ExitStack() as leases:
                        key=leases.enter_context(self.archive.upload('emojis'));self.archive.objects.put_file(key,path,mime)
                        if mime=='application/x-tgsticker':read_animation(self.archive.objects,key)
                        thumb_key=leases.enter_context(self.archive.upload('thumbnails')) if thumb.exists() else None
                        if thumb_key:self.archive.objects.put_file(thumb_key,thumb,'image/webp')
                        self.archive.save_emoji(gid,eid,mime=mime,object_key=key,thumb_key=thumb_key,state='ready',retry_at=0)
            except (errors.FloodWaitError,errors.FloodPremiumWaitError):raise
            except Exception:
                logging.getLogger(__name__).warning('Custom emoji download failed; deferred',exc_info=False)
