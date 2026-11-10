import asyncio
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import select, func
from conftest import photo, message
from family_photo.api import create_app
from family_photo.db import groups, messages, media, outbox, people, emoji_assets, group_profiles, garbage
from family_photo.group_management import Registry, ManagedConfig, GroupInactive, PERIOD
from family_photo.telegram import Synchronizer, send_outbox


def test_disable_is_durable_stops_access_writes_and_bot_but_keeps_data(client,archive,config):
    archive.ingest('-1001',photo())
    archive.enqueue('-1001','actor','Author','first','queued text')
    registry=archive.registry
    assert client.post('/v1/admin/groups/-1001/disable',json={}).status_code==200
    assert [g['id'] for g in client.get('/v1/groups').json['items']]==['-1002']
    assert client.get('/v1/groups/-1001/messages').status_code==404
    assert client.get('/v1/groups/-1001/sync').status_code==404
    for operation in [lambda:archive.ingest('-1001',message(2)),lambda:archive.add_group('-1001','Changed'),
        lambda:archive.save_person('-1001','1',name='Oops'),lambda:archive.queue_emoji('-1001',['123']),
        lambda:archive.enqueue('-1001','actor','Author','second','blocked')]:
        with pytest.raises(GroupInactive):operation()
    managed=ManagedConfig(config,registry)
    http=SimpleNamespace(post=AsyncMock())
    asyncio.run(send_outbox(managed,archive,http));http.post.assert_not_awaited()
    sync=Synchronizer(managed,archive,SimpleNamespace(get_messages=AsyncMock()))
    with pytest.raises(GroupInactive):asyncio.run(sync.scan('-1001'))
    sync.client.get_messages.assert_not_awaited()
    assert len(archive.history('-1001')['items'])==1
    restarted=create_app(config,archive).test_client();restarted.environ_base['HTTP_AUTHORIZATION']='Bearer '+config.service_token
    assert [g['id'] for g in restarted.get('/v1/groups').json['items']]==['-1002']
    registry.action('-1001','enable')
    assert managed.archive_group_ids==('-1001','-1002') or set(managed.archive_group_ids)=={'-1001','-1002'}
    assert archive.group('-1001')['last_sync']==0
    assert len(client.get('/v1/groups/-1001/messages').json['items'])==1


def test_seven_days_exact_no_extension_and_cancel_restores_previous_state(client,archive):
    r=archive.registry
    r.action('-1001','disable')
    first=r.action('-1001','delete',100)
    assert first['delete_at']==100+PERIOD
    assert r.action('-1001','delete',200)['delete_at']==first['delete_at']
    r.action('-1001','cancel',100+PERIOD-1)
    assert r.active('-1001') is False
    r.action('-1001','enable');r.action('-1001','delete',100)
    with pytest.raises(ValueError):r.action('-1001','cancel',100+PERIOD)
    with pytest.raises(ValueError):r.action('-1001','enable',101)
    r.maintenance(100+PERIOD-1);assert archive.group('-1001')
    r.maintenance(100+PERIOD);assert archive.group('-1001') is None
    # Environment import is strictly one-time, including after deletion.
    Registry(archive,r.config)
    assert not r.active('-1001') and archive.group('-1001') is None


def test_discovery_verified_selection_immutable_ids_and_new_group_during_worker_lifetime(client,archive,config):
    r=archive.registry
    assert client.post('/v1/admin/groups',json={'telegram_id':'-1003'}).status_code==409
    assert client.post('/v1/admin/groups',json={'telegram_id':'-1003','title':'Forged'}).status_code==422
    r.request_discovery();r.publish_discovery([{'id':'-1003','title':'Third','kind':'supergroup'}])
    managed=ManagedConfig(config,r)
    sync=Synchronizer(managed,archive,None)
    assert '-1003' not in sync.config.archive_group_ids
    assert client.post('/v1/admin/groups',json={'telegram_id':'-1003'}).status_code==201
    assert '-1003' in sync.config.archive_group_ids
    assert archive.group('-1003')['title']=='Third'
    assert client.post('/v1/admin/groups',json={'telegram_id':'-1003'}).status_code==409
    assert client.post('/v1/admin/groups/-1003/enable',json={'telegram_id':'-1004'}).status_code==422
    assert client.put('/v1/admin/groups/-1003',json={'telegram_id':'-1004'}).status_code in {404,405}
    assert client.get('/v1/groups/-1004/messages').status_code==404


