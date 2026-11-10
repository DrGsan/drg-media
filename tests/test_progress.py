import asyncio
import json
import os
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from telethon import errors

from family_photo.db import media, garbage
from family_photo.progress import Progress, snapshot, read_progress
from family_photo.telegram import Synchronizer, media_transfer
from conftest import photo


def item(archive, mid=1, gid='-1001'):
    with archive.engine.connect() as c:
        return dict(c.execute(select(media).where(media.c.group_id==gid,media.c.message_id==mid)).mappings().one())


def test_manually_stopped_file_survives_edits_and_never_enters_download_queue(config,archive):
    archive.ingest('-1001',photo(1))
    archive.update_group('-1001',history_complete=True,state='online');archive.heartbeat('online')
    progress=Progress(config,archive);progress.begin(item(archive));progress.stage('download',10)
    with archive.transaction() as c:
        c.execute(media.update().values(state='paused',error='missing_from_backup'))
    archive.ingest('-1001',photo(1,edited=100,text='Changed caption'))
    client=SimpleNamespace(iter_messages=lambda *a,**k:(_ for _ in ()).throw(AssertionError('Stopped file was requested')))
    asyncio.run(Synchronizer(config,archive,client).downloads())
    state=snapshot(archive,'-1001')
    assert state['pending']==0 and state['current'] is None
    assert state['stopped']==1 and state['stopped_files']==[{'name':'photo.jpg','message_id':1,'created':1700000001,'group_title':'Семья 1','telegram_url':'https://t.me/c/1/1'}]
    assert state['batch']['total']==0
    assert archive.group_list()[0]['media_pending']==0
    assert item(archive)['state']=='paused' and item(archive)['object_key'] is None


def test_batch_excludes_old_photos_tracks_bytes_restart_new_arrivals_and_deletion(config,archive,client,monkeypatch):
    for mid in (1,2,3):archive.ingest('-1001',photo(mid))
    old=item(archive)
    path=config.data_dir/'photo';path.write_bytes(b'abcdefghij')
    archive.objects.put_file('media/'+'a'*32,path)
    archive.attach_download(old,'media/'+'a'*32,None,10)
    archive.update_group('-1001',history_complete=True,state='online')
    archive.heartbeat('online')
    progress=Progress(config,archive);progress.begin(item(archive,2));progress.stage('download',10)
    clock=time.monotonic();progress.anchor=(clock-2,0)
    progress.transfer(5);progress.save(True)
    current=client.get('/v1/groups/-1001/sync').json
    assert current['batch']['total']==2 and current['batch']['completed']==0
    assert current['current']['bytes']==5 and current['current']['speed']>0
    assert current['current']['created']==1700000002 and current['current']['group_title']=='Семья 1'
    assert current['current']['telegram_url']=='https://t.me/c/1/2'
    assert current['eta_seconds']>0 and current['pending']==2
    assert 'object_key' not in json.dumps(current)
    # Group-scoped API never includes another group's current filename or batch.
    other=client.get('/v1/groups/-1002/sync').json
    assert other['pending']==0 and other['current'] is None and other['batch']['total']==0
    assert client.get('/v1/groups/-999/sync').status_code==404
    assert client.get('/v1/groups/-1001/sync',headers={'Authorization':'bad'}).status_code==401
    with archive.upload('media') as key:
        archive.objects.put_file(key,path);archive.attach_download(item(archive,2),key,None,10)
    progress.finish()
    assert snapshot(archive,'-1001')['batch']['completed']==1
    # Restart retains this batch but discards stale file/rate information.
    progress=Progress(config,archive);progress.save(True)
    assert snapshot(archive,'-1001')['batch']['completed']==1
    archive.delete_messages('-1001',[3])
    assert snapshot(archive,'-1001')['batch']==dict(total=1,completed=1,percent=100.0)
    archive.ingest('-1001',photo(4))
    assert snapshot(archive,'-1001')['batch']['total']==1  # next batch, not 99.9% of old history
    progress.begin(item(archive,4));progress.stage('download',10)
    progress.anchor=(time.monotonic()-2,0);progress.transfer(5);progress.save(True)
    progress.wait(90)
    wait=snapshot(archive,'-1001')
    assert wait['current']['wait_until']>time.time()+80 and wait['current']['speed']==0 and wait['eta_seconds'] is None
    progress.resume()
    assert snapshot(archive,'-1001')['eta_seconds'] is None
    data=read_progress(config);data['heartbeat']=time.time()-200
    (config.data_dir/'.sync-progress.json').write_text(json.dumps(data))
    assert snapshot(archive,'-1001')['current'] is None


