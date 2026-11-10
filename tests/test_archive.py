import asyncio
import io
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select, func
from family_photo.db import messages, media, groups, outbox, garbage, metadata
from family_photo.telegram import Synchronizer, send_outbox, normalize, ephemeral
from family_photo.objects import Objects
from conftest import message, photo


def test_text_is_in_object_storage_and_ingestion_is_idempotent(archive):
    archive.ingest('-1001',photo())
    archive.ingest('-1001',photo())
    with archive.engine.connect() as c:
        row=c.execute(select(messages)).mappings().one()
        assert 'text' not in row and 'body' not in row
        assert archive.objects.read_json(row['content_key'])['text']=='Семейное фото'
        assert c.execute(select(func.count()).select_from(media)).scalar_one()==1
    assert archive.history('-1001')['items'][0]['sender']=='Анна'


def test_edits_replace_text_and_media_and_stale_events_cannot_revert(archive):
    archive.ingest('-1001',photo())
    with archive.engine.connect() as c:old=dict(c.execute(select(media)).mappings().one())
    path=archive.objects.config.data_dir/'sample';path.write_bytes(b'old photo')
    archive.objects.put_file('media/'+'a'*32,path)
    archive.attach_download(old,'media/'+'a'*32,None,9)
    archive.ingest('-1001',message(1,edited=10,text='Исправлено'))
    archive.ingest('-1001',photo())
    assert archive.history('-1001')['items'][0]['text']=='Исправлено'
    assert not archive.history('-1001')['items'][0]['media']
    archive.collect_garbage()
    assert not (archive.objects.root/'media'/('a'*32)).exists()


def test_deletion_erases_content_and_prevents_resurrection_and_late_download(archive):
    archive.ingest('-1001',photo())
    with archive.engine.connect() as c:item=dict(c.execute(select(media)).mappings().one())
    archive.delete_messages('-1001',[1])
    archive.ingest('-1001',photo())
    archive.ingest('-1001',message(1,edited=100,text='Old retry'))
    assert not archive.attach_download(item,'media/'+'b'*32,None,100)
    archive.collect_garbage()
    assert archive.history('-1001')['items']==[]
    with archive.engine.connect() as c:
        row=c.execute(select(messages)).mappings().one()
        assert row['deleted'] and row['content_key'] is None and row['sender']==''
        assert c.execute(select(func.count()).select_from(media)).scalar_one()==0


def test_delete_before_import_and_group_isolation(archive):
    archive.delete_messages('-1001',[5])
    archive.ingest('-1001',photo(5))
    archive.ingest('-1002',photo(5))
    assert archive.history('-1001')['items']==[]
    assert archive.history('-1002')['items'][0]['id']==5


def test_history_pagination_has_no_gaps_and_preserves_albums(archive):
    for mid in range(1,112):archive.ingest('-1001',photo(mid,album_id='77' if mid<4 else None))
    result=archive.history('-1001',limit=50)
    assert [r['id'] for r in result['items']]==list(range(62,112)) and result['more']
    second=archive.history('-1001',before=62,limit=100)
    assert [r['id'] for r in second['items']]==list(range(1,62)) and not second['more']
    assert second['items'][1]['album_id']=='77'


def test_outbox_is_idempotent_and_rejects_changed_retry_and_cross_group_reply(archive):
    archive.ingest('-1002',message(8))
    first=archive.enqueue('-1001','actor','Мария','request-1','Привет')
    assert archive.enqueue('-1001','actor','Мария','request-1','Привет')==first
    with pytest.raises(ValueError):archive.enqueue('-1001','actor','Мария','request-1','Другое')
    with pytest.raises(ValueError):archive.enqueue('-1001','actor','Мария','request-2','Ответ',8)
    assert archive.enqueue('-1001','other','Иван','request-1','Привет')['id']!=first['id']


def test_ambiguous_bot_delivery_is_not_automatically_duplicated(config,archive):
    entry=archive.enqueue('-1001','actor','Мария','request-1','Привет')
    class HTTP:
        calls=0
        async def post(self,*args,**kwargs):
            self.calls+=1
            raise httpx.ReadTimeout('Response lost after Telegram accepted the message')
    http=HTTP()
    asyncio.run(send_outbox(config,archive,http))
    asyncio.run(send_outbox(config,archive,http))
    assert http.calls==1
    with archive.engine.connect() as c:assert c.execute(select(outbox.c.state)).scalar_one()=='uncertain'


