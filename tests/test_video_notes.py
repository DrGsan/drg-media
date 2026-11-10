import asyncio
import io
import json
import subprocess
import time
import uuid
from types import SimpleNamespace
from datetime import datetime,timezone
from unittest.mock import AsyncMock
import httpx
import pytest
from sqlalchemy import select
from telethon.tl import types
from test_site_messaging import messaging,send,echo,ACTOR,GID
from family_photo.db import bot_access,site_uploads,site_parts
from family_photo.site_messaging import MessagingError
from family_photo.telegram import normalize


@pytest.fixture
def circle(messaging,archive,tmp_path):
    value,bot=messaging;bot.permissions['can_send_video_notes']=True
    with archive.transaction() as c:c.execute(bot_access.delete())
    asyncio.run(value.check_access(GID,bot))
    path=tmp_path/'circle.mp4'
    subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','color=c=blue:s=96x96:d=0.6','-f','lavfi','-i','sine=frequency=440:duration=0.6','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-shortest',str(path)],capture_output=True,check=True)
    raw=path.read_bytes();header=json.dumps({'kind':'video_note','duration_ms':600})
    file=value.upload(GID,ACTOR,uuid.uuid4().hex,io.BytesIO(raw),'circle.mp4','preview',header)
    return value,bot,file,raw,header


def test_circle_uses_durable_header_and_send_video_note_without_caption(circle,archive):
    value,bot,file,raw,header=circle
    assert file['kind']=='video_note' and .5<file['duration']<1
    assert file['has_thumbnail']
    job=send(value,text='Кружок',files=[file['id']]);asyncio.run(value.tick(bot));job=value.get(job['id'])
    assert job['state']=='delivered' and len(job['message_ids'])==2
    assert [method for method,_ in bot.calls if method.startswith('send')]==['sendMessage','sendVideoNote']
    body=next(kwargs for method,kwargs in bot.calls if method=='sendVideoNote')
    assert 'caption' not in body['data'] and 'caption_entities' not in body['data']
    assert body['data']['length']=='96'
    assert body['files']['video_note'][2]=='video/mp4' and bot.file==raw
    echo(archive,job)
    rows=archive.history(GID)['items'];assert rows[0]['text']=='Кружок' and not rows[0]['media']
    assert rows[1]['media'][0]['kind']=='video_note' and rows[1]['text']==''
    assert rows[1]['media'][0]['duration']==file['duration']
    value.edit(GID,ACTOR,job['message_ids'][0],'Правка',0,'edit-circle');asyncio.run(value.tick(bot))
    assert bot.calls[-1][0]=='editMessageText'
    assert archive.history(GID)['items'][0]['text']=='Правка'


def test_partial_circle_retry_never_repeats_the_header(circle,archive):
    value,bot,file,_,_=circle;job=send(value,files=[file['id']]);original=bot.post;limited=True
    async def post(url,**kwargs):
        nonlocal limited
        if url.endswith('/sendVideoNote') and limited:
            limited=False;return httpx.Response(429,json={'ok':False,'parameters':{'retry_after':1}})
        return await original(url,**kwargs)
    bot.post=post
    asyncio.run(value.tick(bot));job=value.get(job['id']);assert job['next_part']==1 and job['state']=='queued'
    # Expire only the isolated queue/bot pause; no live Telegram request is involved.
    with archive.transaction() as c:
        job['retry_at']=0;value.save(c,job)
    from family_photo.db import group_management
    with archive.transaction() as c:c.execute(group_management.delete().where(group_management.c.id=='site-bot-pause'))
    asyncio.run(value.tick(bot));assert value.get(job['id'])['state']=='delivered'
    assert len([m for m,_ in bot.calls if m=='sendMessage'])==1


def test_cancel_after_header_discards_only_unsent_recording(circle,archive):
    value,bot,file,_,_=circle;job=send(value,files=[file['id']]);original=bot.post
    async def post(url,**kwargs):
        if url.endswith('/sendVideoNote'):return httpx.Response(400,json={'ok':False,'description':'rejected'})
        return await original(url,**kwargs)
    bot.post=post;asyncio.run(value.tick(bot));job=value.get(job['id']);assert job['state']=='failed' and job['next_part']==1
    value.cancel(GID,ACTOR,job['id']);job=value.get(job['id']);assert job['attachments']==[] and job['state']=='delivered'
    with archive.engine.connect() as c:assert not c.execute(select(site_uploads).where(site_uploads.c.id==file['id'])).first()


@pytest.mark.parametrize('changes',[{'duration_ms':60001},{'waveform':[1]},{'duration_ms':True},{'kind':'unknown'}])
def test_invalid_recording_does_not_create_upload(circle,archive,changes):
    value,_,file,raw,header=circle;count=0
    with archive.engine.connect() as c:count=len(c.execute(select(site_uploads)).all())
    with pytest.raises(MessagingError,match='invalid_recording'):
        value.upload(GID,ACTOR,uuid.uuid4().hex,io.BytesIO(raw),'circle.mp4','preview',json.dumps(json.loads(header)|changes))
    with archive.engine.connect() as c:assert len(c.execute(select(site_uploads)).all())==count


def test_telegram_round_document_is_not_an_ordinary_video():
    document=SimpleNamespace(id=987,attributes=[types.DocumentAttributeVideo(duration=2,w=240,h=240,round_message=True)])
    source=SimpleNamespace(id=34,date=datetime.now(timezone.utc),sender_id=4,get_sender=AsyncMock(return_value=None),document=document,
        file=SimpleNamespace(mime_type='video/mp4',name='circle.mp4',size=123,ext='.mp4',duration=2))
    item=asyncio.run(normalize(source))
    assert item['content_type']=='video_note' and item['media']['kind']=='video_note' and item['duration']==2


def test_copied_circle_keeps_correct_shifted_part_and_private_object(circle,archive):
    value,bot,file,raw,_=circle;job=send(value,files=[file['id']]);asyncio.run(value.tick(bot));job=value.get(job['id']);echo(archive,job)
    copied=archive.registry.copy(GID,uuid.uuid4().hex)
    with archive.engine.connect() as c:key=c.execute(select(site_uploads.c.object_key).where(site_uploads.c.id==file['id'])).scalar_one()
    archive.delete_messages(GID,job['message_ids']);archive.collect_garbage()
    rows=archive.history(copied)['items']
    assert rows[0]['site_actor_id']==ACTOR and not rows[0]['media']
    assert rows[1]['media'][0]['kind']=='video_note' and b''.join(archive.objects.read(key))==raw


def test_incoming_telegram_waveform_is_decoded_and_stored_without_extra_queries(archive):
    from test_voice import voice
    source=voice();samples=[0,3,31,6]*25
    packed=sum(value<<(5*i) for i,value in enumerate(samples)).to_bytes((len(samples)*5+7)//8,'little')
    source.document.attributes=[types.DocumentAttributeAudio(duration=2,voice=True,waveform=packed)]
    item=asyncio.run(normalize(source));assert len(item['waveform'])==64 and max(item['waveform'])==31 and min(item['waveform'])>=0
    archive.ingest(GID,item);file=archive.history(GID)['items'][0]['media'][0]
    assert file['waveform']==item['waveform'] and file['duration']==2