def test_upload_and_preview_are_not_reported_as_download_or_finished_early(config,archive):
    archive.ingest('-1001',photo());archive.heartbeat('online')
    progress=Progress(config,archive);progress.begin(item(archive));progress.stage('upload',10)
    progress.transfer(4,delta=True);progress.save(True)
    state=snapshot(archive,'-1001')
    assert state['current']['stage']=='upload' and state['current']['bytes']==4 and state['eta_seconds'] is None
    with archive.upload('media') as key:
        path=config.data_dir/'webm';path.write_bytes(b'video')
        archive.objects.put_file(key,path);archive.attach_download(item(archive),key,None,5)
        progress.stage('preview')
        state=snapshot(archive,'-1001')
        assert state['state']=='active' and state['batch']['completed']==0 and state['batch']['percent']<100
    progress.finish()
    assert snapshot(archive,'-1001')['pending']==0


def test_real_download_reports_received_chunks_and_server_pause(config,archive,monkeypatch,tmp_path):
    archive.ingest('-1001',photo());archive.heartbeat('online')
    sync=Synchronizer(config,archive,SimpleNamespace())
    snapshots=[]
    class Stream:
        def __init__(self):self.index=0
        def __aiter__(self):return self
        async def __anext__(self):
            self.index+=1
            if self.index==1:return b'abcd'
            if self.index==2:raise errors.FloodWaitError(None,2)
            if self.index==3:return b'efghij'
            raise StopAsyncIteration
        async def close(self):pass
    sync.client.iter_download=lambda *a,**k:Stream()
    async def sleep(seconds):
        snapshots.append(snapshot(archive,'-1001'))
        # End only the waiting clock, while preserving wall time/API deadlines.
        monkeypatch.setattr('family_photo.telegram.time.monotonic',lambda:100)
    async def scenario():
        sync.progress.begin(item(archive));token=media_transfer.set(True)
        monkeypatch.setattr('family_photo.telegram.time.monotonic',lambda:1)
        monkeypatch.setattr('family_photo.telegram.asyncio.sleep',sleep)
        source=SimpleNamespace(document=object(),file=SimpleNamespace(size=10))
        try:await sync.download_media(source,tmp_path/'downloaded')
        finally:media_transfer.reset(token)
    asyncio.run(scenario())
    assert (tmp_path/'downloaded').read_bytes()==b'abcdefghij'
    assert snapshots[0]['current']['bytes']==4
    assert snapshots[0]['current']['wait_until']>time.time()
    assert snapshots[0]['eta_seconds'] is None


def test_gc_preserves_references_and_active_uploads_and_retries_failed_deletes(config,archive,monkeypatch):
    path=config.data_dir/'file';path.write_bytes(b'valid')
    archive.ingest('-1001',photo())
    with archive.upload('media') as live:
        archive.objects.put_file(live,path)
        with archive.transaction() as c:c.execute(garbage.update().where(garbage.c.key==live).values(created=0))
        assert archive.collect_garbage()['objects_removed']==0  # writer lease, even if old
        assert b''.join(archive.objects.read(live))==b'valid'
        archive.attach_download(item(archive),live,None,5)
    # A stale bookkeeping entry can never remove a published original.
    with archive.transaction() as c:c.execute(garbage.insert().values(key=live,created=0))
    assert archive.collect_garbage()['references_preserved']==1
    assert b''.join(archive.objects.read(live))==b'valid'
    orphan=archive.reserve('media');archive.objects.put_file(orphan,path)
    with archive.transaction() as c:c.execute(garbage.update().where(garbage.c.key==orphan).values(created=1))
    delete=archive.objects.delete
    monkeypatch.setattr(archive.objects,'delete',lambda key:(_ for _ in ()).throw(OSError('storage unavailable')))
    assert archive.collect_garbage()['errors']==1
    with archive.engine.connect() as c:assert c.execute(select(garbage).where(garbage.c.key==orphan)).first()
    monkeypatch.setattr(archive.objects,'delete',delete)
    assert archive.collect_garbage()['objects_removed']==1
    with pytest.raises(Exception):b''.join(archive.objects.read(orphan))


