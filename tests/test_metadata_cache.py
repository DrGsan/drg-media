from dataclasses import replace
import json
import os
import threading
import socket
import time

import pytest
from sqlalchemy import select

from conftest import message
from family_photo.api import create_app
from family_photo.archive import Archive
from family_photo.config import Config
from family_photo.db import database, expirations, group_management, messages, site_parts, site_sends
from family_photo.metadata_cache import MetadataCache, REVISION_ID


class FakeRedis:
    def __init__(self):
        self.values={}
        self.gets=self.sets=0
        self.failure=None

    def get(self,key):
        self.gets+=1
        if self.failure:raise self.failure
        return self.values.get(key)

    def set(self,key,value,*,ex):
        self.sets+=1
        if self.failure:raise self.failure
        assert 1<=ex<=300
        self.values[key]=value


@pytest.fixture
def cached(config,archive):
    cfg=replace(config,redis_url='redis://private-cache:6379/0')
    app=create_app(cfg,archive)
    backend=FakeRedis()
    cache=app.extensions['metadata_cache'];cache.client=backend
    client=app.test_client();client.environ_base['HTTP_AUTHORIZATION']='Bearer '+config.service_token
    return app,client,cache,backend


def states(client,ids=(1,),gid='-1001'):
    return client.post('/v1/groups/'+gid+'/message-states',json={'ids':list(ids)})


def revision(engine):
    with engine.connect() as c:
        return c.execute(select(group_management.c.value).where(group_management.c.id==REVISION_ID)).scalar()


def test_disabled_cache_needs_no_driver_connection_or_revision(app,client,archive,monkeypatch):
    monkeypatch.setattr('family_photo.metadata_cache.MetadataCache._client',lambda *a:pytest.fail('disabled cache connects'))
    archive.ingest('-1001',message())
    assert states(client).status_code==200
    assert revision(archive.engine) is None
    assert client.get('/v1/status').json['metadata_cache']['state']=='disabled'


def test_hits_preserve_response_and_do_not_read_objects(cached,archive,monkeypatch):
    app,client,cache,backend=cached
    archive.ingest('-1001',message())
    initial=states(client).json
    assert backend.sets==1
    monkeypatch.setattr(archive.objects,'read_json',lambda *a:pytest.fail('metadata reads objects'))
    with monkeypatch.context() as patch:
        patch.setattr(app.extensions['site_messaging'],'message_states',lambda *a:pytest.fail('hit queries metadata'))
        assert states(client,ids=(1,1)).json==initial
    assert cache.public()['hits']==1
    assert states(client,ids=(1,),gid='-1002').json=={'items':[]}


def test_worker_without_redis_and_direct_core_writes_invalidate(cached,archive,config):
    _,client,cache,backend=cached
    archive.ingest('-1001',message())
    initial=states(client).json;before=revision(archive.engine)
    engine=database(config.database_url)
    try:
        worker=Archive(engine,archive.objects)
        worker.ingest('-1001',message(text='Worker edit',edited=123))
        assert revision(engine)!=before
        assert states(client).json!=initial
        with engine.begin() as c:
            c.execute(messages.update().where(messages.c.group_id=='-1001',messages.c.id==1).values(deleted=True))
        assert states(client).json=={'items':[]}
    finally:
        engine.dispose()
    assert backend.sets==3


def test_expiry_disabling_and_auth_are_checked_before_hits(cached,archive):
    app,client,cache,backend=cached
    archive.ingest('-1001',message())
    assert len(states(client).json['items'])==1
    assert app.test_client().post('/v1/groups/-1001/message-states',json={'ids':[1]}).status_code==401
    archive.registry.action('-1001','disable')
    assert states(client).status_code==404
    archive.registry.action('-1001','enable')
    assert states(client).status_code==200
    with archive.engine.begin() as c:
        c.execute(expirations.insert().values(group_id='-1001',message_id=1,expires=1))
    assert states(client).json=={'items':[]}


