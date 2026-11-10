from dataclasses import replace
import pytest
from sqlalchemy import select
from family_photo.db import media, outbox
from family_photo.api import create_app
from conftest import message,photo


def test_gallery_version_tracks_visible_metadata_without_object_reads(client,archive,monkeypatch):
    from family_photo.db import messages
    archive.ingest('-1001',photo(2));archive.ingest('-1001',photo(3))
    url='/v1/groups/-1001/media-version?through=2'
    version=lambda:client.get(url).json['version']
    first=version()
    assert client.get('/v1/status').json['gallery_versions']==1
    assert first==client.get('/v1/groups/-1001/media-version').json['version']
    archive.ingest('-1001',photo(1))
    with_more=version();assert with_more!=first
    # An unloaded older photo's caption does not invalidate this window.
    archive.ingest('-1001',photo(1,text='Исправление старой подписи'))
    assert version()==with_more
    with archive.engine.begin() as c:
        c.execute(media.update().where(media.c.group_id=='-1001',media.c.message_id==2).values(state='ready',thumb_key='thumbs/'+'a'*32))
    ready=version();assert ready!=with_more
    with archive.engine.begin() as c:
        c.execute(messages.update().where(messages.c.group_id=='-1001',messages.c.id==2).values(sender='Новое имя',album_id='album'))
    renamed=version();assert renamed!=ready
    archive.delete_messages('-1001',[2]);deleted=version();assert deleted!=renamed
    monkeypatch.setattr(archive.objects,'read_json',lambda *args:pytest.fail('version endpoint reads objects'))
    assert version()==deleted
    archive.ingest('-1002',photo(4))
    assert version()==deleted
    assert client.get('/v1/groups/-1001/media-version?through=bad').status_code==422
    assert client.get('/v1/groups/-999/media-version').status_code==404


def test_permission_ack_only_confirms_accepted_revision(client):
    assert client.put('/v1/site-permissions',json={'revision':10,'actors':{}}).json=={'ok':True,'revision':10}
    assert client.put('/v1/site-permissions',json={'revision':9,'actors':{}}).json=={'ok':True,'revision':None}


def test_internal_auth_and_no_public_mutations(app,client,archive):
    assert app.test_client().get('/v1/groups').status_code==401
    assert app.test_client().get('/healthz').status_code==200
    assert client.get('/v1/status').json['api_version']==1
    from family_photo import __version__
    assert client.get('/v1/status').json['service_version']==__version__
    assert app.test_client().get('/v1/status').status_code==401
    assert client.delete('/v1/groups/-1001/messages/1').status_code==405
    assert client.get('/v1/groups').headers['Cache-Control']=='no-store'


def test_pagination_and_scoped_access(client,archive):
    archive.ingest('-1001',photo(1));archive.ingest('-1002',photo(2))
    data=client.get('/v1/groups/-1001/messages').json
    assert [r['id'] for r in data['items']]==[1]
    assert client.get('/v1/groups/-1001/messages/2').status_code==404
    assert client.get('/v1/groups/-999/messages').status_code==404
    assert client.get('/v1/groups/-1001/media?before=bad').status_code==422
    assert client.get('/v1/groups/-1001/media?limit=10000').status_code==422
    assert client.get('/v1/groups/-1001/media').json['items'][0]['state']=='pending'


@pytest.mark.parametrize('range_header,status,body',[
    ('bytes=2-5',206,b'2345'),('bytes=7-',206,b'789'),('bytes=-3',206,b'789'),
    ('bytes=0-999',206,b'0123456789'),('bytes=20-',416,b''),('bytes=4-1',416,b''),
    ('bytes=0-1,4-5',416,b'')])
def test_video_range_streaming_and_deletion(client,archive,range_header,status,body):
    archive.ingest('-1001',photo(1))
    with archive.engine.connect() as c:item=dict(c.execute(select(media)).mappings().one())
    path=archive.objects.config.data_dir/'sample';path.write_bytes(b'0123456789')
    archive.objects.put_file('media/'+'a'*32,path)
    archive.attach_download(item,'media/'+'a'*32,None,10)
    url='/v1/groups/-1001/media/'+item['id']
    result=client.get(url,headers={'Range':range_header})
    assert result.status_code==status and result.data==body
    assert client.get('/v1/groups/-1002/media/'+item['id']).status_code==404
    archive.delete_messages('-1001',[1])
    assert client.get(url).status_code==404


def test_outgoing_actor_validation_and_idempotence(client,archive):
    payload={'actor_id':'actor','author':'Мария','text':'Привет','client_id':'a1'}
    first=client.post('/v1/groups/-1001/messages',json=payload)
    assert first.status_code==202
    assert client.post('/v1/groups/-1001/messages',json=payload).json==first.json
    assert client.post('/v1/groups/-1001/messages',json=payload|{'text':'Другое'}).status_code==409
    assert client.post('/v1/groups/-1001/messages',json=payload|{'author':'spoof\nname'}).status_code==422
    assert client.post('/v1/groups/-1001/messages',json=payload|{'reply_to':True}).status_code==422
    assert client.post('/v1/groups/-1001/messages',json=payload|{'surprise':1}).status_code==422


