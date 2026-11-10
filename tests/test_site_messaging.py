import asyncio
import io
import json
import time
import uuid
from unittest.mock import AsyncMock
import httpx
import pytest
from PIL import Image
from sqlalchemy import select
from conftest import message
from family_photo.db import bot_access,site_sends,site_uploads,site_parts,groups,messages
from family_photo.site_messaging import MessagingError,EDIT_SECONDS
from family_photo.group_management import PERIOD

ACTOR='a'*32;OTHER='b'*32;GID='-1001'


class Bot:
    def __init__(self):self.calls=[];self.mid=100;self.status='member';self.failure=None;self.permissions={'can_send_messages':True,'can_send_photos':True,'can_send_documents':True,'can_send_videos':True};self.after_send=None
    async def post(self,url,**kwargs):
        method=url.rsplit('/',1)[-1];self.calls.append((method,kwargs))
        if method=='getMe':result={'id':77,'is_bot':True}
        elif method=='getChatMember':result={'status':self.status,'is_member':self.status=='restricted',**self.permissions}
        elif method=='getChat':result={'permissions':self.permissions}
        else:
            if self.failure:raise self.failure
            if kwargs.get('files'):
                file=next(iter(kwargs['files'].values()));self.file=file[1].read()
            self.mid+=1;result={'message_id':self.mid,'date':int(time.time()),'from':{'id':77},'chat':{'id':-1001}}
            if self.after_send:self.after_send()
        return httpx.Response(200,json={'ok':True,'result':result})


@pytest.fixture
def messaging(app,monkeypatch):
    value=app.extensions['site_messaging'];bot=Bot()
    # Delivery/reaction tests isolate profile setup; its API has separate tests.
    monkeypatch.setattr(value.profile,'tick',AsyncMock())
    value.sync_permissions({'revision':1,'actors':{ACTOR:[GID],OTHER:[GID]}})
    asyncio.run(value.check_access(GID,bot))
    return value,bot


def send(value,text='Привет',files=None,request=None):
    return value.create(GID,ACTOR,'Анна',request or uuid.uuid4().hex,text,files or [])


def upload(value,body=b'original bytes',mode='file',actor=ACTOR,gid=GID):
    return value.upload(gid,actor,uuid.uuid4().hex,io.BytesIO(body),'Семейный файл.bin',mode)


def echo(archive,job,**extra):
    for mid in job['message_ids']:archive.ingest(GID,message(mid)|{'created':job['created'],'sender_id':'77','sender':'Bot','text':'Анна\nПривет'}|extra)


def test_verified_membership_and_fresh_portal_grants_are_both_required(messaging,archive,client):
    value,bot=messaging
    assert value.capability(GID)['can_send']
    assert client.get('/v1/groups').json['items'][0]['can_send']
    value.sync_permissions({'revision':2,'actors':{}})
    with pytest.raises(MessagingError,match='send_not_allowed'):send(value)
    # An older in-flight ACL refresh cannot undo revocation.
    value.sync_permissions({'revision':1,'actors':{ACTOR:[GID]}})
    with pytest.raises(MessagingError):send(value)
    value.sync_permissions({'revision':3,'actors':{ACTOR:[GID]}})
    bot.status='left'
    with archive.transaction() as c:c.execute(bot_access.delete())
    asyncio.run(value.check_access(GID,bot))
    assert not value.capability(GID)['can_send']
    with pytest.raises(MessagingError,match='read_only'):send(value)


def test_basic_groups_and_copies_stay_read_only(messaging,archive):
    value,_=messaging
    with archive.transaction() as c:c.execute(groups.update().where(groups.c.id==GID).values(kind='group'))
    assert value.capability(GID)['bot_reason']=='unsupported_group'
    with pytest.raises(MessagingError):send(value)


def test_delivered_message_keeps_portal_author_and_deduplicates_retry(messaging,archive):
    value,bot=messaging;request=uuid.uuid4().hex
    job=send(value,request=request)
    assert send(value,request=request)['id']==job['id']
    with pytest.raises(MessagingError,match='client_conflict'):send(value,text='Другой текст',request=request)
    asyncio.run(value.tick(bot));job=value.get(job['id']);echo(archive,job)
    item=archive.history(GID)['items'][0]
    assert item['site_actor_id']==ACTOR and item['sender_id']=='site:'+ACTOR
    assert item['text']=='Привет' and item['sender']=='Анна'
    assert not value.list(GID,ACTOR)
    assert [kwargs['json']['text'] for method,kwargs in bot.calls if method=='sendMessage']==['Анна:\nПривет']
    # An ordinary message imitating the author prefix never gains ownership.
    archive.ingest(GID,message(999)|{'sender_id':'999','text':'Анна\nПривет'})
    assert 'site_actor_id' not in archive.history(GID)['items'][-1]


