"""Polling cost stays bounded while permissions and metadata remain fresh."""
from contextlib import contextmanager
import hashlib
import json
import time

import pytest
from sqlalchemy import event

from conftest import photo
from family_photo.connection import source_status
from family_photo.db import (bot_access, expirations, groups, media, messages,
                             site_parts, site_sends)


@contextmanager
def selects(engine):
    queries=[]
    def record(c,cursor,statement,parameters,context,many):
        if statement.lstrip().upper().startswith('SELECT'):
            queries.append(statement)
    event.listen(engine,'before_cursor_execute',record)
    try:
        yield queries
    finally:
        event.remove(engine,'before_cursor_execute',record)


@pytest.mark.parametrize('count',[10,1000])
def test_message_states_use_bounded_queries_and_confirm_bot_sender(client,archive,app,count,monkeypatch):
    with archive.engine.begin() as c:
        c.execute(messages.insert(),[
            dict(group_id=gid,id=mid,created=mid,sender='Bot',sender_id='77',fingerprint=f'fp-{mid}')
            for gid in ('-1001','-1002') for mid in range(1,count+1)])
        c.execute(site_sends.insert(),[
            dict(id=jid,group_id=gid,actor_id=actor,author='Author',client_id=jid,
                 content_key='unused',value=json.dumps({'revision':revision}))
            for jid,gid,actor,revision in [('job','-1001','a'*32,2),('other','-1002','b'*32,9)]])
        c.execute(site_parts.insert(),[
            dict(group_id=gid,message_id=mid,send_id=jid,ordinal=0,bot_id=bot)
            for gid,mid,jid,bot in [('-1001',1,'job',77),('-1001',2,'job',88),('-1002',1,'other',77)]])
    monkeypatch.setattr(archive.objects,'read_json',lambda *args:pytest.fail('metadata reads objects'))
    endpoint='/v1/groups/-1001/message-states'
    with selects(archive.engine) as queries:
        response=client.post(endpoint,json={'ids':list(range(1,count+1))})
    assert response.status_code==200
    states={row['id']:row for row in response.json['items']}
    assert len(states)==count
    assert states[1]=={'id':1,'edited':0,'fingerprint':'fp-1:site:'+'a'*32+':2'}
    assert states[2]['fingerprint']=='fp-2'  # A different bot cannot claim this message.
    assert states[count]['fingerprint']==f'fp-{count}'
    assert len(queries)<=4  # Group/expiry checks plus the batch, independent of ID count.
    with archive.engine.begin() as c:
        c.execute(site_sends.update().where(site_sends.c.id=='job').values(value=json.dumps({'revision':3})))
        c.execute(messages.update().where(messages.c.group_id=='-1001',messages.c.id==count).values(deleted=True))
    changed=client.post(endpoint,json={'ids':[1,count]}).json['items']
    assert changed==[{'id':1,'edited':0,'fingerprint':'fp-1:site:'+'a'*32+':3'}]
    assert client.post(endpoint,json={'ids':[]}).json=={'items':[]}
    assert client.post(endpoint,json={'ids':[1]*1001}).status_code==422
    archive.registry.action('-1001','disable')
    assert client.post(endpoint,json={'ids':[1]}).status_code==404


def test_message_state_batch_expires_messages_before_returning(client,archive):
    archive.ingest('-1001',photo(1))
    with archive.engine.begin() as c:
        c.execute(expirations.insert().values(group_id='-1001',message_id=1,expires=int(time.time())-1))
    assert client.post('/v1/groups/-1001/message-states',json={'ids':[1]}).json=={'items':[]}


def test_group_poll_queries_do_not_grow_with_group_count(client,archive,app):
    def costs():
        with selects(archive.engine) as public:
            response=client.get('/v1/groups')
        assert response.status_code==200
        with selects(archive.engine) as admin:
            response=client.get('/v1/admin/groups')
        assert response.status_code==200
        return len(public),len(admin)
    original=costs()
    with archive.registry.edit() as (c,state):
        for n in range(3,43):
            gid=f'-100{n}'
            c.execute(groups.insert().values(id=gid,title=f'Group {n}',kind='supergroup'))
            state['groups'][gid]={'telegram_id':gid,'enabled':True,'delete_at':None,'phase':None}
    assert costs()==original
    assert max(original)<=7
    assert len(client.get('/v1/groups').json['items'])==42


def test_group_batches_preserve_counts_profiles_and_fresh_status(client,archive,app,config):
    for mid in range(1,4):archive.ingest('-1001',photo(mid))
    with archive.engine.begin() as c:
        c.execute(media.update().where(media.c.message_id==1).values(state='ready',kind='video',size=20))
        c.execute(media.update().where(media.c.message_id==2).values(state='paused',size=30))
        c.execute(messages.insert().values(group_id='-1001',id=999,created=999,sender='gone',sender_id='42',deleted=True))
        c.execute(bot_access.insert().values(group_id='-1001',value=json.dumps({
            'token':hashlib.sha256(config.bot_token.encode()).hexdigest(),'checked':int(time.time()),
            'can_send':True,'bot_id':77,'can_send_voice_notes':True})))
    archive.save_group_profile('-1001',avatar_key='avatars/'+'a'*32,avatar_identity='avatar-v1')
    archive.heartbeat('online')
    bot=app.extensions['site_messaging']
    groups_before=archive.group_list()
    controls=archive.registry.read()['groups']
    public={g['id']:g for g in client.get('/v1/groups').json['items']}
    for group in groups_before:
        gid=group['id']
        assert public[gid]==group|{'connection':source_status(archive,gid,group,controls)}|bot.capability(gid)
    assert public['-1001']['media_pending']==1
    assert public['-1001']['latest_message_id']==3
    assert public['-1001']['avatar_version']=='avatar-v1'
    assert not public['-1002']['has_avatar'] and public['-1002']['latest_message_id']==0
    admin={g['id']:g for g in client.get('/v1/admin/groups').json['items']}
    # PostgreSQL SUM(bigint) is numeric and Flask serializes it as a string,
    # exactly as the previous catalog did; SQLite returns an integer.
    size_type=str if archive.engine.dialect.name=='postgresql' else int
    assert (admin['-1001']['photos'],admin['-1001']['videos'],admin['-1001']['media_bytes'])==(2,1,size_type(60))
    assert (admin['-1002']['photos'],admin['-1002']['videos'],admin['-1002']['media_bytes'])==(0,0,size_type(0))
    assert not {'photos','videos','media_bytes','avatar_key','avatar_identity'} & public['-1001'].keys()
    archive.heartbeat('login_required')
    with archive.engine.begin() as c:
        c.execute(bot_access.update().values(value=json.dumps({'checked':0})))
    updated={g['id']:g for g in client.get('/v1/groups').json['items']}
    assert updated['-1001']['connection']['state']=='login_required'
    assert updated['-1001']['bot_reason']=='checking'
    archive.registry.action('-1001','delete')
    assert '-1001' not in {g['id'] for g in client.get('/v1/groups').json['items']}
    deleting=next(g for g in client.get('/v1/admin/groups').json['items'] if g['id']=='-1001')
    assert deleting['connection']['state']=='deleting'
