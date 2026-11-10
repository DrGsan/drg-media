"""Durable, bounded-memory ZIP jobs. Originals are pinned until work finishes."""
import json
import re
import time
import uuid
import zipfile
import struct
from datetime import datetime,timezone
from pathlib import Path
from sqlalchemy import select, func
from .db import export_jobs, export_files, media, groups, garbage
from .group_management import GroupInactive, PERIOD


class Stopped(Exception):
    pass


def safe_name(value):
    value = re.sub(r'[\\/\x00-\x1f:*?"<>|]', '_', value).strip(' .')[:120]
    return value or 'Фотографии'


class StreamWriter:
    """Unseekable ZIP sink; multipart buffer stays below 32 MiB."""
    def __init__(self, objects, key, check):
        self.objects, self.key, self.check = objects, key, check
        self.position = 0
        self.buffer = bytearray()
        self.parts = []
        self.upload = None
        self.file = None
        if objects.config.storage == 's3':
            self.upload = objects.s3.create_multipart_upload(Bucket=objects.config.bucket,
                Key=objects.key(key), ContentType='application/zip')['UploadId']
        else:
            path = objects.root / key
            path.parent.mkdir(exist_ok=True, mode=0o700)
            self.file = path.open('xb')

    def tell(self): return self.position
    def flush(self):
        if self.file: self.file.flush()

    def write(self, data):
        self.check()
        if self.file:
            self.file.write(data)
        else:
            self.buffer.extend(data)
            while len(self.buffer) >= 32 * 1024 * 1024:
                self.part(bytes(self.buffer[:32 * 1024 * 1024]))
                del self.buffer[:32 * 1024 * 1024]
        self.position += len(data)
        return len(data)

    def part(self, data):
        if len(self.parts) >= 10000: raise ValueError('archive_too_large')
        response = self.objects.s3.upload_part(Bucket=self.objects.config.bucket, Key=self.objects.key(self.key),
            UploadId=self.upload, PartNumber=len(self.parts)+1, Body=data)
        self.parts.append({'PartNumber':len(self.parts)+1, 'ETag':response['ETag']})

    def finish(self):
        self.check()
        if self.file:
            import os
            self.file.flush(); os.fsync(self.file.fileno()); self.file.close()
        else:
            if self.buffer: self.part(bytes(self.buffer)); self.buffer.clear()
            self.objects.s3.complete_multipart_upload(Bucket=self.objects.config.bucket,
                Key=self.objects.key(self.key), UploadId=self.upload, MultipartUpload={'Parts':self.parts})

    def close(self):
        if self.file: self.file.close()
        # Multipart cleanup is durable via object_gc, even if this process dies.


