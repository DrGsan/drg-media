import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select
from PIL import Image

from family_photo.db import media
from family_photo.telegram import normalize, Synchronizer


def test_normalized_profiles_include_optional_username_without_contact_alias():
    async def author(**fields):
        sender=SimpleNamespace(**fields)
        source=SimpleNamespace(id=1,date=datetime.now(timezone.utc),sender_id=42,get_sender=AsyncMock(return_value=sender))
        return await normalize(source)
    assert asyncio.run(author(first_name='Анна',last_name='Иванова',username='anna_test'))['sender']=='Анна Иванова (@anna_test)'
    assert asyncio.run(author(first_name='Анна'))['sender']=='Анна'
    assert asyncio.run(author(first_name='Прозвище',last_name='Моё',username='anna_test',contact=True))['sender']=='@anna_test'
    assert asyncio.run(author(first_name='Прозвище',contact=True))['sender']=='Автор не уточнён'


def test_group_avatar_rename_change_removal_and_access(config, archive, client, tmp_path, monkeypatch):
    path=tmp_path/'avatar.png';Image.new('RGB',(40,40),'red').save(path)
    entity=SimpleNamespace(title='Новое название',photo=SimpleNamespace(photo_id=123))
    telegram=SimpleNamespace(get_entity=AsyncMock(return_value=entity),download_profile_photo=AsyncMock(return_value=str(path)))
    sync=Synchronizer(config,archive,telegram);asyncio.run(sync.sync_group_profile('-1001'))
    item=next(g for g in client.get('/v1/groups').json['items'] if g['id']=='-1001')
    assert item['title']=='Новое название' and item['has_avatar'] and item['avatar_version']=='123'
    first_key=archive.group_profile('-1001')['avatar_key']
    assert client.get('/v1/groups/-1001/avatar?v=123').status_code==200
    assert client.get('/v1/groups/-1001/avatar?v=wrong').status_code==404
    assert client.get('/v1/groups/-999/avatar').status_code==404
    assert client.get('/v1/groups/-1001/avatar',headers={'Authorization':'wrong'}).status_code==401
    asyncio.run(sync.sync_group_profile('-1001'));assert telegram.get_entity.await_count==1
    archive.save_group_profile('-1001',checked_at=0)
    asyncio.run(sync.sync_group_profile('-1001'));assert telegram.download_profile_photo.await_count==1
    entity.photo.photo_id=124;archive.save_group_profile('-1001',checked_at=0)
    asyncio.run(sync.sync_group_profile('-1001'));assert archive.group_profile('-1001')['avatar_key']!=first_key
    entity.photo=None;archive.save_group_profile('-1001',checked_at=0)
    asyncio.run(sync.sync_group_profile('-1001'));assert client.get('/v1/groups/-1001/avatar').status_code==404
    assert archive.history('-1001')['items']==[]


def test_bound_group_profile_refresh_keeps_id_history_and_originals(config,archive,tmp_path,monkeypatch):
    from conftest import photo
    from family_photo.db import groups
    from sqlalchemy import func
    # The existing export ID is also what site permissions refer to.
    gid='export:abcdef0123456789'
    config=replace(config,group_ids=('-1001',),group_bindings=(('-1001',gid),))
    archive.add_group(gid,'Лёвик',kind='supergroup');archive.ingest(gid,photo())
    with archive.engine.connect() as c:item=dict(c.execute(select(media).where(media.c.group_id==gid)).mappings().one())
    original=tmp_path/'original.jpg';Image.new('RGB',(40,40),'blue').save(original)
    with archive.upload('media') as key:
        archive.objects.put_file(key,original,'image/jpeg');archive.attach_download(item,key,None,original.stat().st_size)
    path=tmp_path/'avatar.png';Image.new('RGB',(40,40),'red').save(path)
    entity=SimpleNamespace(title='Лёвик',photo=SimpleNamespace(photo_id=123))
    telegram=SimpleNamespace(get_entity=AsyncMock(return_value=entity),download_profile_photo=AsyncMock(return_value=str(path)))
    sync=Synchronizer(config,archive,telegram)
    stamp=1700000000;monkeypatch.setattr('family_photo.telegram.time.time',lambda:stamp)
    asyncio.run(sync.sync_group_profile(gid));first=archive.group_profile(gid)['avatar_key']
    # Renaming alone does not download the unchanged avatar again.
    entity.title='Лёвик · семейные фото';stamp+=301
    asyncio.run(sync.sync_group_profile(gid))
    assert archive.group(gid)['title']==entity.title
    assert archive.group_profile(gid)['avatar_key']==first
    assert telegram.download_profile_photo.await_count==1
    telegram.get_entity.assert_awaited_with(-1001)
    # New photo identity changes both content and the versioned browser URL.
    entity.photo.photo_id=124;Image.new('RGB',(40,40),'green').save(path);stamp+=301
    asyncio.run(sync.sync_group_profile(gid))
    second=archive.group_profile(gid)['avatar_key'];assert second!=first
    import io
    with Image.open(io.BytesIO(b''.join(archive.objects.read(second)))) as image:
        red,green,blue=image.getpixel((10,10));assert green>red+50
    archive.collect_garbage()
    assert b''.join(archive.objects.read(key))==original.read_bytes()
    assert archive.history(gid)['items'][0]['media'][0]['id']==item['id']
    # Removing a Telegram avatar also clears its site version and reference.
    entity.photo=None;stamp+=301;asyncio.run(sync.sync_group_profile(gid))
    assert archive.group_profile(gid)['avatar_key'] is None
    assert archive.group_profile(gid)['avatar_identity'] is None
    assert len(archive.history(gid)['items'])==1
    with archive.engine.connect() as c:
        assert c.execute(select(func.count()).select_from(groups).where(groups.c.id==gid)).scalar_one()==1
        assert c.execute(select(media.c.object_key).where(media.c.id==item['id'])).scalar_one()==key


def test_failed_replacement_keeps_old_avatar_and_updates_title_then_retries(config,archive,tmp_path,monkeypatch):
    path=tmp_path/'avatar.png';Image.new('RGB',(40,40),'red').save(path)
    entity=SimpleNamespace(title='Лёвик',photo=SimpleNamespace(photo_id=1))
    telegram=SimpleNamespace(get_entity=AsyncMock(return_value=entity),download_profile_photo=AsyncMock(return_value=str(path)))
    sync=Synchronizer(config,archive,telegram);stamp=1700000000
    monkeypatch.setattr('family_photo.telegram.time.time',lambda:stamp)
    asyncio.run(sync.sync_group_profile('-1001'));old=archive.group_profile('-1001')['avatar_key']
    entity.title='Новое название';entity.photo.photo_id=2;stamp+=301
    telegram.download_profile_photo.return_value=None
    import pytest
    with pytest.raises(ValueError,match='avatar_unavailable'):asyncio.run(sync.sync_group_profile('-1001'))
    assert archive.group('-1001')['title']=='Новое название'
    assert archive.group_profile('-1001')['avatar_key']==old
    asyncio.run(sync.sync_group_profile('-1001'))
    assert telegram.download_profile_photo.await_count==2  # failed calls are throttled too
    telegram.download_profile_photo.return_value=str(path);stamp+=301
    asyncio.run(sync.sync_group_profile('-1001'))
    assert archive.group_profile('-1001')['avatar_identity']=='2'
    assert archive.group_profile('-1001')['avatar_key']!=old
