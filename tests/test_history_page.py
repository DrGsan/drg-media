"""History batching preserves presentation and bounds repeated metadata queries."""
import json
import asyncio
from sqlalchemy import event, select
import pytest

from conftest import message, photo
from family_photo.db import messages, people, emoji_assets, group_management
from family_photo.site_reactions import key as reaction_key
from test_site_messaging import messaging, send, upload, echo


def individual(archive,gid,ids):
    with archive.engine.connect() as c:
        rows=c.execute(select(messages).where(messages.c.group_id==gid,messages.c.id.in_(ids)).order_by(messages.c.id)).mappings()
        return [archive.serialize(c,row) for row in rows]


@pytest.mark.parametrize('rich', [False,True])
def test_history_metadata_queries_are_bounded_for_a_hundred_messages(archive,client,rich):
    for number in range(1,102):
        content=photo(number,sender_id=str(number%20))
        if rich:content['reactions']=[{'emoji':'👍','count':4,'people':[{'id':str(i)} for i in range(4)]}]
        archive.ingest('-1001',content)
    with archive.engine.begin() as c:
        c.execute(people.insert(),[dict(group_id='-1001',sender_id=str(i),name='Synthetic',checked_at=0) for i in range(20)])
    calls=[]
    def counted(c,cur,sql,params,context,many):calls.append(sql)
    event.listen(archive.engine,'before_cursor_execute',counted)
    try:response=client.get('/v1/groups/-1001/messages?limit=100')
    finally:event.remove(archive.engine,'before_cursor_execute',counted)
    assert response.status_code==200
    assert len(response.json['items'])==100 and response.json['more']
    assert len(calls)<=20
    assert response.json['items']==individual(archive,'-1001',[row['id'] for row in response.json['items']])


def test_group_scoped_people_emoji_reactions_and_refresh_match_individual_messages(archive,client):
    for gid,name in [('-1001','First'),('-1002','Other')]:
        archive.ingest(gid,message(1,custom_emojis=[{'offset':0,'length':1,'document_id':'7'}],
            reactions=[{'emoji':'👍','count':2,'people':[{'id':'42'},{'id':'404'}]},
                       {'document_id':'8','count':1,'people':[{'id':'42'}]}]))
        with archive.engine.begin() as c:
            c.execute(people.insert().values(group_id=gid,sender_id='42',name=name,avatar_identity=name,avatar_key='avatars/'+'a'*32,checked_at=0))
            c.execute(emoji_assets.insert().values(group_id=gid,document_id='7',mime='image/png',state='ready'))
            c.execute(group_management.insert().values(id=reaction_key(gid,1),value=json.dumps({'group_id':gid,'message_id':1,
                'revision':1,'state':'delivered','applied':'👍','emoji':'👍','bot_id':'42'})))
    for gid in ['-1001','-1002']:
        actual=client.get(f'/v1/groups/{gid}/messages').json['items']
        assert actual==individual(archive,gid,[1])
        assert actual[0]['sender_avatar_version']==('First' if gid=='-1001' else 'Other')
        assert actual[0]['reactions'][0]['people']==[{'id':'404'}]
        assert actual[0]['custom_emojis'][0]['state']=='ready'
        assert actual[0]['reactions'][1]['custom_emoji']=={'state':'pending'}
    archive.save_person('-1001','42',name='Changed',avatar_key=None,avatar_identity='',checked_at=1)
    assert client.get('/v1/groups/-1001/messages').json['items']==individual(archive,'-1001',[1])
    assert not client.get('/v1/groups/-1001/messages').json['items'][0]['sender_has_avatar']


def test_history_keeps_album_boundary_and_empty_pages(archive,client):
    for number in range(1,115):archive.ingest('-1001',photo(number,album_id='album' if number in range(8,18) else None))
    result=client.get('/v1/groups/-1001/messages?before=110&limit=100').json
    assert [item['id'] for item in result['items']]==list(range(8,110))
    assert result['more'] and result['items']==individual(archive,'-1001',list(range(8,110)))
    assert client.get('/v1/groups/-1001/messages?before=1').json=={'items':[],'more':False}


def test_object_failures_remain_private_and_do_not_serve_a_previous_page(archive,client,monkeypatch):
    archive.ingest('-1001',message(1))
    assert client.get('/v1/groups/-1001/messages').status_code==200
    def failed(*args):raise OSError('synthetic storage failure')
    monkeypatch.setattr(archive.objects,'read_json',failed)
    response=client.get('/v1/groups/-1001/messages')
    assert response.status_code==503 and response.json=={'detail':'Archive storage is unavailable'}


def test_confirmed_site_upload_and_edits_match_individual_rendering(messaging,archive,client):
    value,bot=messaging
    attachment=upload(value)
    job=send(value,text='Caption',files=[attachment['id']])
    asyncio.run(value.tick(bot));job=value.get(job['id']);echo(archive,job)
    ids=job['message_ids']
    actual=client.get('/v1/groups/-1001/messages').json['items']
    assert actual==individual(archive,'-1001',ids)
    assert actual[0]['media'][0]['id']==attachment['id']
    value.edit('-1001',job['actor_id'],ids[0],'Changed',job['revision'],'edit-batch')
    asyncio.run(value.tick(bot))
    actual=client.get('/v1/groups/-1001/messages').json['items']
    assert actual==individual(archive,'-1001',ids)
    assert actual[0]['text']=='Changed' and actual[0]['site_revision']==1