def test_bot_configuration_is_optional(config,archive):
    app=create_app(replace(config,bot_token=''),archive)
    c=app.test_client();c.environ_base['HTTP_AUTHORIZATION']='Bearer '+config.service_token
    assert not c.get('/v1/status').json['can_send']
    assert c.post('/v1/groups/-1001/messages',json={}).status_code==503


def test_configuration_rejects_missing_shared_secret(config):
    with pytest.raises(ValueError):create_app(replace(config,service_token=''))
    with pytest.raises(ValueError):create_app(replace(config,storage='s3',bucket=''))


def test_pending_deliveries_survive_reload_and_are_actor_scoped(client,archive):
    archive.enqueue('-1001','one','Анна','a1','Комментарий Анны')
    archive.enqueue('-1001','two','Мария','b1','Комментарий Марии')
    archive.enqueue('-1002','one','Анна','c1','Другая группа')
    data=client.get('/v1/groups/-1001/outbox?actor_id=one').json
    assert len(data['items'])==1 and data['items'][0]['text']=='Комментарий Анны'
    assert client.get('/v1/groups/-1001/outbox').status_code==422


def test_basic_groups_remain_readable_but_never_use_wrong_bot_message_ids(client,archive):
    archive.add_group('-1001','Old basic group',kind='basic')
    archive.ingest('-1001',message(1))
    assert client.get('/v1/groups/-1001/messages').status_code==200
    assert client.post('/v1/groups/-1001/messages',json={'actor_id':'actor','author':'Анна','text':'Ответ','client_id':'basic'}).status_code==422
    assert not next(g for g in client.get('/v1/groups').json['items'] if g['id']=='-1001')['can_send']


def test_storage_statistics_count_real_objects_cache_and_require_auth(app,client,archive,monkeypatch):
    from family_photo import api
    source=archive.objects.config.data_dir/'usage-test'
    for i,(kind,size) in enumerate([('media',19),('thumbnails',7),('messages',11),('avatars',5),('outbox',3)]):
        source.write_bytes(b'x'*size)
        archive.objects.put_file(kind+'/'+str(i)*32,source)
    assert app.test_client().get('/v1/storage').status_code==401
    first=client.get('/v1/storage');assert first.status_code==200
    usage=first.json
    assert usage['objects']==5 and usage['objects_bytes']==45
    assert usage['media_bytes']==19 and usage['thumbnail_bytes']==7
    assert usage['message_bytes']==14 and usage['avatar_bytes']==5 and usage['other_bytes']==0
    assert usage['database_bytes']>0 and usage['total_bytes']==45+usage['database_bytes']
    assert not any(key in usage for key in ('bucket','prefix','database_url','token','path'))
    source.write_bytes(b'extra');archive.objects.put_file('media/'+'f'*32,source)
    assert client.get('/v1/storage').json==usage
    current=api.time.monotonic();monkeypatch.setattr(api.time,'monotonic',lambda:current+61)
    assert client.get('/v1/storage').json['objects_bytes']==50


def test_storage_inventory_paginated_s3_is_scoped_to_service_prefix(config):
    from family_photo.objects import Objects
    class Paginator:
        def paginate(self,**kwargs):
            assert kwargs=={'Bucket':'private-test','Prefix':'family-photo/'}
            return [{'Contents':[{'Key':'family-photo/media/'+'a'*32,'Size':5*1024**3}]},
                    {'Contents':[{'Key':'family-photo/thumbnails/'+'b'*32,'Size':23}]}]
    class S3:
        def get_paginator(self,name):
            assert name=='list_objects_v2';return Paginator()
    store=Objects(replace(config,storage='s3',bucket='private-test'),S3())
    usage=store.usage()
    assert usage['objects']==2 and usage['objects_bytes']==5*1024**3+23
    assert usage['media_bytes']==5*1024**3 and usage['thumbnail_bytes']==23


def test_notification_metadata_and_unread_respect_scope_and_deletions(client,archive):
    archive.ingest('-1001',message(1,sender_id='42'))
    archive.ingest('-1001',photo(2))
    archive.ingest('-1002',message(3))
    assert next(g for g in client.get('/v1/groups').json['items'] if g['id']=='-1001')['latest_message_id']==2
    assert client.get('/v1/groups/-1001/unread?after=1').json=={'unread':1}
    assert client.get('/v1/groups/-1001/unread?after=0&exclude_sender=42').json=={'unread':0}
    assert [m['id'] for m in client.get('/v1/groups/-1001/updates?after=0&limit=1').json['items']]==[1]
    assert client.get('/v1/groups/-999/updates').status_code==404
    assert client.get('/v1/groups/-1001/unread?exclude_sender=bad').status_code==422
    data=client.post('/v1/groups/-1001/message-states',json={'ids':[1,2,3]}).json
    assert [m['id'] for m in data['items']]==[1,2]
    assert client.post('/v1/groups/-1001/message-states',json={'ids':[True]}).status_code==422
    archive.delete_messages('-1001',[2])
    assert client.get('/v1/groups/-1001/unread?after=1').json=={'unread':0}
    assert client.post('/v1/groups/-1001/message-states',json={'ids':[2]}).json=={'items':[]}
