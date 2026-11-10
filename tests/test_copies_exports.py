import asyncio
import io
import json
import time
import uuid
import zipfile
from datetime import datetime,timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import select,func
from conftest import message,photo
from family_photo.db import messages,media,expirations,export_files,export_jobs,garbage
from family_photo.group_management import Registry,PERIOD
from family_photo.exports import Exports,StreamWriter
from family_photo.telegram import Synchronizer


def ready(archive,config,mid=1,body=b'original photo'):
    archive.ingest('-1001',photo(mid))
    with archive.engine.connect() as c: item=dict(c.execute(select(media).where(media.c.message_id==mid)).mappings().one())
    path=config.data_dir/'photo';path.write_bytes(body)
    with archive.upload('media') as key:
        archive.objects.put_file(key,path)
        archive.attach_download(item,key,None,len(body))
    return key


def test_snapshot_is_atomic_idempotent_and_independent_of_source(config,archive):
    key=ready(archive,config)
    registry=Registry(archive,config)
    request=uuid.uuid4().hex
    copied=registry.copy('-1001',request)
    assert registry.copy('-1001',request)==copied
    archive.delete_messages('-1001',[1])
    archive.collect_garbage()
    assert b''.join(archive.objects.read(key))==b'original photo'
    assert archive.history(copied)['items'][0]['media'][0]['state']=='ready'
    assert not dict(registry.read()['groups'][copied]).get('telegram_id')
    registry.action(copied,'disable');assert not registry.active(copied)
    registry.action(copied,'enable');registry.action(copied,'delete',stamp=0)
    registry.maintenance(stamp=PERIOD);archive.collect_garbage();registry.maintenance(stamp=PERIOD)
    assert copied not in registry.read()['groups']
    with pytest.raises(Exception):b''.join(archive.objects.read(key))


def test_ttl_messages_restore_only_after_confirmation_and_expire_without_worker(config,archive,monkeypatch):
    stamp=int(time.time())
    archive.delete_messages('-1001',[1,2])
    sync=Synchronizer(config,archive,SimpleNamespace())
    source=SimpleNamespace(id=1,date=datetime.fromtimestamp(stamp,timezone.utc),ttl_period=100)
    monkeypatch.setattr('family_photo.telegram.normalize',AsyncMock(return_value=message(1)|{'created':stamp}))
    asyncio.run(sync.apply('-1001',source))
    assert [m['id'] for m in archive.history('-1001')['items']]==[1]
    with archive.engine.connect() as c:assert c.execute(select(expirations.c.expires)).scalar_one()==stamp+100
    registry=Registry(archive,config);copied=registry.copy('-1001',uuid.uuid4().hex)
    monkeypatch.setattr('time.time',lambda:stamp+101)
    archive.expire_messages()
    assert not archive.history('-1001')['items']
    assert archive.history(copied)['items']
    # Non-TTL source events cannot resurrect ordinary tombstones.
    asyncio.run(sync.apply('-1001',SimpleNamespace(id=1,date=source.date)))
    assert not archive.history('-1001')['items']
    source.media=SimpleNamespace(ttl_seconds=1)
    monkeypatch.setattr('time.time',lambda:stamp)
    asyncio.run(sync.apply('-1001',source))
    assert not archive.history('-1001')['items']


def setup_job(config,archive):
    key=ready(archive,config)
    registry=getattr(archive,'registry',None) or Registry(archive,config)
    jobs=Exports(archive,registry)
    job=jobs.create('a'*32,['-1001'],False,uuid.uuid4().hex)
    return registry,jobs,job,key


def test_zip_pins_files_then_supports_ranges_and_expires(client,app,config,archive,monkeypatch):
    registry,jobs,job,key=setup_job(config,archive)
    assert jobs.create('a'*32,['-1001'],False,uuid.uuid4().hex)['id']==job['id']
    assert 'file_dates' not in job
    archive.delete_messages('-1001',[1]);archive.collect_garbage()
    assert b''.join(archive.objects.read(key))==b'original photo'
    jobs.tick();prepared=jobs.read(job['id'])
    assert prepared['state']=='ready'
    assert prepared['expires']==prepared['ready_at']+PERIOD
    blob=b''.join(archive.objects.read(prepared['object_key']))
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        assert z.testzip() is None
        assert b'original photo' in [z.read(name) for name in z.namelist()]
    url='/v1/exports/'+job['id']+'/download?actor_id='+'a'*32
    result=client.get(url,headers={'Range':'bytes=5-19'})
    assert result.status_code==206 and result.data==blob[5:20]
    assert client.get(url.replace('a'*32,'b'*32)).status_code==404
    archive.collect_garbage()
    with pytest.raises(Exception):b''.join(archive.objects.read(key))
    monkeypatch.setattr('time.time',lambda:prepared['expires'])
    assert client.get(url).status_code==410
    jobs.maintain();archive.collect_garbage();jobs.maintain()
    assert jobs.read(job['id'])['state']=='cleaned'
    with pytest.raises(Exception):b''.join(archive.objects.read(prepared['object_key']))


