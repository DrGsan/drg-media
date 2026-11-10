import asyncio
import gzip
import json
import shutil
import subprocess
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from sqlalchemy import select
from telethon import types

from family_photo.db import media
from family_photo.telegram import normalize,Synchronizer,make_thumbnail
from family_photo.stickers import read_animation
from conftest import photo


def source(**values):
    return SimpleNamespace(id=3,date=datetime.now(timezone.utc),sender_id=4,get_sender=AsyncMock(return_value=None),**values)


def test_normalize_sticker_and_voice_preserve_semantics_without_audio_download():
    document=SimpleNamespace(id=55,attributes=[types.DocumentAttributeSticker(alt='😆',stickerset=types.InputStickerSetEmpty())])
    for mime in ('image/webp','video/webm','application/x-tgsticker'):
        row=asyncio.run(normalize(source(document=document,sticker=document,file=SimpleNamespace(mime_type=mime,name='sticker',ext='',size=123))))
        assert row['content_type']=='sticker' and row['media']['kind']=='sticker' and row['sticker_emoji']=='😆'
    row=asyncio.run(normalize(source(voice=object(),file=SimpleNamespace(duration=14))))
    assert row['content_type']=='voice' and row['duration']==14 and 'media' not in row


def test_single_custom_emoji_resolves_its_document_once_with_utf16_offsets(config,archive):
    document=types.Document(id=99,access_hash=1,file_reference=b'test',date=datetime.now(timezone.utc),
        mime_type='application/x-tgsticker',size=123,dc_id=2,
        attributes=[types.DocumentAttributeCustomEmoji(alt='🤣',stickerset=types.InputStickerSetEmpty())])
    client=AsyncMock(return_value=[document]);sync=Synchronizer(config,archive,client)
    message=source(raw_text='🤣',entities=[types.MessageEntityCustomEmoji(offset=0,length=2,document_id=99)])
    async def scenario():
        await sync.preload_emoji([message,message]);await sync.apply('-1001',message);await sync.apply('-1001',message)
        inline=source(raw_text='Привет 🤣',entities=[types.MessageEntityCustomEmoji(offset=7,length=2,document_id=99)])
        assert await sync.prepare_source(inline) is inline
    asyncio.run(scenario())
    row=archive.history('-1001')['items'][0]
    assert row['text']=='' and row['content_type']=='sticker' and row['sticker_emoji']=='🤣' and row['custom_emoji'] is True
    assert row['media'][0]['kind']=='sticker' and row['media'][0]['mime']=='application/x-tgsticker'
    assert client.await_count==1
    async def retry_expired_reference():
        from dataclasses import replace
        from telethon import errors
        sync.config=replace(config,page_delay=0)
        sync.download_media=AsyncMock(side_effect=errors.FileReferenceExpiredError(None))
        await sync.downloads()
        assert 99 not in sync.emoji_documents
        with archive.transaction() as c:c.execute(media.update().values(retry_at=0))
        def history(*args,**kwargs):
            async def rows():yield message
            return rows()
        client.iter_messages=history
        async def downloaded(source,target):target.write_bytes(gzip.compress(json.dumps(animation()).encode()));return target
        sync.download_media=downloaded
        await sync.downloads()
    asyncio.run(retry_expired_reference())
    assert client.await_count==2 and archive.history('-1001')['items'][0]['media'][0]['state']=='ready'


def test_photo_size_check_uses_the_downloaded_variant_not_the_heaviest(config,archive,tmp_path):
    from telethon import TelegramClient,functions
    from telethon.sessions import StringSession
    async def scenario():
        client=TelegramClient(StringSession(),123,'a'*32);client.session.set_dc(1,'127.0.0.1',80)
        class Sender:
            def send(self,request,ordered=False):
                assert isinstance(request,functions.upload.GetFileRequest) and request.location.thumb_size=='y'
                future=asyncio.get_running_loop().create_future();future.set_result(types.upload.File(type=types.storage.FileUnknown(),mtime=0,bytes=b'data'));return future
        client._sender=Sender()
        photo=types.Photo(id=1,access_hash=2,file_reference=b'test',date=datetime.now(timezone.utc),
            sizes=[types.PhotoSize(type='x',w=100,h=100,size=8),types.PhotoSize(type='y',w=200,h=200,size=4)],dc_id=1)
        item=SimpleNamespace(photo=photo,file=SimpleNamespace(size=8))
        path=await Synchronizer(config,archive,client).download_media(item,tmp_path/'photo')
        assert Path(path).read_bytes()==b'data';client.session.close()
    asyncio.run(scenario())


