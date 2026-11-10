import asyncio
import io
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from sqlalchemy import select
from telethon import types
from conftest import message, photo
from family_photo.db import media, export_files
from family_photo.telegram import normalize, make_thumbnail
from family_photo.animations import classify
from family_photo.group_management import Registry
from family_photo.exports import Exports


def source(mime='video/mp4', animated=False, sticker=False):
    attrs=[types.DocumentAttributeAnimated()] if animated else []
    return SimpleNamespace(id=1, date=datetime.now(timezone.utc), sender_id=42,
        get_sender=AsyncMock(return_value=types.User(id=42,first_name='Анна')),
        raw_text='',document=SimpleNamespace(id=123,attributes=attrs),
        file=SimpleNamespace(mime_type=mime,name='file',ext='',size=10),sticker=sticker)


@pytest.mark.parametrize('mime,animated,sticker,expected',[
    ('video/mp4',True,False,'animation'),('image/gif',False,False,'animation'),
    ('video/mp4',False,False,'video'),('image/jpeg',False,False,'photo'),
    ('video/webm',True,True,'sticker')])
def test_telegram_uses_animation_attribute_not_duration(mime,animated,sticker,expected):
    assert asyncio.run(normalize(source(mime,animated,sticker)))['media']['kind']==expected


def test_reclassification_preserves_copies_originals_ids_and_retires_old_zips(config,archive,client):
    registry=Registry(archive,config);exports=Exports(archive,registry)
    for mid,kind,mime in [(1,'video','video/mp4'),(2,'photo','image/jpeg')]:
        archive.ingest('-1001',photo(mid)|{'media':dict(identity=str(mid),kind=kind,mime=mime,name=f'{mid}.mp4',size=10)})
        with archive.engine.connect() as c:item=dict(c.execute(select(media).where(media.c.message_id==mid)).mappings().one())
        path=config.data_dir/'original';path.write_bytes(b'x'*10)
        with archive.upload('media') as key:
            archive.objects.put_file(key,path);archive.attach_download(item,key,None,10)
    copied=registry.copy('-1001',uuid.uuid4().hex)
    with archive.transaction() as c:
        # A frozen imported identity can differ, but the immutable object is shared.
        c.execute(media.update().where(media.c.group_id==copied,media.c.message_id==1).values(identity='export:old'))
    queued=exports.create('owner',['-1001'],True,uuid.uuid4().hex)
    ready=exports.create('owner',[copied],True,uuid.uuid4().hex);exports.build(ready['id'])
    with archive.engine.connect() as c:before={r['id']:dict(r) for r in c.execute(select(media)).mappings()}
    changed=classify(archive,[dict(identity='1',mime='video/mp4')]);assert len(changed)==2
    assert not classify(archive,[dict(identity='1',mime='video/mp4')])
    with archive.engine.connect() as c:
        after={r['id']:dict(r) for r in c.execute(select(media)).mappings()}
        assert not c.execute(select(export_files).where(export_files.c.job_id==queued['id'])).first()
    for key,row in before.items():
        assert after[key]==row|({'kind':'animation'} if row['message_id']==1 else {})
        assert b''.join(archive.objects.read(row['object_key']))==b'x'*10
    assert exports.read(queued['id'])['error']=='media_type_corrected'
    assert exports.read(ready['id'])['state']=='cleaning'
    for gid in ['-1001',copied]:
        gallery=client.get(f'/v1/groups/{gid}/media').json
        assert [item['message_id'] for item in gallery['items']]==[2]
        assert archive.history(gid)['items'][0]['media'][0]['kind']=='animation'
        job=exports.create('owner',[gid],True,uuid.uuid4().hex)
        assert job['count']==1
    archive.collect_garbage()
    for row in after.values():assert b''.join(archive.objects.read(row['object_key']))==b'x'*10


def test_gif_thumbnail_does_not_replace_animated_original(tmp_path):
    path=tmp_path/'source.gif';out=tmp_path/'thumb.jpg'
    Image.new('RGB',(20,20),'red').save(path,save_all=True,append_images=[Image.new('RGB',(20,20),'blue')],duration=100,loop=0)
    body=path.read_bytes();make_thumbnail(path,out,'animation')
    assert path.read_bytes()==body
    with Image.open(path) as image:assert image.n_frames==2
    with Image.open(out) as image:assert image.format=='JPEG'


def test_false_gif_mime_keeps_the_real_jpeg_and_repairs_its_copy(config,archive):
    from family_photo.telegram import Synchronizer
    src=source('image/gif');data=asyncio.run(normalize(src))
    # A source can be labelled GIF even though it is an ordinary still JPEG.
    assert not data.get('animation_verified')
    archive.ingest('-1001',data)
    path=config.data_dir/'jpeg';Image.new('RGB',(20,20),'red').save(path,format='JPEG')
    with archive.engine.connect() as c:item=dict(c.execute(select(media)).mappings().one())
    with archive.upload('media') as key:
        archive.objects.put_file(key,path);archive.attach_download(item,key,None,path.stat().st_size)
    registry=Registry(archive,config);copied=registry.copy('-1001',uuid.uuid4().hex)
    sync=Synchronizer(config,archive,SimpleNamespace())
    asyncio.run(sync.apply('-1001',src))
    with archive.engine.connect() as c:
        rows=list(c.execute(select(media)).mappings())
        assert len(rows)==2 and all(row['kind']=='photo' and row['mime']=='image/jpeg' for row in rows)
    asyncio.run(sync.apply('-1001',src))
    assert archive.history(copied)['items'][0]['media'][0]['kind']=='photo'
    assert b''.join(archive.objects.read(key))==path.read_bytes()


def test_gif_download_verifies_bytes_before_correcting_legacy_photo(config,archive):
    from family_photo.telegram import Synchronizer
    src=source('image/gif');data=asyncio.run(normalize(src));data['media']['kind']='photo'
    archive.ingest('-1001',data)
    path=config.data_dir/'gif';Image.new('RGB',(20,20),'red').save(path,format='GIF',save_all=True,append_images=[Image.new('RGB',(20,20),'blue')],duration=80,loop=0)
    sync=Synchronizer(config,archive,SimpleNamespace());sync.sources[('-1001',1)]=src
    sync.download_media=AsyncMock(return_value=path)
    asyncio.run(sync.downloads(limit=1))
    item=archive.history('-1001')['items'][0]['media'][0]
    assert item['kind']=='animation' and item['state']=='ready' and item['mime']=='image/gif'