def test_confirmed_bot_revision_and_sender_changes_invalidate(cached,archive):
    _,client,_,_=cached
    archive.ingest('-1001',message(sender_id='77'))
    with archive.engine.begin() as c:
        c.execute(site_sends.insert().values(id='job',group_id='-1001',actor_id='a'*32,
            author='A',client_id='job',content_key='unused',value=json.dumps({'revision':1})))
        c.execute(site_parts.insert().values(group_id='-1001',message_id=1,send_id='job',bot_id=77,ordinal=0))
    first=states(client).json['items'][0]['fingerprint'];assert first.endswith(':1')
    with archive.engine.begin() as c:
        c.execute(site_sends.update().where(site_sends.c.id=='job').values(value=json.dumps({'revision':2})))
    assert states(client).json['items'][0]['fingerprint'].endswith(':2')
    with archive.engine.begin() as c:
        c.execute(messages.update().where(messages.c.group_id=='-1001',messages.c.id==1).values(sender_id='88'))
    assert ':site:' not in states(client).json['items'][0]['fingerprint']


def test_rollback_and_savepoint_do_not_publish_uncommitted_data(cached,archive):
    _,client,cache,backend=cached
    archive.ingest('-1001',message())
    original=states(client).json;first=revision(archive.engine)
    with archive.engine.connect() as c:
        tx=c.begin()
        c.execute(messages.update().where(messages.c.group_id=='-1001').values(edited=55))
        before=backend.sets
        loaded=cache.load(c,'-1001',[1],lambda:[{'id':1,'edited':55,'fingerprint':'uncommitted'}])
        assert loaded[0]['edited']==55 and backend.sets==before
        tx.rollback()
    assert revision(archive.engine)==first and states(client).json==original
    with archive.engine.begin() as c:
        nested=c.begin_nested()
        c.execute(messages.update().where(messages.c.group_id=='-1001').values(edited=77))
        nested.rollback()
        c.execute(messages.update().where(messages.c.group_id=='-1001').values(edited=88))
    assert revision(archive.engine)!=first
    assert states(client).json['items'][0]['edited']==88


def test_failed_get_bypasses_for_cooldown_then_recovers_with_fresh_revision(cached,archive,monkeypatch,caplog):
    _,client,cache,backend=cached
    archive.ingest('-1001',message());original=states(client).json
    backend.failure=TimeoutError('redis://user:SECRET@private-cache')
    now=1000.;monkeypatch.setattr('family_photo.metadata_cache.time.monotonic',lambda:now)
    assert states(client).json==original
    attempts=backend.gets
    archive.ingest('-1001',message(text='Changed during outage',edited=22))
    backend.failure=None
    changed=states(client).json
    assert changed!=original and backend.gets==attempts
    now+=6
    assert states(client).json==changed and backend.gets==attempts+1
    assert cache.public()['state']=='ready'
    assert 'SECRET' not in caplog.text


def test_failed_set_and_corrupt_values_fall_back(cached,archive):
    _,client,cache,backend=cached
    archive.ingest('-1001',message());original=states(client).json
    key=next(iter(backend.values))
    for malformed in (b'invalid JSON',b'{}',b'[{"id":true}]',b'[{"id":999,"edited":0,"fingerprint":"wrong"}]'):
        backend.values[key]=malformed
        assert states(client).json==original
    backend.values.clear()
    def bad_set(*a,**k):raise ConnectionError('set failed')
    backend.set=bad_set
    assert states(client).json==original
    assert cache.public()['state']=='fallback'


def test_missing_optional_driver_keeps_api_available(cached,archive,monkeypatch):
    import builtins
    _,client,cache,_=cached
    archive.ingest('-1001',message())
    original=builtins.__import__
    def missing(name,*args,**kwargs):
        if name=='redis':raise ImportError('optional driver missing')
        return original(name,*args,**kwargs)
    cache.client=None
    monkeypatch.setattr(builtins,'__import__',missing)
    assert states(client).status_code==200
    assert cache.public()['state']=='fallback'


def test_unresponsive_tcp_peer_times_out_and_does_not_stall_next_request(cached,archive):
    _,client,cache,_=cached
    archive.ingest('-1001',message())
    accepted=threading.Event();done=threading.Event()
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));listener.listen(1);listener.settimeout(2)
        def silent_peer():
            conn,_=listener.accept()
            with conn:
                accepted.set();done.wait(2)
        thread=threading.Thread(target=silent_peer,daemon=True);thread.start()
        cache.client=None;cache.url=f'redis://127.0.0.1:{listener.getsockname()[1]}/0'
        try:
            start=time.perf_counter()
            assert states(client).status_code==200
            assert time.perf_counter()-start<1
            assert accepted.wait(1) and cache.public()['state']=='fallback'
            assert states(client).status_code==200
            assert cache.public()['failures']==1
        finally:
            done.set();thread.join(3)
            if cache.client:cache.client.close()
        assert not thread.is_alive()