def animation():return {'v':'5.7.4','w':512,'h':512,'fr':30,'ip':0,'op':60,'layers':[],'assets':[]}


def test_tgs_decoding_is_bounded_and_forbids_expressions_and_external_assets(archive):
    key='media/'+'1'*32;path=archive.objects.config.data_dir/'animation.tgs'
    def load(data):
        path.write_bytes(gzip.compress(json.dumps(data).encode()));archive.objects.put_file(key,path);return read_animation(archive.objects,key)
    assert load(animation())['fr']==30
    for changed in ({'w':99999},{'op':99999},{'layers':[{'ty':2,'refId':'a'}]},
                    {'assets':[{'p':'https://example.org/private.png'}]},{'layers':[{'ks':{'x':'alert(1)'}}]},
                    {'layers':[{'__proto__':{}}]}):
        with pytest.raises(ValueError):load(animation()|changed)
    path.write_bytes(gzip.compress(b' '* (2*1024*1024+1)));archive.objects.put_file(key,path)
    with pytest.raises(ValueError):read_animation(archive.objects,key)


def test_sticker_voice_history_gallery_and_private_animation_api(config,archive,client):
    data={'id':3,'created':1700000000,'sender_id':'4','sender':'Анна','text':'','content_type':'sticker','sticker_emoji':'😆',
          'media':{'identity':'55','kind':'sticker','mime':'application/x-tgsticker','name':'sticker.tgs','size':100}}
    archive.ingest('-1001',data)
    with archive.engine.connect() as c:item=dict(c.execute(select(media)).mappings().one())
    path=config.data_dir/'sticker';path.write_bytes(gzip.compress(json.dumps(animation()).encode()))
    key='media/'+'2'*32;archive.objects.put_file(key,path);archive.attach_download(item,key,None,path.stat().st_size)
    archive.ingest('-1001',dict(id=4,created=1700000001,sender_id='4',sender='Анна',text='',content_type='voice',duration=14))
    rows=client.get('/v1/groups/-1001/messages').json['items']
    assert rows[0]['content_type']=='sticker' and rows[1]['duration']==14
    assert client.get('/v1/groups/-1001/media').json['items']==[]
    url='/v1/groups/-1001/media/'+item['id']+'?animation=1'
    assert client.get(url).json==animation()
    assert client.get(url,headers={'Authorization':'invalid'}).status_code==401
    assert client.get(url.replace('-1001','-999')).status_code==404


def test_existing_webm_changes_kind_without_redownloading_original(config,archive,monkeypatch):
    data={'id':3,'created':1700000000,'sender_id':'4','sender':'Анна','text':'','media':{'identity':'55','kind':'video','mime':'video/webm','name':'sticker.webm','size':4}}
    archive.ingest('-1001',data)
    with archive.engine.connect() as c:item=dict(c.execute(select(media)).mappings().one())
    path=config.data_dir/'source';path.write_bytes(b'test');key='media/'+'3'*32
    archive.objects.put_file(key,path);archive.attach_download(item,key,None,4)
    data['content_type']='sticker';data['media']['kind']='sticker';archive.ingest('-1001',data)
    class Client:
        def iter_messages(self,*args,**kwargs):
            async def rows():yield SimpleNamespace(id=3)
            return rows()
    monkeypatch.setattr('family_photo.telegram.normalize',AsyncMock(return_value=data))
    monkeypatch.setattr('family_photo.telegram.ephemeral',lambda message:False)
    monkeypatch.setattr('family_photo.telegram.make_thumbnail',lambda source,target,kind:target.write_bytes(b'webp'))
    sync=Synchronizer(config,archive,Client());sync.download_media=AsyncMock(side_effect=AssertionError('Do not download again'))
    asyncio.run(sync.downloads())
    with archive.engine.connect() as c:changed=dict(c.execute(select(media)).mappings().one())
    assert changed['id']==item['id'] and changed['object_key']==key and changed['state']=='ready' and changed['kind']=='sticker'