def test_edit_is_owner_only_revision_checked_and_confirmed_before_publishing(messaging,archive):
    value,bot=messaging;job=send(value);asyncio.run(value.tick(bot));job=value.get(job['id']);echo(archive,job);mid=job['message_ids'][0]
    with pytest.raises(MessagingError,match='not_author'):value.edit(GID,OTHER,mid,'Украдено',0,'x')
    with pytest.raises(MessagingError,match='edit_conflict'):value.edit(GID,ACTOR,mid,'Новое',2,'x')
    pending=value.edit(GID,ACTOR,mid,'Новый текст',0,'edit-1')
    assert pending['state']=='edit_queued' and archive.history(GID)['items'][0]['text']=='Привет'
    asyncio.run(value.tick(bot))
    item=archive.history(GID)['items'][0]
    assert item['text']=='Новый текст' and item['site_revision']==1 and item['edited']
    assert value.edit(GID,ACTOR,mid,'Новый текст',0,'edit-1')['revision']==1
    with pytest.raises(MessagingError,match='client_conflict'):value.edit(GID,ACTOR,mid,'Другое',0,'edit-1')
    with archive.transaction() as c:
        current=value.get(job['id'],c);current['created']=current['sent_at']=int(time.time())-EDIT_SECONDS;value.save(c,current)
    with pytest.raises(MessagingError,match='edit_expired'):value.edit(GID,ACTOR,mid,'Поздно',1,'edit-2')


def test_ambiguous_send_never_retries_automatically(messaging):
    value,bot=messaging;job=send(value);bot.failure=httpx.ReadTimeout('private response omitted')
    asyncio.run(value.tick(bot));asyncio.run(value.tick(bot))
    assert value.get(job['id'])['state']=='uncertain'
    assert len([call for call in bot.calls if call[0]=='sendMessage'])==1


def test_restart_does_not_repeat_send_but_can_retry_an_idempotent_edit(messaging):
    value,_=messaging;job=send(value)
    with value.archive.transaction() as c:
        current=value.get(job['id'],c);current['state']='sending';value.save(c,current)
    value.recover();assert value.get(job['id'])['state']=='uncertain'


def test_revocation_before_delivery_prevents_network_send(messaging):
    value,bot=messaging;job=send(value)
    value.sync_permissions({'revision':2,'actors':{}});asyncio.run(value.tick(bot))
    assert value.get(job['id'])['state']=='failed'
    assert not any(method.startswith('send') for method,_ in bot.calls)


def test_expired_permission_snapshot_fails_closed(messaging,monkeypatch):
    value,_=messaging;now=time.time();monkeypatch.setattr('time.time',lambda:now+121)
    with pytest.raises(MessagingError,match='send_not_allowed'):send(value)


def test_upload_download_scope_integrity_and_generic_file_in_history(messaging,archive,client):
    value,bot=messaging;file=upload(value);fid=file['id'];url=f'/v1/groups/{GID}/site-uploads/{fid}?actor_id='
    assert file['kind']=='file' and file['site_upload']
    assert client.get(url+OTHER).status_code==404
    assert client.get(url+ACTOR).data==b'original bytes'
    assert client.get(url+ACTOR,headers={'Range':'bytes=1-4'}).data==b'rigi'
    job=send(value,files=[fid]);asyncio.run(value.tick(bot));job=value.get(job['id']);echo(archive,job)
    assert bot.file==b'original bytes'
    assert archive.history(GID)['items'][0]['media'][0]['kind']=='file'
    assert client.get(url+OTHER).status_code==200
    assert 'attachment' in client.get(url+OTHER).headers['Content-Disposition']
    assert client.get('/v1/groups/-1002/site-uploads/'+fid+'?actor_id='+ACTOR).status_code==404