def test_revision_change_during_cache_fill_cannot_poison_new_key(cached,archive):
    _,client,cache,backend=cached
    archive.ingest('-1001',message())
    entered=threading.Event();resume=threading.Event()
    initial_get=backend.get
    def paused_get(key):
        entered.set();assert resume.wait(5)
        return initial_get(key)
    backend.get=paused_get
    result=[]
    def request():
        try:result.append(states(client).json)
        except Exception as error:result.append(error)
    thread=threading.Thread(target=request);thread.start()
    assert entered.wait(5)
    try:
        archive.ingest('-1001',message(text='Committed while reader paused',edited=99))
    finally:
        resume.set();thread.join(5)
    assert not thread.is_alive() and len(result)==1 and isinstance(result[0],dict)
    backend.get=initial_get
    assert states(client).json['items'][0]['edited']==99


def test_writer_commit_after_loader_prevents_late_stale_fill(cached,archive,monkeypatch):
    app,client,cache,backend=cached
    archive.ingest('-1001',message())
    entered=threading.Event();resume=threading.Event()
    original=app.extensions['site_messaging'].message_states
    def paused_loader(*args):
        result=original(*args)
        entered.set();assert resume.wait(5)
        return result
    monkeypatch.setattr(app.extensions['site_messaging'],'message_states',paused_loader)
    results=[]
    thread=threading.Thread(target=lambda:results.append(states(client).json));thread.start()
    assert entered.wait(5)
    try:archive.ingest('-1001',message(text='Later commit',edited=111))
    finally:resume.set();thread.join(5)
    assert not thread.is_alive() and results[0]['items'][0]['edited']==0
    assert backend.sets==0
    monkeypatch.setattr(app.extensions['site_messaging'],'message_states',original)
    assert states(client).json['items'][0]['edited']==111


def test_config_optional_and_private(config,monkeypatch):
    monkeypatch.setenv('FP_SERVICE_TOKEN',config.service_token)
    monkeypatch.setenv('FP_REDIS_URL','rediss://user:SECRET@private-cache:6380/0')
    parsed=Config.from_env();parsed.validate()
    assert parsed.redis_url.startswith('rediss://') and 'SECRET' not in repr(parsed)
    for url in ('https://cache','redis://','redis://cache:bad/0','redis://cache/0?socket_timeout=99'):
        with pytest.raises(ValueError):replace(config,redis_url=url).validate()
    for ttl in (0,301,True):
        with pytest.raises(ValueError):replace(config,redis_cache_ttl=ttl).validate()


@pytest.mark.skipif(not os.getenv('FP_TEST_REDIS_URL'),reason='optional live Redis integration')
def test_real_redis_cache_and_recovery(config,archive):
    app=create_app(replace(config,redis_url=os.environ['FP_TEST_REDIS_URL']),archive)
    client=app.test_client();client.environ_base['HTTP_AUTHORIZATION']='Bearer '+config.service_token
    archive.ingest('-1001',message())
    assert states(client).status_code==200
    assert states(client).status_code==200
    cache=app.extensions['metadata_cache'];assert cache.public()['hits']==1
    archive.ingest('-1001',message(text='Real Redis edit',edited=33))
    assert states(client).json['items'][0]['edited']==33
    assert cache.public()['misses']==2
    working=cache.client
    from redis import Redis
    from redis.retry import Retry
    from redis.backoff import NoBackoff
    failed=Redis.from_url('redis://127.0.0.1:1/0',socket_connect_timeout=.05,
                         socket_timeout=.05,retry=Retry(NoBackoff(),0))
    try:
        cache.client=failed
        archive.ingest('-1001',message(text='Edit during real connection failure',edited=44))
        assert states(client).json['items'][0]['edited']==44
        assert cache.public()['state']=='fallback'
        cache.client=working;cache.unavailable_until=0
        assert states(client).json['items'][0]['edited']==44
        assert cache.public()['state']=='ready'
    finally:
        failed.close()
        working.close()