def test_file_flood_wait_does_not_block_history_or_outbox(config,archive,monkeypatch):
    async def scenario():
        sync=Synchronizer(config,archive,None);sync.state='online'
        waiting=asyncio.Event();scanned=asyncio.Event()
        async def download():waiting.set();await asyncio.Event().wait()
        async def scan(gid):
            await waiting.wait();scanned.set()
        sync.downloads=download;sync.scan=scan;sync.reconcile=AsyncMock();sync.sync_group_profile=AsyncMock()
        monkeypatch.setattr('family_photo.telegram.send_outbox',AsyncMock())
        worker=asyncio.create_task(sync.media_loop());history=asyncio.create_task(sync.sync_loop())
        try:await asyncio.wait_for(scanned.wait(),1)
        finally:
            worker.cancel();history.cancel();await asyncio.gather(worker,history,return_exceptions=True)
    asyncio.run(scenario())


def test_zero_progress_file_yields_to_other_media_after_server_pauses(config,archive,monkeypatch):
    from telethon import TelegramClient,errors,functions
    from telethon.sessions import StringSession
    import time
    async def scenario():
        client=TelegramClient(StringSession(),123,'a'*32,flood_sleep_threshold=0)
        sources={};waits=[]
        for mid in (1,2):
            data=photo(id=mid,created=1700000003-mid);data['media'].update(identity=str(mid),size=4);archive.ingest('-1001',data)
            sources[mid]=SimpleNamespace(id=mid,normalized=data,document=types.InputDocumentFileLocation(id=mid,access_hash=2,file_reference=b'test',thumb_size=''),file=SimpleNamespace(size=4))
        class Sender:
            def send(self,request,ordered=False):
                assert isinstance(request,functions.upload.GetFileRequest)
                future=asyncio.get_running_loop().create_future()
                if request.location.id==1:future.set_exception(errors.FloodWaitError(request,capture=4))
                else:future.set_result(types.upload.File(type=types.storage.FileUnknown(),mtime=0,bytes=b'data'))
                return future
        client._sender=Sender();sync=Synchronizer(config,archive,client)
        sync.sources={('-1001',mid):value for mid,value in sources.items()}
        async def pause(seconds):waits.append(seconds);client._flood_waited_requests.clear()
        sync.wait_flood=pause
        monkeypatch.setattr('family_photo.telegram.normalize',AsyncMock(side_effect=lambda source:source.normalized))
        monkeypatch.setattr('family_photo.telegram.ephemeral',lambda source:False)
        monkeypatch.setattr('family_photo.telegram.make_thumbnail',lambda *args:None)
        monkeypatch.setattr('family_photo.telegram.asyncio.sleep',AsyncMock())
        await sync.downloads()
        with archive.engine.connect() as c:rows={r['message_id']:dict(r) for r in c.execute(select(media)).mappings()}
        assert rows[1]['state']=='pending' and rows[1]['retry_at']>=time.time()+58 and rows[1]['attempts']==3
        assert rows[2]['state']=='ready' and waits==[4,8,16]
        client.session.close()
    asyncio.run(scenario())


@pytest.mark.skipif(not shutil.which('ffmpeg'),reason='FFmpeg unavailable')
def test_video_sticker_preview_is_animated_webp(tmp_path):
    source=tmp_path/'sticker.webm';preview=tmp_path/'preview.webp'
    from PIL import ImageDraw
    for index in range(3):
        frame=Image.new('RGBA',(64,64),(0,0,0,0));ImageDraw.Draw(frame).rectangle((10+index*5,10,35+index*5,40),fill=(0,200,80,255));frame.save(tmp_path/f'frame{index}.png')
    subprocess.run(['ffmpeg','-loglevel','error','-y','-framerate','5','-i',str(tmp_path/'frame%d.png'),'-c:v','libvpx-vp9','-pix_fmt','yuva420p','-auto-alt-ref','0','-an',str(source)],check=True)
    make_thumbnail(source,preview,'sticker')
    with Image.open(preview) as image:
        assert image.format=='WEBP' and image.n_frames>1
        assert image.convert('RGBA').getpixel((0,0))[3]==0