@pytest.mark.parametrize('failure',['read','write','cancel','restart','disabled'])
def test_failure_cleanup_is_durable_and_never_deletes_original(config,archive,monkeypatch,failure):
    registry,jobs,job,key=setup_job(config,archive)
    if failure=='read':monkeypatch.setattr(archive.objects,'read',lambda *a,**kw:(_ for _ in ()).throw(OSError('storage down')))
    if failure=='write':monkeypatch.setattr(StreamWriter,'write',lambda *a:(_ for _ in ()).throw(OSError('disk full')))
    if failure=='cancel':jobs.retire(job['id'],'cancelled')
    if failure=='disabled':registry.action('-1001','disable')
    if failure=='restart':
        with archive.transaction() as c:
            saved=jobs.read(job['id'],c);saved['state']='running';jobs.save(c,saved)
    jobs.tick()
    monkeypatch.undo()
    jobs.maintain();archive.collect_garbage();jobs.maintain()
    assert jobs.read(job['id'])['state']=='cleaned'
    assert b''.join(archive.objects.read(key))==b'original photo'
    with archive.engine.connect() as c:assert c.execute(select(func.count()).select_from(export_files)).scalar_one()==0
    assert not archive.objects.unfinished_uploads()


def test_failed_cleanup_retries_after_s3_returns(config,archive,monkeypatch):
    registry,jobs,job,key=setup_job(config,archive)
    jobs.tick();output=jobs.read(job['id'])['object_key']
    jobs.retire(job['id'],'cancelled');jobs.maintain()
    original=archive.objects.delete
    monkeypatch.setattr(archive.objects,'delete',lambda *a:(_ for _ in ()).throw(OSError('down')))
    archive.collect_garbage();jobs.maintain()
    assert jobs.read(job['id'])['state']=='cleaning'
    monkeypatch.setattr(archive.objects,'delete',original)
    archive.collect_garbage();jobs.maintain()
    assert jobs.read(job['id'])['state']=='cleaned'
    assert b''.join(archive.objects.read(key))==b'original photo'
    with pytest.raises(Exception):b''.join(archive.objects.read(output))


def test_export_request_validation_and_idempotence(client,config,archive):
    ready(archive,config)
    url='/v1/exports?actor_id='+'a'*32
    body={'groups':['-1001'],'videos':False,'request_id':uuid.uuid4().hex}
    result=client.post(url,json=body)
    assert result.status_code==202
    assert client.post(url,json=body).json['id']==result.json['id']
    assert client.post(url,json=body|{'videos':True}).status_code==409
    assert client.post(url,json=body|{'groups':['-1001','-1001']}).status_code==422
    assert client.post(url,json=body|{'groups':['-999'],'request_id':uuid.uuid4().hex}).status_code==404
    assert client.post('/v1/admin/groups/-1001/copy',json={'request_id':uuid.uuid4().hex}).status_code==201


def test_gc_between_retirement_and_maintenance_cannot_strand_zip(config,archive):
    registry,jobs,job,key=setup_job(config,archive)
    jobs.tick();output=jobs.read(job['id'])['object_key']
    jobs.retire(job['id'],'cancelled')
    archive.collect_garbage()  # Another process runs before job maintenance.
    jobs.maintain()
    assert jobs.read(job['id'])['state']=='cleaned'
    with pytest.raises(Exception):b''.join(archive.objects.read(output))


def test_zip64_streaming_and_orphan_multipart_are_cleaned(config,archive,monkeypatch):
    registry,jobs,job,key=setup_job(config,archive)
    monkeypatch.setattr(zipfile,'ZIP64_LIMIT',10)
    jobs.tick();prepared=jobs.read(job['id'])
    assert prepared['state']=='ready'
    with zipfile.ZipFile(io.BytesIO(b''.join(archive.objects.read(prepared['object_key'])))) as z:assert z.testzip() is None
    # Simulate SIGKILL after multipart creation: no finally block gets to run.
    orphan=archive.reserve('exports')
    writer=StreamWriter(archive.objects,orphan,lambda:None)
    writer.write(b'partial')
    writer.close()
    with archive.transaction() as c:c.execute(garbage.update().where(garbage.c.key==orphan).values(created=0))
    archive.collect_garbage()
    assert not archive.objects.unfinished_uploads()
    with pytest.raises(Exception):b''.join(archive.objects.read(orphan))


def test_repair_checks_tombstones_against_telegram_and_keeps_absent_deleted(config,archive,monkeypatch):
    registry=Registry(archive,config)
    archive.delete_messages('-1001',[1,2])
    archive.update_group('-1001',history_complete=True)
    with registry.edit() as (_,control):control['groups']['-1001']['ttl_repair']=True
    now=int(time.time())
    source=SimpleNamespace(id=1,date=datetime.fromtimestamp(now,timezone.utc),ttl_period=300)
    client=SimpleNamespace(get_messages=AsyncMock(return_value=[source,None]))
    sync=Synchronizer(config,archive,client);sync.can_read=AsyncMock(return_value=True)
    monkeypatch.setattr('family_photo.telegram.normalize',AsyncMock(return_value=message(1)|{'created':now}))
    asyncio.run(sync.reconcile('-1001'))
    assert [row['id'] for row in archive.history('-1001')['items']]==[1]
    with archive.engine.connect() as c:assert c.execute(select(messages.c.deleted).where(messages.c.id==2)).scalar_one()
    asyncio.run(sync.reconcile('-1001'))
    assert not registry.read()['groups']['-1001'].get('ttl_repair')