def test_cleanup_recovers_crashed_workspaces_but_preserves_active_and_unrelated_files(config,archive):
    legacy=config.data_dir/'download-crashed1';legacy.mkdir();(legacy/'original').write_bytes(b'partial')
    backup=config.data_dir/'.sticker-upgrade-test';backup.mkdir();(backup/'archive.sqlite3').write_bytes(b'backup')
    session=config.data_dir/'telegram.session';session.write_bytes(b'private')
    unrelated=config.data_dir/'download-my-photos';unrelated.mkdir();(unrelated/'photo').write_bytes(b'keep')
    with archive.objects.workspace('download-') as active:
        Path(active,'original').write_bytes(b'active')
        old=time.time()-900;os.utime(active,(old,old))
        cleaned=archive.objects.clean_temporary(startup=True)
        assert cleaned==dict(temporary_removed=1,temporary_bytes=7)
        assert Path(active,'original').read_bytes()==b'active'
    assert (unrelated/'photo').read_bytes()==b'keep'
    assert session.read_bytes()==b'private' and (backup/'archive.sqlite3').read_bytes()==b'backup'


def test_cleanup_runs_while_telegram_download_is_blocked(config,archive,monkeypatch):
    sync=Synchronizer(replace(config,page_delay=0),archive,AsyncMock())
    async def scenario():
        entered=asyncio.Event();release=asyncio.Event()
        async def blocked():entered.set();await release.wait()
        sync.downloads=blocked
        archive.collect_garbage=lambda: {'objects_removed':1}
        download=asyncio.create_task(sync.media_loop());cleanup=asyncio.create_task(sync.maintenance_loop())
        try:
            await entered.wait()
            for _ in range(100):
                if read_progress(config).get('cleanup',{}).get('objects_removed'):break
                await asyncio.sleep(.01)
            assert read_progress(config)['cleanup']['objects_removed']==1
            assert not download.done()
        finally:
            download.cancel();cleanup.cancel()
            await asyncio.gather(download,cleanup,return_exceptions=True)
    asyncio.run(scenario())


def test_multipart_orphan_cleanup_is_scoped_and_lease_protected(config,archive):
    if config.storage!='s3':pytest.skip('S3 multipart lifecycle')
    store=archive.objects
    orphan=archive.reserve('media')
    foreign='outside-prefix/media/'+'f'*32
    for key in (store.key(orphan),foreign):
        upload=store.s3.create_multipart_upload(Bucket=config.bucket,Key=key)['UploadId']
        store.s3.upload_part(Bucket=config.bucket,Key=key,UploadId=upload,PartNumber=1,Body=b'partial')
    with archive.transaction() as c:c.execute(garbage.update().where(garbage.c.key==orphan).values(created=1))
    assert archive.collect_garbage()['objects_removed']==1
    uploads=store.s3.list_multipart_uploads(Bucket=config.bucket).get('Uploads',[])
    assert [u['Key'] for u in uploads]==[foreign]


def test_local_atomic_partial_cleanup_skips_an_active_upload(config,archive):
    if config.storage!='local':pytest.skip('Local atomic write lifecycle')
    root=archive.objects.root/'media';root.mkdir(exist_ok=True)
    key='media/'+'b'*32
    partial=root/('.'+'b'*32+'-'+'c'*32);partial.write_bytes(b'partial')
    old=time.time()-600;os.utime(partial,(old,old))
    with archive.objects.lease(key):
        assert archive.objects.clean_temporary()['temporary_removed']==0
        assert partial.exists()
    assert archive.objects.clean_temporary()['temporary_removed']==1
    assert not partial.exists()


def test_stopped_source_uses_bound_telegram_id_and_retains_exact_message_time(config,archive):
    from dataclasses import replace
    archive.objects.config=replace(config,group_ids=('-1001915828291',),group_bindings=(('-1001915828291','-1001'),))
    archive.ingest('-1001',photo(5462,created=1775564224))
    with archive.transaction() as c:c.execute(media.update().values(state='paused'))
    source=snapshot(archive,'-1001')['stopped_files'][0]
    assert source['created']==1775564224 and source['telegram_url']=='https://t.me/c/1915828291/5462'
    archive.delete_messages('-1001',[5462])
    assert snapshot(archive,'-1001')['stopped_files']==[]


def test_offline_exports_have_dates_but_no_invented_telegram_link(config,archive):
    archive.ingest('-1001',photo(10,created=1775564224))
    archive.update_group('-1001',kind='export')
    with archive.transaction() as c:c.execute(media.update().values(state='paused'))
    source=snapshot(archive,'-1001')['stopped_files'][0]
    assert source['created']==1775564224 and source['telegram_url'] is None