def test_copies_preserve_site_files_and_attribution_after_live_deletion(messaging,archive):
    value,bot=messaging;file=upload(value);job=send(value,files=[file['id']]);asyncio.run(value.tick(bot));job=value.get(job['id']);echo(archive,job)
    copied=archive.registry.copy(GID,uuid.uuid4().hex)
    with archive.engine.connect() as c:key=c.execute(select(site_uploads.c.object_key).where(site_uploads.c.id==file['id'])).scalar_one()
    archive.delete_messages(GID,job['message_ids']);archive.collect_garbage()
    assert b''.join(archive.objects.read(key))==b'original bytes'
    snapshot=archive.history(copied)['items'][0]
    assert snapshot['site_actor_id']==ACTOR and snapshot['media'][0]['kind']=='file'
    archive.registry.action(copied,'delete',stamp=0);archive.registry.maintenance(stamp=PERIOD);archive.collect_garbage();archive.registry.maintenance(stamp=PERIOD)
    with pytest.raises(Exception):b''.join(archive.objects.read(key))


def test_cancel_releases_staged_file_and_expired_drafts_are_cleaned(messaging,archive,monkeypatch):
    value,_=messaging;file=upload(value)
    with archive.engine.connect() as c:key=c.execute(select(site_uploads.c.object_key).where(site_uploads.c.id==file['id'])).scalar_one()
    job=send(value,files=[file['id']]);value.cancel(GID,ACTOR,job['id']);archive.collect_garbage()
    with pytest.raises(Exception):b''.join(archive.objects.read(key))
    file=upload(value)
    now=time.time();monkeypatch.setattr('time.time',lambda:now+86401);value.maintain();archive.collect_garbage()
    with archive.engine.connect() as c:assert not c.execute(select(site_uploads)).first()


def test_real_raw_upload_limit_photo_validation_and_ownership(messaging,client):
    value,_=messaging;fid=uuid.uuid4().hex
    url=f'/v1/groups/{GID}/site-uploads/{fid}?actor_id={ACTOR}&name=photo.jpg&mode=preview'
    assert client.put(url,data=b'<html>not photo</html>').status_code==422
    image=io.BytesIO();Image.new('RGB',(20,20),'blue').save(image,'JPEG')
    assert client.put(url,data=image.getvalue()).status_code==201
    response=client.get(f'/v1/groups/{GID}/site-uploads/{fid}?actor_id={ACTOR}&thumbnail=1')
    assert response.status_code==200 and response.mimetype=='image/jpeg'
    assert client.delete(f'/v1/groups/{GID}/site-uploads/{fid}?actor_id={OTHER}').status_code==200
    assert client.get(f'/v1/groups/{GID}/site-uploads/{fid}?actor_id={ACTOR}').status_code==200


def test_group_deletion_collects_queue_uploads_permissions_and_objects(messaging,archive):
    value,_=messaging;file=upload(value);send(value,files=[file['id']])
    archive.registry.action(GID,'delete',stamp=0);archive.registry.maintenance(stamp=PERIOD);archive.collect_garbage();archive.registry.maintenance(stamp=PERIOD)
    with archive.engine.connect() as c:
        for table in (site_sends,site_parts,site_uploads,bot_access):assert not c.execute(select(table)).first()


def test_omitted_member_permissions_fail_closed_but_admin_can_send(messaging,archive):
    value,bot=messaging;bot.permissions={}
    with archive.transaction() as c:c.execute(bot_access.delete())
    asyncio.run(value.check_access(GID,bot));assert not value.capability(GID)['can_send']
    bot.status='administrator'
    with archive.transaction() as c:c.execute(bot_access.delete())
    asyncio.run(value.check_access(GID,bot));assert value.capability(GID)['can_send_photos']


def test_retry_after_stops_all_bot_requests(messaging):
    value,bot=messaging;job=send(value)
    original=bot.post
    async def limited(url,**kwargs):
        if url.endswith('/sendMessage'):return httpx.Response(429,json={'ok':False,'parameters':{'retry_after':60}})
        return await original(url,**kwargs)
    bot.post=limited
    asyncio.run(value.tick(bot));assert value.get(job['id'])['state']=='queued' and value.paused()
    calls=len(bot.calls);asyncio.run(value.tick(bot));assert len(bot.calls)==calls


