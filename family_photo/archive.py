import hashlib
import json
import time
import uuid
from contextlib import contextmanager

from sqlalchemy import select, and_, or_, func, case
from sqlalchemy.exc import IntegrityError
from .db import groups, messages, media, outbox, garbage, runtime, group_profiles, people, emoji_assets, expirations, export_files, export_jobs,site_uploads,site_sends
from .details import Details


class Archive(Details):
    def __init__(self, engine, objects):
        self.engine, self.objects = engine, objects

    @contextmanager
    def transaction(self):
        with self.engine.connect() as connection:
            if self.engine.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                connection.begin()
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def require_active(self, c, gid):
        if getattr(self, "registry", None): self.registry.require_write(c, gid)

    def gc(self, c, key):
        if not key:
            return
        if c.execute(select(garbage).where(garbage.c.key == key)).first():
            c.execute(garbage.update().where(garbage.c.key == key).values(created=0))
        else:
            c.execute(garbage.insert().values(key=key, created=0))

    def reserve(self, kind):
        """Durable upload intent: a crash cannot strand an untracked object."""
        key = kind + '/' + uuid.uuid4().hex
        with self.transaction() as c:
            c.execute(garbage.insert().values(key=key, created=int(time.time())))
        return key

    @contextmanager
    def upload(self, kind):
        key = kind + '/' + uuid.uuid4().hex
        with self.objects.lease(key):
            with self.transaction() as c:
                c.execute(garbage.insert().values(key=key, created=int(time.time())))
            yield key

    @staticmethod
    def referenced(c, key):
        return any(c.execute(select(table).where(condition).limit(1)).first() for table, condition in (
            (site_uploads,or_(site_uploads.c.object_key==key,site_uploads.c.thumb_key==key)),
            (site_sends,or_(site_sends.c.content_key==key,site_sends.c.pending_key==key)),
            (export_files, export_files.c.object_key == key),
            (export_jobs, export_jobs.c.object_key == key),
            (messages, messages.c.content_key == key),
            (media, or_(media.c.object_key == key, media.c.thumb_key == key)),
            (outbox, outbox.c.content_key == key),
            (group_profiles, group_profiles.c.avatar_key == key),
            (people, people.c.avatar_key == key),
            (emoji_assets, or_(emoji_assets.c.object_key == key,emoji_assets.c.thumb_key == key))))

    def add_group(self, gid, title, kind=None):
        with self.transaction() as c:
            self.require_active(c, gid)
            values = {'title': title} | ({'kind': kind} if kind else {})
            if c.execute(select(groups.c.id).where(groups.c.id == gid)).first():
                c.execute(groups.update().where(groups.c.id == gid).values(**values))
            else:
                c.execute(groups.insert().values(id=gid, **values))

    def group(self, gid):
        with self.engine.connect() as c:
            row = c.execute(select(groups).where(groups.c.id == gid)).mappings().first()
            return dict(row) if row else None

    def update_group(self, gid, **values):
        with self.transaction() as c:
            self.require_active(c, gid)
            c.execute(groups.update().where(groups.c.id == gid).values(**values))

    def group_profile(self, gid):
        with self.engine.connect() as c:
            row = c.execute(select(group_profiles).where(group_profiles.c.group_id == gid)).mappings().first()
            return dict(row) if row else {}

    def save_group_profile(self, gid, **values):
        with self.transaction() as c:
            self.require_active(c, gid)
            old = c.execute(select(group_profiles).where(group_profiles.c.group_id == gid)).mappings().first()
            if old:
                if 'avatar_key' in values and values['avatar_key'] != old['avatar_key']:
                    self.gc(c, old['avatar_key'])
                c.execute(group_profiles.update().where(group_profiles.c.group_id == gid).values(**values))
            else:
                c.execute(group_profiles.insert().values(group_id=gid, **values))
            if values.get('avatar_key'):
                c.execute(garbage.delete().where(garbage.c.key == values['avatar_key']))

    def store_avatar(self, gid, path, identity):
        if self.group_profile(gid).get('avatar_identity') == identity:
            return
        from pathlib import Path
        from .telegram import make_thumbnail
        with self.objects.workspace() as temporary:
            preview = Path(temporary) / 'avatar.jpg'
            make_thumbnail(Path(path), preview, 'photo')
            with self.upload('avatars') as key:
                self.objects.put_file(key, preview, 'image/jpeg')
                self.save_group_profile(gid, avatar_key=key, avatar_identity=identity)

    def ingest(self, gid, data, *, confirmed_ttl=False):
        """Idempotent application; only source-confirmed live TTL can repair a tombstone."""
        if (data.get('media') or {}).get('kind') == 'animation' and data.get('animation_verified'):
            from .animations import classify
            with self.transaction() as c: self.require_active(c, gid)
            # Also correct earlier forwards and snapshots; all IDs and bytes stay.
            classify(self, [data['media']])
        fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        condition = and_(messages.c.group_id == gid, messages.c.id == data["id"])
        with self.engine.connect() as c:
            old = c.execute(select(messages).where(condition)).mappings().first()
            if old and ((old["deleted"] and not confirmed_ttl) or old["edited"] > data.get("edited", 0) or old["fingerprint"] == fingerprint):
                return
        with self.upload('messages') as key:
            self.objects.put_json(key, {"text": data.get("text", ""),
                **{k:data[k] for k in ('content_type','duration','waveform','sticker_emoji','custom_emoji','custom_emojis','reactions') if k in data}})
            with self.transaction() as c:
                self.require_active(c, gid)
                old = c.execute(select(messages).where(condition)).mappings().first()
                if old and ((old["deleted"] and not confirmed_ttl) or old["edited"] > data.get("edited", 0) or old["fingerprint"] == fingerprint):
                    return
                c.execute(expirations.delete().where(expirations.c.group_id == gid, expirations.c.message_id == data["id"]))
                if data.get("expires"):
                    c.execute(expirations.insert().values(group_id=gid, message_id=data["id"], expires=data["expires"]))
                values = dict(created=data["created"], edited=data.get("edited", 0),
                    sender_id=str(data["sender_id"]), sender=data["sender"], album_id=data.get("album_id"),
                    reply_to=data.get("reply_to"), content_key=key, fingerprint=fingerprint, deleted=False)
                try:
                    if old:
                        c.execute(messages.update().where(condition).values(**values))
                        self.gc(c, old["content_key"])
                    else:
                        c.execute(messages.insert().values(group_id=gid, id=data["id"], **values))
                    c.execute(garbage.delete().where(garbage.c.key == key))
                    previous = c.execute(select(media).where(and_(media.c.group_id == gid,
                        media.c.message_id == data["id"]))).mappings().first()
                    incoming = data.get("media")
                    if previous and (not incoming or previous["identity"] != incoming["identity"]):
                        self.gc(c, previous["object_key"])
                        self.gc(c, previous["thumb_key"])
                        c.execute(media.delete().where(media.c.id == previous["id"]))
                        previous = None
                    if incoming and not previous:
                        c.execute(media.insert().values(id=uuid.uuid4().hex, group_id=gid,
                            message_id=data["id"], created=data["created"], **incoming))
                    elif incoming and previous and any(previous[k]!=incoming[k] for k in ('kind','mime','name')):
                        values={k:incoming[k] for k in ('kind','mime','name')}
                        if previous['state']!='paused' and incoming['kind']=='sticker' and incoming['mime']=='video/webm':
                            values.update(state='pending',retry_at=0)
                        c.execute(media.update().where(media.c.id==previous['id']).values(**values))
                    if getattr(self,'site_messaging',None):self.site_messaging.reuse_upload(c,gid,data['id'])
                except BaseException:
                    self.objects.delete(key)
                    raise

    def delete_messages(self, gid, ids, *, expiration=False):
        """Keep only source IDs as tombstones, never deleted text or files."""
        with self.transaction() as c:
            if expiration and getattr(self, "registry", None): self.registry.read(c, lock=True)
            if not expiration: self.require_active(c, gid)
            for mid in ids:
                if expiration:
                    due = c.execute(select(expirations).where(expirations.c.group_id == gid, expirations.c.message_id == mid, expirations.c.expires <= int(time.time())).with_for_update()).first()
                    if not due: continue
                c.execute(expirations.delete().where(expirations.c.group_id == gid, expirations.c.message_id == mid))
                condition = and_(messages.c.group_id == gid, messages.c.id == mid)
                old = c.execute(select(messages).where(condition)).mappings().first()
                if old:
                    self.gc(c, old["content_key"])
                    c.execute(messages.update().where(condition).values(deleted=True, content_key=None,
                        fingerprint=None, sender="", sender_id="", album_id=None, reply_to=None))
                else:
                    c.execute(messages.insert().values(group_id=gid, id=mid, created=0,
                        sender="", sender_id="", deleted=True))
                for item in c.execute(select(media).where(and_(media.c.group_id == gid,
                        media.c.message_id == mid))).mappings():
                    self.gc(c, item["object_key"])
                    self.gc(c, item["thumb_key"])
                c.execute(media.delete().where(and_(media.c.group_id == gid, media.c.message_id == mid)))
                for sent in c.execute(select(outbox).where(and_(outbox.c.group_id == gid,
                        outbox.c.message_id == mid))).mappings():
                    self.gc(c, sent["content_key"])
                    c.execute(outbox.delete().where(outbox.c.id == sent["id"]))
                if getattr(self,'site_messaging',None):self.site_messaging.drop_message(c,gid,mid)

    def expire_messages(self):
        with self.engine.connect() as c:
            due = list(c.execute(select(expirations.c.group_id, expirations.c.message_id).where(expirations.c.expires <= int(time.time())).limit(500)))
        for gid, mid in due:
            self.delete_messages(gid, [mid], expiration=True)

    def collect_garbage(self, limit=100):
        cutoff = int(time.time()) - 300
        with self.engine.connect() as c:
            keys = list(c.execute(select(garbage.c.key).where(
                garbage.c.created <= cutoff).order_by(garbage.c.created, garbage.c.key).limit(limit)).scalars())
        result = dict(objects_removed=0, references_preserved=0, errors=0)
        uploads = self.objects.unfinished_uploads() if keys else {}
        for key in keys:
            with self.objects.lease(key, blocking=False) as acquired:
                if not acquired: continue
                try:
                    with self.transaction() as c:
                        row = c.execute(select(garbage).where(garbage.c.key == key).with_for_update()).mappings().first()
                        if not row or row['created'] > cutoff: continue
                        if self.referenced(c, key):
                            result['references_preserved'] += 1
                        else:
                            # A completed object and an unfinished S3 multipart
                            # upload are separate resources. Both need cleanup.
                            self.objects.abort_uploads(key, uploads)
                            self.objects.delete(key)
                            result['objects_removed'] += 1
                        c.execute(garbage.delete().where(garbage.c.key == key))
                except Exception:
                    # Leave the durable intent queued; a failed delete retries.
                    result['errors'] += 1
        return result

    def attach_download(self, item, key, thumb, size):
        with self.transaction() as c:
            self.require_active(c, item["group_id"])
            current = c.execute(select(media).where(media.c.id == item["id"])).mappings().first()
            if not current or current["identity"] != item["identity"]:
                self.gc(c, key)
                self.gc(c, thumb)
                return False
            if current['state']=='ready' and current['object_key'] and current['object_key']!=key and c.execute(select(site_uploads.c.id).where(site_uploads.c.object_key==current['object_key'],site_uploads.c.send_id.is_not(None))).first():
                self.gc(c,key);self.gc(c,thumb);return False
            if current['object_key']!=key:self.gc(c,current['object_key'])
            if current['thumb_key']!=thumb:self.gc(c,current['thumb_key'])
            c.execute(media.update().where(media.c.id == item["id"]).values(
                object_key=key, thumb_key=thumb, size=size, state="ready", attempts=0, error=None, retry_at=0))
            c.execute(garbage.delete().where(garbage.c.key.in_([value for value in (key, thumb) if value])))
            return True

    def heartbeat(self, state):
        with self.transaction() as c:
            if c.execute(select(runtime)).first():
                c.execute(runtime.update().values(heartbeat=int(time.time()), state=state))
            else:
                c.execute(runtime.insert().values(id=1, heartbeat=int(time.time()), state=state))

    def group_list(self, *, media_stats=False):
        totals=[func.sum(case((media.c.state.not_in(('ready','paused')),1),else_=0)).label('media_pending')]
        if media_stats:
            totals.extend((func.sum(case((media.c.kind=='photo',1),else_=0)).label('photos'),
                func.sum(case((media.c.kind=='video',1),else_=0)).label('videos'),
                func.sum(media.c.size).label('media_bytes')))
        counts=select(media.c.group_id,*totals).group_by(media.c.group_id).subquery()
        # The (group_id, id) primary key finds the latest visible ID without
        # materializing or grouping the full message archive on every poll.
        latest=select(messages.c.id).where(messages.c.group_id==groups.c.id,
            messages.c.deleted==False).order_by(messages.c.id.desc()).limit(1).scalar_subquery()
        query=select(groups,group_profiles.c.avatar_key,group_profiles.c.avatar_identity,
            func.coalesce(latest,0).label('latest_message_id'),
            *(func.coalesce(counts.c[t.name],0).label(t.name) for t in totals)).select_from(
                groups.outerjoin(group_profiles,group_profiles.c.group_id==groups.c.id)
                .outerjoin(counts,counts.c.group_id==groups.c.id)).order_by(groups.c.title)
        with self.engine.connect() as c:
            result = [dict(row) for row in c.execute(query).mappings()]
            for group in result:
                group['has_avatar'] = bool(group.pop('avatar_key'))
                identity=group.pop('avatar_identity')
                group['avatar_version'] = identity if group['has_avatar'] else None
            return result

    def serialize(self, c, row):
        result = {k: row[k] for k in ("id", "group_id", "created", "edited", "sender_id", "sender", "album_id", "reply_to")}
        result['version'] = row['fingerprint']
        content = self.objects.read_json(row["content_key"])
        result["text"] = content["text"]
        result.update({k:content[k] for k in ('content_type','duration','waveform','sticker_emoji','custom_emoji','custom_emojis','reactions') if k in content})
        sent = c.execute(select(outbox.c.author).where(and_(outbox.c.group_id == row["group_id"],
            outbox.c.message_id == row["id"], outbox.c.state == "delivered"))).scalar()
        if sent:
            result["sender"] = sent + " · с сайта"
        result.update(self.person(c,row['group_id'],row['sender_id']))
        result['custom_emojis']=[{**entry,**self.emoji(c,row['group_id'],entry['document_id'])} for entry in result.get('custom_emojis',[])]
        result['reactions']=[{**reaction,'people':[self.reactor(c,row['group_id'],person) for person in reaction.get('people',[])],**({'custom_emoji':self.emoji(c,row['group_id'],reaction['document_id'])} if reaction.get('document_id') else {})} for reaction in result.get('reactions',[])]
        result["media"] = [self.serial_media(item) for item in c.execute(select(media).where(
            and_(media.c.group_id == row["group_id"], media.c.message_id == row["id"]))).mappings()]
        for item in result['media']:
            if item['kind'] in {'voice','audio','video_note'} and content.get('duration') is not None:item['duration']=content['duration']
            if item['kind']=='voice' and content.get('waveform'):item['waveform']=content['waveform']
        if getattr(self,'site_messaging',None):
            self.site_messaging.decorate(c,row,result)
            from .site_reactions import BotReactions
            BotReactions(self.site_messaging).decorate(c,row,result)
        return result

    @staticmethod
    def serial_media(row):
        return {k: row[k] for k in ("id", "group_id", "message_id", "created", "kind", "mime", "name", "size", "state")} | {"has_thumbnail": bool(row["thumb_key"]) and row["kind"] not in {"voice", "audio"},
            "has_audio_preview": bool(row["thumb_key"]) and row["kind"] in {"voice", "audio"}}

    def history(self, gid, before=0, limit=50):
        with self.engine.connect() as c:
            query = select(messages).where(and_(messages.c.group_id == gid, messages.c.deleted == False))
            if before:
                query = query.where(messages.c.id < before)
            rows = list(c.execute(query.order_by(messages.c.id.desc()).limit(limit + 1)).mappings())
            selected = rows[:limit]
            # Never split an album at a history page boundary. IDs remain source IDs.
            if selected and selected[-1]['album_id']:
                tail = selected[-1]
                extra = c.execute(select(messages).where(and_(messages.c.group_id == gid,
                    messages.c.deleted == False, messages.c.id < tail['id'],
                    messages.c.album_id == tail['album_id'], messages.c.sender_id == tail['sender_id']))
                    .order_by(messages.c.id.desc()).limit(9)).mappings()
                selected.extend(extra)
            more = bool(selected and c.execute(select(messages.c.id).where(and_(
                messages.c.group_id == gid, messages.c.deleted == False,
                messages.c.id < selected[-1]['id'])).limit(1)).first())
            return {"items": [self.serialize(c, row) for row in reversed(selected)], "more": more}

    def enqueue(self, gid, actor, author, client_id, text, reply_to=None):
        digest = hashlib.sha256(json.dumps([text, reply_to], ensure_ascii=False).encode()).hexdigest()
        query = select(outbox).where(and_(outbox.c.group_id == gid, outbox.c.actor_id == actor,
                                         outbox.c.client_id == client_id))
        def existing():
            with self.engine.connect() as c:
                row = c.execute(query).mappings().first()
                if row:
                    if row['fingerprint'] != digest:
                        raise ValueError("This client_id already belongs to another message")
                    return {k: row[k] for k in ("id", "state", "message_id")}
        if old := existing():
            return old
        with self.upload('outbox') as key:
            self.objects.put_json(key, {"text": text})
            try:
                return self._enqueue(gid, actor, author, client_id, reply_to, digest, key, query)
            except IntegrityError:
                # Concurrent retries race safely on PostgreSQL's unique constraint.
                if old := existing():
                    return old
                raise

    def _enqueue(self, gid, actor, author, client_id, reply_to, digest, key, query):
        with self.transaction() as c:
            self.require_active(c, gid)
            old = c.execute(query).mappings().first()
            if old:
                if old["fingerprint"] != digest:
                    raise ValueError("This client_id already belongs to another message")
                return {k: old[k] for k in ("id", "state", "message_id")}
            if reply_to and not c.execute(select(messages.c.id).where(and_(messages.c.group_id == gid,
                    messages.c.id == reply_to, messages.c.deleted == False))).first():
                raise ValueError("Reply target is unavailable")
            oid = uuid.uuid4().hex
            c.execute(outbox.insert().values(id=oid, group_id=gid, actor_id=actor, author=author,
                client_id=client_id, content_key=key, fingerprint=digest, reply_to=reply_to, created=int(time.time())))
            c.execute(garbage.delete().where(garbage.c.key == key))
            return {"id": oid, "state": "queued", "message_id": None}