def test_bot_rate_limit_persists_retry_without_busy_loop(config,archive):
    archive.enqueue('-1001','actor','Мария','request-1','Привет')
    class HTTP:
        async def post(self,*args,**kwargs):
            assert kwargs['json']['text']=='Мария:\nПривет'
            return httpx.Response(429,json={'ok':False,'parameters':{'retry_after':120}})
    asyncio.run(send_outbox(config,archive,HTTP()))
    with archive.engine.connect() as c:
        row=c.execute(select(outbox)).mappings().one()
        assert row['state']=='queued' and row['retry_at']>0


def test_oldest_first_scan_resumes_durable_cursor(config,archive):
    class Client:
        async def get_messages(self,*args,**kwargs):return [SimpleNamespace(id=5)]
        def iter_messages(self,*args,**kwargs):
            assert kwargs['reverse'] is True
            assert kwargs['max_id']==6
            async def iterator():
                for mid in range(kwargs['min_id']+1,6):yield SimpleNamespace(id=mid)
            return iterator()
    sync=Synchronizer(config,archive,Client());seen=[]
    async def apply(gid,item):
        seen.append(item.id)
        if item.id==3:raise ConnectionError('Disconnected')
        archive.ingest(gid,message(item.id))
    sync.apply=apply
    with pytest.raises(ConnectionError):asyncio.run(sync.scan('-1001'))
    assert seen==[1,2,3] and archive.group('-1001')['cursor']==2
    async def success(gid,item):
        seen.append(item.id);archive.ingest(gid,message(item.id))
    sync.apply=success
    asyncio.run(sync.scan('-1001'))
    assert seen==[1,2,3,3,4,5]
    assert archive.group('-1001')['cursor']==5 and archive.group('-1001')['history_complete']


def test_network_and_access_failures_never_delete_messages(config,archive):
    archive.ingest('-1001',photo())
    archive.update_group('-1001',history_complete=True)
    client=SimpleNamespace(get_messages=AsyncMock(side_effect=ConnectionError('offline')))
    sync=Synchronizer(config,archive,client);sync.can_read=AsyncMock(return_value=True)
    with pytest.raises(ConnectionError):asyncio.run(sync.reconcile('-1001'))
    assert len(archive.history('-1001')['items'])==1
    sync.can_read=AsyncMock(return_value=False)
    asyncio.run(sync.reconcile('-1001'))
    assert len(archive.history('-1001')['items'])==1
    assert archive.group('-1001')['state']=='access_lost'


def test_reconciliation_walks_entire_archive_and_confirms_missing_twice(config,archive,monkeypatch):
    config=replace(config,reconcile_seconds=3600)
    archive.ingest('-1001',photo(1));archive.ingest('-1001',photo(2))
    archive.update_group('-1001',history_complete=True)
    client=SimpleNamespace(get_messages=AsyncMock(return_value=[None]))
    sync=Synchronizer(config,archive,client);sync.can_read=AsyncMock(return_value=True)
    monkeypatch.setattr('family_photo.telegram.time.time',lambda:1000)
    asyncio.run(sync.reconcile('-1001',batch=1))
    assert archive.group('-1001')['reconcile_cursor']==1
    assert len(archive.history('-1001')['items'])==2
    asyncio.run(sync.reconcile('-1001',batch=1))
    assert archive.group('-1001')['reconcile_cursor']==2
    asyncio.run(sync.reconcile('-1001',batch=1))
    monkeypatch.setattr('family_photo.telegram.time.time',lambda:5000)
    asyncio.run(sync.reconcile('-1001',batch=1))
    assert [r['id'] for r in archive.history('-1001')['items']]==[2]


def test_s3_and_volume_store_same_content_and_ranges(config,tmp_path):
    class S3:
        objects={}
        def upload_file(self,path,bucket,key,ExtraArgs):self.objects[(bucket,key)]=Path(path).read_bytes()
        def get_object(self,Bucket,Key,Range=None):
            body=self.objects[(Bucket,Key)]
            if Range:
                start,end=Range[6:].split('-');body=body[int(start):int(end)+1 if end else None]
            return {'Body':io.BytesIO(body)}
        def head_object(self,Bucket,Key):return {'ContentLength':len(self.objects[(Bucket,Key)])}
        def get_bucket_versioning(self,Bucket):return {}
        def delete_object(self,Bucket,Key):self.objects.pop((Bucket,Key),None)
    local=Objects(replace(config,storage='local'));remote=Objects(replace(config,storage='s3',bucket='family-only'),S3())
    path=tmp_path/'file';path.write_bytes(b'0123456789')
    for store in (local,remote):
        key='media/'+'a'*32;store.put_file(key,path)
        assert store.size(key)==10 and b''.join(store.read(key,2,5))==b'2345'
        store.put_json('messages/'+'b'*32,{'text':'Привет'})
        assert store.read_json('messages/'+'b'*32)=={'text':'Привет'}
        store.delete(key)
        with pytest.raises((KeyError,FileNotFoundError)):store.size(key)
        with pytest.raises(ValueError):store.put_file('../secret',path)


