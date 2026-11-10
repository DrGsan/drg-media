import asyncio
from datetime import datetime,timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from PIL import Image
from sqlalchemy import select
from telethon import types
from family_photo.db import people,emoji_assets
from family_photo.telegram import Synchronizer,normalize
from conftest import message


def test_sender_photo_is_reused_revoked_and_group_gated(config,archive,client,tmp_path,monkeypatch):
    archive.ingest('-1001',message(1));archive.ingest('-1001',message(2))
    path=tmp_path/'avatar.jpg';Image.new('RGB',(40,40),'green').save(path)
    person=types.User(id=42,first_name='Анна',photo=types.UserProfilePhoto(photo_id=123,dc_id=2))
    telegram=SimpleNamespace(get_participants=AsyncMock(return_value=[person]),download_profile_photo=AsyncMock(return_value=str(path)))
    sync=Synchronizer(config,archive,telegram);stamp=1700000000
    monkeypatch.setattr('family_photo.telegram_details.time.time',lambda:stamp)
    asyncio.run(sync.sync_people('-1001'));assert telegram.download_profile_photo.await_count==1
    telegram.get_participants.assert_awaited_with(-1001,limit=100)
    rows=archive.history('-1001')['items'];assert all(row['sender_has_avatar'] and row['sender_avatar_version']=='123' for row in rows)
    first=client.get('/v1/groups/-1001/people/42/avatar?v=123');assert first.status_code==200 and first.content_type=='image/jpeg'
    assert client.get('/v1/groups/-1002/people/42/avatar?v=123').status_code==404
    assert client.get('/v1/groups/-1001/people/42/avatar?v=124').status_code==404
    assert client.get('/v1/groups/-1001/people/42/avatar',headers={'Authorization':'invalid'}).status_code==401
    stamp+=301;asyncio.run(sync.sync_people('-1001'));assert telegram.download_profile_photo.await_count==1
    person.photo=None;stamp+=301;asyncio.run(sync.sync_people('-1001'))
    assert client.get('/v1/groups/-1001/people/42/avatar?v=123').status_code==404


def test_inline_custom_emoji_offsets_reactions_and_asset_access(config,archive,client,tmp_path):
    emoji=types.ReactionEmoji('🔥')
    source=SimpleNamespace(id=1,date=datetime.now(timezone.utc),sender_id=42,get_sender=AsyncMock(return_value=types.User(id=42,first_name='Анна')),raw_text='😎 кот 🐈',entities=[types.MessageEntityCustomEmoji(offset=7,length=2,document_id=123)],reactions=types.MessageReactions(results=[types.ReactionCount(reaction=emoji,count=1)],recent_reactions=[types.MessagePeerReaction(peer_id=types.PeerUser(42),date=datetime.now(timezone.utc),reaction=emoji)]))
    value=asyncio.run(normalize(source));assert value['custom_emojis']==[{'offset':7,'length':2,'document_id':'123'}]
    assert value['reactions'][0]['people']==[{'id':'42'}] and value['reactions'][0]['complete']
    archive.ingest('-1001',value);archive.queue_emoji('-1001',['123'])
    path=tmp_path/'emoji.png';Image.new('RGBA',(40,40),'red').save(path)
    with archive.upload('emojis') as key:
        archive.objects.put_file(key,path,'image/png');archive.save_emoji('-1001','123',object_key=key,mime='image/png',state='ready')
    assert archive.history('-1001')['items'][0]['custom_emojis'][0]['state']=='ready'
    assert client.get('/v1/groups/-1001/emoji/123').status_code==200
    assert client.get('/v1/groups/-1002/emoji/123').status_code==404
    assert client.get('/v1/groups/-1001/emoji/abc').status_code==404
    archive.collect_garbage();assert b''.join(archive.objects.read(key))==path.read_bytes()


def test_additive_cache_migration_and_rollback_preserve_original_rows(archive):
    from family_photo.db import metadata
    archive.ingest('-1001',message(17))
    originals=[table for table in metadata.sorted_tables if table not in (people,emoji_assets)]
    def snapshot():
        with archive.engine.connect() as c:return {table.name:sorted([tuple(row) for row in c.execute(select(table))],key=repr) for table in originals}
    before=snapshot()
    for table in (people,emoji_assets):table.drop(archive.engine)
    for table in (people,emoji_assets):table.create(archive.engine,checkfirst=True);table.create(archive.engine,checkfirst=True)
    assert snapshot()==before
    for table in (people,emoji_assets):table.drop(archive.engine)
    assert snapshot()==before
    for table in (people,emoji_assets):table.create(archive.engine)