def test_final_purge_removes_all_owned_records_files_and_keeps_other_group(client,archive,config,tmp_path,monkeypatch):
    r=archive.registry
    archive.ingest('-1001',photo());archive.ingest('-1002',message(2,text='Keep'))
    archive.enqueue('-1001','actor','Author','outgoing','secret')
    path=tmp_path/'file';path.write_bytes(b'photo')
    with archive.engine.connect() as c:item=dict(c.execute(select(media).where(media.c.group_id=='-1001')).mappings().one())
    with archive.upload('media') as key,archive.upload('thumbnails') as thumb:
        archive.objects.put_file(key,path);archive.objects.put_file(thumb,path)
        archive.attach_download(item,key,thumb,5)
    with archive.upload('avatars') as avatar:
        archive.objects.put_file(avatar,path);archive.save_group_profile('-1001',avatar_key=avatar)
        archive.save_person('-1001','42',name='Person',avatar_key=avatar)
    with archive.upload('emojis') as emoji:
        archive.objects.put_file(emoji,path);archive.queue_emoji('-1001',['123'])
        archive.save_emoji('-1001','123',object_key=emoji,state='ready')
    r.action('-1001','delete',100)
    r.maintenance(100+PERIOD)
    assert r.read()['groups']['-1001']['phase']=='purging'
    for table in (messages,media,outbox,group_profiles,people,emoji_assets):
        with archive.engine.connect() as c:assert not c.execute(select(table).where(table.c.group_id=='-1001')).first()
    original=archive.objects.delete
    monkeypatch.setattr(archive.objects,'delete',lambda k:(_ for _ in ()).throw(OSError('offline')))
    assert archive.collect_garbage()['errors']>0
    r.maintenance(100+PERIOD+1);assert archive.group('-1001')
    monkeypatch.setattr(archive.objects,'delete',original)
    archive.collect_garbage();r.maintenance(100+PERIOD+2)
    assert archive.group('-1001') is None
    assert archive.history('-1002')['items'][0]['text']=='Keep'
    state=r.read();assert '-1001' in state['purged']
    token=state['purged']['-1001']
    client.post('/v1/admin/purged/ack',json={'purged':{'-1001':'stale'}})
    assert r.read()['purged']['-1001']==token
    assert client.post('/v1/admin/purged/ack',json={'purged':{'-1001':token}}).status_code==200
    assert r.read()['purged']=={}
    for object_key in (key,thumb,avatar,emoji):
        with pytest.raises(Exception):archive.objects.size(object_key)


def test_registry_preserves_export_binding_and_accepts_empty_new_install(config,archive):
    gid='export:abcdef1234567890'
    archive.add_group(gid,'Import',kind='export');archive.ingest(gid,message())
    cfg=replace(config,group_ids=('-1003',),group_bindings=(('-1003',gid),))
    r=Registry(archive,cfg);managed=ManagedConfig(cfg,r)
    assert managed.telegram_id(gid)=='-1003'
    assert managed.archive_id('-1003')==gid
    assert managed.archive_group_ids==(gid,)
    assert archive.history(gid)['items']
    r.action(gid,'disable');assert managed.archive_group_ids==()
    assert r.read()['groups'][gid]['telegram_id']=='-1003'


def test_versioned_s3_deletion_removes_every_version_and_marker(config,tmp_path):
    import boto3
    from moto import mock_aws
    from family_photo.objects import Objects
    with mock_aws():
        s3=boto3.client('s3',region_name='us-east-1');s3.create_bucket(Bucket='versioned-family-test')
        s3.put_bucket_versioning(Bucket='versioned-family-test',VersioningConfiguration={'Status':'Enabled'})
        objects=Objects(replace(config,storage='s3',bucket='versioned-family-test'),s3)
        key='media/'+'f'*32;remote=objects.key(key)
        for body in (b'old',b'new'):s3.put_object(Bucket=objects.config.bucket,Key=remote,Body=body)
        s3.delete_object(Bucket=objects.config.bucket,Key=remote)
        objects.delete(key)
        response=s3.list_object_versions(Bucket=objects.config.bucket,Prefix=remote)
        assert not response.get('Versions') and not response.get('DeleteMarkers')


def test_additive_schema_migration_and_rollback_preserve_existing_rows_and_objects(config,archive):
    from family_photo.db import group_management,metadata
    archive.ingest('-1001',message());archive.ingest('-1002',photo(2))
    legacy_tables=[table for table in metadata.sorted_tables if table is not group_management]
    def snapshot():
        with archive.engine.connect() as c:
            return {table.name:sorted([tuple(row) for row in c.execute(select(table))],key=repr) for table in legacy_tables}
    before=snapshot();usage=archive.objects.usage()
    group_management.drop(archive.engine)
    group_management.create(archive.engine)
    r=Registry(archive,config)
    assert snapshot()==before and archive.objects.usage()==usage
    assert set(r.read()['groups'])=={'-1001','-1002'}
    group_management.drop(archive.engine)
    assert snapshot()==before and archive.objects.usage()==usage
    group_management.create(archive.engine)
    Registry(archive,config)
    assert snapshot()==before


def test_disable_during_scan_never_advances_cursor_or_reintroduces_records(client,archive,config):
    registry=archive.registry
    async def latest(*args,**kwargs):
        registry.action('-1001','disable')
        return []
    sync=Synchronizer(ManagedConfig(config,registry),archive,SimpleNamespace(get_messages=latest))
    with pytest.raises(GroupInactive):asyncio.run(sync.scan('-1001'))
    assert archive.group('-1001')['cursor']==0
    assert archive.group('-1001')['last_sync']==0
    registry.action('-1001','enable')
    assert registry.active('-1001')