class Exports:
    def __init__(self, archive, registry):
        self.archive, self.registry = archive, registry

    def read(self, jid, c=None, lock=False):
        if c is None:
            with self.archive.engine.connect() as connection: return self.read(jid, connection)
        query = select(export_jobs).where(export_jobs.c.id == jid)
        if lock: query = query.with_for_update()
        row = c.execute(query).mappings().first()
        if not row: raise KeyError(jid)
        return json.loads(row['value']) | {'id':jid, 'actor_id':row['actor_id'], 'object_key':row['object_key']}

    @staticmethod
    def save(c, job):
        value = {k:v for k,v in job.items() if k not in {'id','actor_id','object_key'}}
        c.execute(export_jobs.update().where(export_jobs.c.id == job['id']).values(value=json.dumps(value), object_key=job['object_key']))

    @staticmethod
    def public(job):
        return {k:v for k,v in job.items() if k not in {'object_key','request_id','actor_id','cleanup_key','file_dates'}}

    def create(self, actor, gids, videos, request_id):
        self.archive.expire_messages()
        with self.registry.edit() as (c, state):
            # Registry lock serializes deduplication and snapshotting with all source writes.
            for row in c.execute(select(export_jobs).where(export_jobs.c.actor_id == actor)).mappings():
                previous = json.loads(row['value'])
                if previous['request_id'] == request_id:
                    if previous['groups'] != gids or previous['videos'] != videos: raise ValueError('request_conflict')
                    return self.public(self.read(row['id'], c))
                if previous['groups'] == gids and previous['videos'] == videos and previous['state'] in {'queued','running'}:
                    return self.public(self.read(row['id'], c))
            jobs = [json.loads(row) for row in c.execute(select(export_jobs.c.value)).scalars()]
            if sum(j['state'] in {'queued','running'} for j in jobs) >= 3: raise ValueError('queue_full')
            if sum(j['state'] == 'ready' and j['expires'] > time.time() for j in jobs) >= 20: raise ValueError('exports_full')
            manifest = []
            file_dates = []
            incomplete = False
            for gid in gids:
                control = state['groups'].get(gid)
                if not control or not control['enabled'] or control['delete_at'] is not None: raise GroupInactive('group_inactive')
                group = c.execute(select(groups).where(groups.c.id == gid)).mappings().one()
                incomplete |= not group['history_complete']
                query = select(media).where(media.c.group_id == gid, media.c.kind.in_(['photo','video'] if videos else ['photo'])).order_by(media.c.message_id)
                for item in c.execute(query).mappings():
                    if item['state'] != 'ready' or not item['object_key']:
                        incomplete = True
                        continue
                    name = safe_name(group['title']) + '-' + safe_name(gid) + '/' + str(item['message_id']) + '-' + safe_name(item['name'])
                    file_dates.append(item['created'])
                    manifest.append(dict(ordinal=len(manifest), object_key=item['object_key'], name=name, size=item['size']))
            if not manifest: raise ValueError('no_photos')
            jid = uuid.uuid4().hex
            job = dict(id=jid, actor_id=actor, object_key=None, groups=gids, videos=videos, request_id=request_id,
                file_dates=file_dates, state='queued', created=int(time.time()), ready_at=None, expires=None, count=len(manifest),
                total_bytes=sum(i['size'] for i in manifest), done=0, bytes=0, size=0, incomplete=bool(incomplete), error=None)
            c.execute(export_jobs.insert().values(id=jid, actor_id=actor, value='{}'))
            self.save(c, job)
            for start in range(0,len(manifest),100):
                c.execute(export_files.insert(), [dict(job_id=jid, **item) for item in manifest[start:start+100]])
            return self.public(job)

    def list(self, actor):
        with self.archive.engine.connect() as c:
            return [self.public(self.read(jid,c)) for jid in c.execute(select(export_jobs.c.id).where(export_jobs.c.actor_id == actor)).scalars()]

    def retire(self, jid, reason):
        with self.archive.transaction() as c:
            job = self.read(jid,c,lock=True)
            self.retire_in(self.archive,c,job,reason)

    @staticmethod
    def retire_in(archive,c,job,reason):
        if job['state'] in {'cleaned','cleaning'}: return
        job.update(state='cleaning', error=reason, retired=int(time.time()))
        # Release source pins, but re-enqueue GC: originals may have disappeared.
        for key in c.execute(select(export_files.c.object_key).where(export_files.c.job_id == job['id'])).scalars():
            archive.gc(c,key)
        c.execute(export_files.delete().where(export_files.c.job_id == job['id']))
        if job['object_key']:
            archive.gc(c,job['object_key'])
            job['cleanup_key']=job['object_key']
            job['object_key']=None
        Exports.save(c,job)

    def maintain(self):
        with self.archive.engine.connect() as c: ids = list(c.execute(select(export_jobs.c.id)).scalars())
        for jid in ids:
            job = self.read(jid)
            if job['state'] in {'queued','running','ready'}:
                if not all(self.registry.active(gid) for gid in job['groups']): self.retire(jid,'group_unavailable')
                elif job['state'] == 'ready' and job['expires'] <= time.time(): self.retire(jid,'expired')
            job = self.read(jid)
            if job['state'] == 'cleaning':
                key = job['object_key']
                with self.archive.transaction() as c:
                    # Unreference output before GC. Keep the durable key until confirmed gone.
                    c.execute(export_jobs.update().where(export_jobs.c.id == jid).values(object_key=None))
                    job['cleanup_key'] = key or job.get('cleanup_key')
                    job['object_key'] = None
                    key = job['cleanup_key']
                    if not key or not c.execute(select(garbage).where(garbage.c.key == key)).first():
                        job.update(state='cleaned', cleaned_at=int(time.time()))
                    self.save(c,job)
            elif job['state'] == 'cleaned' and job['cleaned_at'] < time.time()-86400:
                with self.archive.transaction() as c: c.execute(export_jobs.delete().where(export_jobs.c.id == jid))

    def tick(self):
        """One worker across gunicorn processes; lock releases automatically on crash."""
        with self.archive.objects.lease('exportworker/'+'0'*32, blocking=False) as acquired:
            if not acquired: return
            self.maintain()
            with self.archive.engine.connect() as c: ids = list(c.execute(select(export_jobs.c.id)).scalars())
            for jid in ids:
                job = self.read(jid)
                if job['state'] == 'running': self.retire(jid,'interrupted')
            for jid in ids:
                if self.read(jid)['state'] == 'queued':
                    self.build(jid)
                    return True
            return False

    def build(self, jid):
        writer = None
        try:
            with self.archive.objects.lease('exportjob/' + jid):
                with self.archive.transaction() as c:
                    job = self.read(jid,c,lock=True)
                    if job['state'] != 'queued': return
                    key = 'exports/' + uuid.uuid4().hex
                    c.execute(garbage.insert().values(key=key,created=int(time.time())))
                    job.update(state='running',object_key=key)
                    self.save(c,job)
                with self.archive.objects.lease(key):
                    last = 0
                    def check(force=False):
                        nonlocal last
                        if not force and time.monotonic()-last < .5: return
                        last = time.monotonic()
                        current = self.read(jid)
                        if current['state'] != 'running' or not all(self.registry.active(g) for g in current['groups']): raise Stopped()
                    writer = StreamWriter(self.archive.objects, key, check)
                    with self.archive.engine.connect() as c:
                        manifest = [dict(r) for r in c.execute(select(export_files).where(export_files.c.job_id == jid).order_by(export_files.c.ordinal)).mappings()]
                    with zipfile.ZipFile(writer, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as output:
                        for item in manifest:
                            check(True)
                            read = 0
                            reported = time.monotonic()
                            dates=job.get('file_dates',[])
                            stamp=dates[item['ordinal']] if item['ordinal']<len(dates) and dates[item['ordinal']]>0 else job['created']
                            moment=datetime.fromtimestamp(stamp,timezone.utc)
                            dos=max(datetime(1980,1,1,tzinfo=timezone.utc),min(moment,datetime(2107,12,31,23,59,58,tzinfo=timezone.utc)))
                            info=zipfile.ZipInfo(item['name'],dos.timetuple()[:6])
                            if 0<=stamp<=0xffffffff:info.extra=struct.pack('<HHBI',0x5455,5,1,int(stamp))
                            with output.open(info, 'w', force_zip64=True) as target:
                                for chunk in self.archive.objects.read(item['object_key']):
                                    target.write(chunk); read += len(chunk)
                                    if time.monotonic()-reported >= 2:
                                        with self.archive.transaction() as c:
                                            current=self.read(jid,c,lock=True)
                                            if current['state']!='running':raise Stopped()
                                            current.update(current_name=item['name'],current_bytes=read)
                                            self.save(c,current)
                                        reported=time.monotonic()
                            if read != item['size']: raise OSError('source_size_changed')
                            with self.archive.transaction() as c:
                                current = self.read(jid,c,lock=True)
                                if current['state'] != 'running': raise Stopped()
                                current.update(done=current['done']+1,bytes=current['bytes']+read,current_bytes=0,current_name=None)
                                self.save(c,current)
                        output.writestr('Состав архива.json',json.dumps({'created':job['created'],'incomplete':job['incomplete'],
                            'files':[{'name':i['name'],'size':i['size']} for i in manifest]},ensure_ascii=False))
                    writer.finish()
                    with self.registry.edit() as (c, state):
                        current = self.read(jid,c,lock=True)
                        if current['state'] != 'running' or not all(state['groups'].get(g,{}).get('enabled') and state['groups'][g]['delete_at'] is None for g in current['groups']): raise Stopped()
                        stamp=int(time.time())
                        current.update(state='ready',ready_at=stamp,expires=stamp+PERIOD,size=writer.position)
                        for item in manifest: self.archive.gc(c,item['object_key'])
                        c.execute(export_files.delete().where(export_files.c.job_id == jid))
                        c.execute(garbage.delete().where(garbage.c.key == key))
                        self.save(c,current)
        except Exception as error:
            self.retire(jid,'cancelled' if isinstance(error,Stopped) else 'preparation_failed')
        finally:
            if writer: writer.close()