def test_partial_delivery_revocation_preserves_confirmed_file_and_collects_rest(messaging,archive):
    value,bot=messaging;one=upload(value);two=upload(value,b'second')
    job=send(value,files=[one['id'],two['id']])
    bot.after_send=lambda:value.sync_permissions({'revision':2,'actors':{}})
    asyncio.run(value.tick(bot));job=value.get(job['id'])
    assert job['next_part']==1 and len(job['message_ids'])==1 and job['state']=='failed'
    echo(archive,job);value.cancel(GID,ACTOR,job['id']);archive.collect_garbage()
    with archive.engine.connect() as c:assert c.execute(select(site_uploads.c.id)).scalars().all()==[one['id']]
    assert archive.history(GID)['items'][0]['media'][0]['id']==one['id']


def test_photo_reuses_original_after_ingest_and_late_download_cannot_replace_it(messaging,archive):
    from conftest import photo
    from family_photo.db import media
    value,bot=messaging;data=io.BytesIO();Image.new('RGB',(20,20),'blue').save(data,'JPEG')
    file=upload(value,data.getvalue(),mode='preview');job=send(value,files=[file['id']]);asyncio.run(value.tick(bot));job=value.get(job['id']);mid=job['message_ids'][0]
    archive.ingest(GID,photo(mid,sender_id='77'))
    with archive.engine.connect() as c:
        item=c.execute(select(media).where(media.c.message_id==mid)).mappings().one()
        original=c.execute(select(site_uploads).where(site_uploads.c.id==file['id'])).mappings().one()
    assert item['state']=='ready' and item['object_key']==original['object_key']
    with archive.upload('originals') as late:
        archive.objects.put_json(late,{'late':True});archive.attach_download(dict(item),late,None,5)
    with archive.engine.connect() as c:assert c.execute(select(media.c.object_key).where(media.c.id==item['id'])).scalar_one()==original['object_key']


def test_self_unread_exclusion_and_metadata_version_after_confirmed_edit(messaging,archive,client):
    value,bot=messaging;job=send(value);asyncio.run(value.tick(bot));job=value.get(job['id']);echo(archive,job);mid=job['message_ids'][0]
    archive.ingest(GID,message(999))
    assert client.get(f'/v1/groups/{GID}/unread?exclude_actor={ACTOR}').json['unread']==1
    assert client.get(f'/v1/groups/{GID}/unread?exclude_actor={OTHER}').json['unread']==2
    state=lambda:client.post(f'/v1/groups/{GID}/message-states',json={'ids':[mid]}).json['items'][0]['fingerprint']
    old=state();value.edit(GID,ACTOR,mid,'Новое',0,'edit');asyncio.run(value.tick(bot))
    assert state()!=old and state()==archive.history(GID)['items'][0]['version']


def test_recovered_edit_accepts_already_modified_response(messaging,archive):
    value,bot=messaging;job=send(value);asyncio.run(value.tick(bot));job=value.get(job['id']);echo(archive,job)
    value.edit(GID,ACTOR,job['message_ids'][0],'Новое',0,'edit')
    with archive.transaction() as c:
        current=value.get(job['id'],c);current['state']='editing';value.save(c,current)
    async def not_modified(url,**kwargs):return httpx.Response(400,json={'ok':False,'description':'Bad Request: message is not modified'})
    bot.post=not_modified
    asyncio.run(value.tick(bot));assert value.get(job['id'])['state']=='delivered'
    assert archive.history(GID)['items'][0]['text']=='Новое'


def test_ambiguous_edit_retries_safely_until_confirmation(messaging,archive):
    value,bot=messaging;job=send(value);asyncio.run(value.tick(bot));job=value.get(job['id']);echo(archive,job)
    value.edit(GID,ACTOR,job['message_ids'][0],'Новое',0,'edit-timeout');bot.failure=httpx.ReadTimeout('response lost')
    asyncio.run(value.tick(bot));assert value.get(job['id'])['state']=='edit_queued'
    assert archive.history(GID)['items'][0]['text']=='Привет'
    bot.failure=None
    with archive.transaction() as c:
        current=value.get(job['id'],c);current['retry_at']=0;value.save(c,current)
    asyncio.run(value.tick(bot));assert value.get(job['id'])['state']=='delivered'
    assert archive.history(GID)['items'][0]['text']=='Новое'