def test_additive_schema_roundtrip_preserves_existing_tables(config,archive):
    from family_photo.db import metadata
    ready(archive,config)
    Registry(archive,config)
    additions=(expirations,export_jobs,export_files)
    originals=[table for table in metadata.sorted_tables if table not in additions]
    def records():
        with archive.engine.connect() as c:return {table.name:sorted(repr(tuple(r)) for r in c.execute(select(table))) for table in originals}
    before=records()
    for table in reversed(additions):table.drop(archive.engine)
    assert records()==before
    for table in additions:table.create(archive.engine)
    assert records()==before


def test_snapshot_cannot_be_copied_through_internal_api(client,config,archive):
    ready(archive,config)
    first=client.post('/v1/admin/groups/-1001/copy',json={'request_id':uuid.uuid4().hex})
    assert first.status_code==201
    copied=first.json['id']
    result=client.post('/v1/admin/groups/'+copied+'/copy',json={'request_id':uuid.uuid4().hex})
    assert result.status_code==409 and result.json['detail']=='copy_of_copy'
    assert archive.history(copied)['items']


def test_disabled_group_can_be_copied_without_reenabling_sync(client,config,archive):
    ready(archive,config)
    registry=archive.registry
    registry.action('-1001','disable')
    before=archive.group('-1001')
    result=client.post('/v1/admin/groups/-1001/copy',json={'request_id':uuid.uuid4().hex})
    assert result.status_code==201
    assert archive.history(result.json['id'])['items'][0]['media'][0]['state']=='ready'
    assert not registry.active('-1001')
    assert archive.group('-1001')==before
    registry.action('-1001','delete')
    assert client.post('/v1/admin/groups/-1001/copy',json={'request_id':uuid.uuid4().hex}).status_code==404


def test_owner_export_events_have_readiness_without_private_object_keys(client,app,config,archive):
    ready(archive,config)
    response=client.post('/v1/exports?actor_id='+'a'*32,json={'groups':['-1001'],'videos':False,'request_id':uuid.uuid4().hex})
    assert response.status_code==202
    app.extensions['exports'].tick()
    job=client.get('/v1/admin/exports').json['items'][0]
    assert job['state']=='ready' and job['expires']>time.time()
    assert job['actor_id']=='a'*32 and job['group_titles']==['Семья 1']
    assert 'object_key' not in job and 'cleanup_key' not in job and 'request_id' not in job
    assert client.get('/v1/status').json['export_notifications']==1


def test_zip_keeps_snapshot_message_dates_and_original_exif_bytes(app,config,archive):
    from PIL import Image
    import struct
    image=Image.new('RGB',(12,12),'green');exif=Image.Exif();exif[306]='2018:06:23 16:49:05'
    stream=io.BytesIO();image.save(stream,format='JPEG',exif=exif);original=stream.getvalue()
    ready(archive,config,body=original)
    jobs=Exports(archive,archive.registry);job=jobs.create('a'*32,['-1001'],False,uuid.uuid4().hex)
    archive.delete_messages('-1001',[1]);jobs.tick();saved=jobs.read(job['id'])
    with zipfile.ZipFile(io.BytesIO(b''.join(archive.objects.read(saved['object_key'])))) as z:
        info=next(i for i in z.infolist() if i.filename!='Состав архива.json')
        assert info.date_time[:5]==datetime.fromtimestamp(1700000001,timezone.utc).timetuple()[:5]
        extra=info.extra;found=None
        while extra:
            tag,size=struct.unpack('<HH',extra[:4]);payload=extra[4:4+size];extra=extra[4+size:]
            if tag==0x5455:found=struct.unpack('<BI',payload)[1]
        assert found==1700000001 and z.read(info)==original
        with Image.open(io.BytesIO(z.read(info))) as restored:assert restored.getexif()[306]=='2018:06:23 16:49:05'


def test_pre_upgrade_queued_zip_without_dates_uses_job_creation(app,config,archive):
    _,jobs,job,_=setup_job(config,archive)
    with archive.transaction() as c:
        saved=jobs.read(job['id'],c);saved.pop('file_dates');jobs.save(c,saved)
    jobs.tick();saved=jobs.read(job['id'])
    with zipfile.ZipFile(io.BytesIO(b''.join(archive.objects.read(saved['object_key'])))) as z:
        info=next(i for i in z.infolist() if i.filename!='Состав архива.json')
        assert info.date_time[:5]==datetime.fromtimestamp(saved['created'],timezone.utc).timetuple()[:5]