def test_postgresql_schema_compiles_without_sqlite_specific_statements():
    from sqlalchemy import create_mock_engine
    statements=[]
    engine=create_mock_engine('postgresql+psycopg://',lambda sql,*args,**kwargs:statements.append(str(sql.compile(dialect=engine.dialect))))
    metadata.create_all(engine)
    assert any('telegram_messages' in sql for sql in statements)
    assert not any('AUTOINCREMENT' in sql or 'PRAGMA' in sql for sql in statements)


def test_service_messages_without_media_or_edit_fields_do_not_stop_backfill():
    from datetime import datetime, timezone
    source=SimpleNamespace(id=1,date=datetime.now(timezone.utc),get_sender=AsyncMock(return_value=None))
    data=asyncio.run(normalize(source))
    assert data['id']==1 and data['text']=='' and 'media' not in data


def test_self_destructing_media_is_never_imported(config,archive):
    from datetime import datetime,timezone
    source=SimpleNamespace(id=1,date=datetime.now(timezone.utc),media=SimpleNamespace(ttl_seconds=60))
    asyncio.run(Synchronizer(config,archive,None).apply('-1001',source))
    archive.ingest('-1001',photo(1))
    assert archive.history('-1001')['items']==[]


def test_real_boto3_s3_roundtrip_and_deleted_objects(config,tmp_path):
    moto=pytest.importorskip('moto')
    import boto3
    with moto.mock_aws():
        s3=boto3.client('s3',region_name='us-east-1')
        s3.create_bucket(Bucket='family-photo-private-test')
        store=Objects(replace(config,storage='s3',bucket='family-photo-private-test'),s3)
        store.check()
        path=tmp_path/'video';path.write_bytes(b'0123456789')
        key='media/'+'c'*32;store.put_file(key,path,'video/mp4')
        store.put_json('messages/'+'d'*32,{'text':'Сообщение хранится в S3'})
        assert store.size(key)==10 and b''.join(store.read(key,2,5))==b'2345'
        assert store.read_json('messages/'+'d'*32)['text']=='Сообщение хранится в S3'
        assert s3.get_object(Bucket='family-photo-private-test',Key='family-photo/'+key)['ContentType']=='video/mp4'
        store.delete(key)
        assert 'Contents' not in s3.list_objects_v2(Bucket='family-photo-private-test',Prefix='family-photo/media/')


def test_crash_during_upload_is_collected_but_published_content_survives(archive,monkeypatch):
    import time
    stamp=int(time.time())
    key=archive.reserve('messages');archive.objects.put_json(key,{'text':'Interrupted upload'})
    archive.ingest('-1001',message(1))
    archive.collect_garbage()
    assert archive.objects.read_json(key)['text']=='Interrupted upload'
    monkeypatch.setattr('family_photo.archive.time.time',lambda:stamp+301)
    archive.collect_garbage()
    with pytest.raises(Exception):archive.objects.read_json(key)
    assert archive.history('-1001')['items'][0]['text']=='Семейное фото'


def test_concurrent_outbox_retries_produce_one_delivery(archive):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=6) as pool:
        results=list(pool.map(lambda _:archive.enqueue('-1001','actor','Мария','concurrent','Привет'),range(12)))
    assert len({r['id'] for r in results})==1
    with archive.engine.connect() as c:assert c.execute(select(func.count()).select_from(outbox)).scalar_one()==1


def test_idle_and_inaccessible_groups_are_not_polled_in_a_busy_loop(config,archive):
    import time
    client=SimpleNamespace(get_messages=AsyncMock(side_effect=AssertionError('Should not call Telegram')))
    sync=Synchronizer(config,archive,client)
    archive.update_group('-1001',history_complete=True,last_sync=int(time.time()))
    assert asyncio.run(sync.scan('-1001'))==0
    archive.update_group('-1001',history_complete=False,retry_at=int(time.time())+300)
    assert asyncio.run(sync.scan('-1001'))==0
    assert client.get_messages.await_count==0


def test_history_page_keeps_whole_album_and_exact_reply_message_ids(archive):
    for mid in range(1,14):archive.ingest('-1001',photo(mid,album_id='album' if mid>=4 else None))
    page=archive.history('-1001',limit=5)
    assert [m['id'] for m in page['items']]==list(range(4,14)) and page['more']
    previous=archive.history('-1001',before=4,limit=5)
    assert [m['id'] for m in previous['items']]==[1,2,3] and not previous['more']
    archive.delete_messages('-1001',[8])
    assert 8 not in [m['id'] for m in archive.history('-1001',limit=5)['items']]