def test_signature_uses_italic_entities_without_interpreting_user_markup(messaging,archive):
    from family_photo.site_messaging import render_message
    value,bot=messaging;text='<b>текст</b> 😀 _буквально_';author='Анна 🐈'
    rendered,entities=render_message(text,author)
    assert rendered==author+':\n'+text
    entity=entities[0];encoded=rendered.encode('utf-16-le')
    assert encoded[entity['offset']*2:(entity['offset']+entity['length'])*2].decode('utf-16-le')==author
    job=value.create(GID,ACTOR,author,'formatted',text,[]);asyncio.run(value.tick(bot))
    body=next(kw['json'] for method,kw in bot.calls if method=='sendMessage')
    assert body['text']==rendered and body['entities']==entities and 'parse_mode' not in body
    file=upload(value);job=send(value,text='',files=[file['id']]);asyncio.run(value.tick(bot))
    body=next(kw['data'] for method,kw in bot.calls if method=='sendDocument')
    assert body['caption']=='Анна' and json.loads(body['caption_entities'])==render_message('','Анна')[1]


def test_gif_upload_uses_animation_permission_and_sendanimation(messaging,archive):
    value,bot=messaging;bot.permissions['can_send_other_messages']=True
    # Refresh the persisted membership without waiting for its ordinary TTL.
    with archive.transaction() as c:c.execute(bot_access.delete())
    asyncio.run(value.check_access(GID,bot))
    buffer=io.BytesIO();Image.new('RGB',(20,20),'red').save(buffer,format='GIF',save_all=True,append_images=[Image.new('RGB',(20,20),'blue')],duration=100,loop=0)
    file=upload(value,buffer.getvalue(),'preview')
    assert file['kind']=='animation' and file['mime']=='image/gif'
    job=send(value,files=[file['id']]);asyncio.run(value.tick(bot))
    sent=[kwargs for method,kwargs in bot.calls if method=='sendAnimation']
    assert len(sent)==1 and sent[0]['files']['animation'][2]=='image/gif'
    assert bot.file==buffer.getvalue()
    assert not any(method in {'sendPhoto','sendVideo'} for method,_ in bot.calls)
    bot.permissions['can_send_other_messages']=False
    with archive.transaction() as c:c.execute(bot_access.delete())
    asyncio.run(value.check_access(GID,bot))
    with pytest.raises(MessagingError,match='attachment_not_allowed'):upload(value,buffer.getvalue(),'preview')


@pytest.mark.parametrize('format,codec,extension,mime',[('ipod','aac','m4a','audio/mp4'),('webm','libopus','webm','audio/webm')])
def test_recorded_voice_reuses_queue_originals_permissions_and_sendvoice(messaging,archive,tmp_path,format,codec,extension,mime):
    import subprocess
    value,bot=messaging;bot.permissions['can_send_voice_notes']=True
    with archive.transaction() as c:c.execute(bot_access.delete())
    asyncio.run(value.check_access(GID,bot))
    path=tmp_path/('voice.'+extension)
    subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','sine=frequency=440:duration=0.4','-c:a',codec,'-f',format,str(path)],capture_output=True,check=True)
    raw=path.read_bytes();file=value.upload(GID,ACTOR,uuid.uuid4().hex,io.BytesIO(raw),'Голосовое.'+extension,'preview')
    assert file['kind']=='voice' and file['mime']==mime
    with archive.engine.connect() as c:row=c.execute(select(site_uploads).where(site_uploads.c.id==file['id'])).mappings().one()
    assert b''.join(archive.objects.read(row['object_key']))==raw
    job=send(value,files=[file['id']]);asyncio.run(value.tick(bot));assert value.get(job['id'])['state']=='delivered'
    sent=[kwargs for method,kwargs in bot.calls if method=='sendVoice'];assert len(sent)==1
    assert sent[0]['files']['voice'][2]==('audio/ogg' if mime=='audio/webm' else mime)
    assert bot.file.startswith(b'OggS') if mime=='audio/webm' else bot.file==raw
    bot.permissions['can_send_voice_notes']=False
    with archive.transaction() as c:c.execute(bot_access.delete())
    asyncio.run(value.check_access(GID,bot))
    with pytest.raises(MessagingError,match='attachment_not_allowed'):value.upload(GID,ACTOR,uuid.uuid4().hex,io.BytesIO(raw),'Голосовое.'+extension,'preview')
