import asyncio
import hashlib
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from conftest import photo
from family_photo.api import create_app
from family_photo.config import Config
from family_photo.db import media, messages
from family_photo.telegram import Synchronizer, send_outbox

EXPORT = 'export:1234567890abcdef'

class MediaClient:
    def __init__(self,body=b'photo-data'):
        self.body=body;self.calls=0
    def iter_download(self,location,**args):
        async def chunks():
            self.calls+=1
            yield self.body[args.get('offset',0):]
        return chunks()

def source_file(**values):
    return SimpleNamespace(document=object(),file=SimpleNamespace(size=10),**values)



def bound(config):
    return replace(config, group_ids=('-1003','-1002'), group_bindings=(('-1003',EXPORT),))


def imported(archive):
    archive.add_group(EXPORT,'Imported',kind='export')
    data=photo(sender_id='export:author')
    data['media']['identity']='export:'+hashlib.sha256(b'photo-data').hexdigest()
    archive.ingest(EXPORT,data)
    with archive.engine.connect() as c:
        item=dict(c.execute(select(media).where(media.c.group_id==EXPORT)).mappings().one())
    path=archive.objects.config.data_dir/'source';path.write_bytes(b'photo-data')
    archive.objects.put_file('media/'+'a'*32,path)
    archive.attach_download(item,'media/'+'a'*32,None,10)
    return data,item


def test_binding_config_is_explicit_unique_and_only_for_configured_groups(config,monkeypatch):
    value=bound(config);value.validate()
    assert value.archive_group_ids==(EXPORT,'-1002')
    assert value.telegram_id(EXPORT)=='-1003' and value.archive_id('-1003')==EXPORT
    monkeypatch.setenv('FP_TELEGRAM_GROUPS','-1003,-1002')
    monkeypatch.setenv('FP_TELEGRAM_ARCHIVE_BINDINGS','{"-1003":"'+EXPORT+'"}')
    assert Config.from_env().group_bindings==value.group_bindings
    for bindings in [(('-999',EXPORT),),(('-1003','../bad'),),(('-1003',EXPORT),('-1002',EXPORT))]:
        with pytest.raises(ValueError):replace(value,group_bindings=bindings).validate()
    monkeypatch.setenv('FP_TELEGRAM_ARCHIVE_BINDINGS','[1]')
    with pytest.raises(ValueError):Config.from_env()


def test_bound_group_api_keeps_export_urls_and_hides_duplicate_numeric_group(config,archive):
    imported(archive)
    archive.add_group(EXPORT,'Live',kind='supergroup')
    client=create_app(bound(config),archive).test_client()
    client.environ_base['HTTP_AUTHORIZATION']='Bearer '+config.service_token
    assert {g['id'] for g in client.get('/v1/groups').json['items']}=={EXPORT,'-1002'}
    assert client.get('/v1/groups/'+EXPORT+'/messages').json['items'][0]['id']==1
    assert client.get('/v1/groups/-1003/messages').status_code==404
    assert client.get('/v1/groups/-1001/messages').status_code==404


@pytest.mark.parametrize('edited',[False,True])
def test_adopting_import_retains_file_id_object_and_preview(config,archive,monkeypatch,edited):
    data,item=imported(archive)
    incoming=photo();incoming['media']['identity']='telegram-photo-42'
    monkeypatch.setattr('family_photo.telegram.normalize',AsyncMock(return_value=incoming))
    async def download(message,file):Path(file).write_bytes(b'photo-data');return file
    client=MediaClient()
    source=source_file(id=1,date=datetime.fromtimestamp(data['created'],timezone.utc),
                           edit_date=datetime.now(timezone.utc) if edited else None)
    sync=Synchronizer(bound(config),archive,client)
    asyncio.run(sync.apply(EXPORT,source))
    asyncio.run(sync.apply(EXPORT,source))
    with archive.engine.connect() as c:
        current=dict(c.execute(select(media).where(media.c.group_id==EXPORT)).mappings().one())
        assert current['id']==item['id'] and current['object_key']=='media/'+'a'*32
        assert current['identity']=='telegram-photo-42' and current['state']=='ready'
    assert client.calls==(1 if edited else 0)
    assert b''.join(archive.objects.read(current['object_key']))==b'photo-data'


def test_changed_export_media_is_not_falsely_adopted(config,archive,monkeypatch):
    data,item=imported(archive)
    incoming=photo();incoming['media']['identity']='changed'
    monkeypatch.setattr('family_photo.telegram.normalize',AsyncMock(return_value=incoming))
    async def download(message,file):Path(file).write_bytes(b'different!');return file
    client=MediaClient(b'different!')
    source=source_file(id=1,date=datetime.fromtimestamp(data['created'],timezone.utc),edit_date=datetime.now(timezone.utc))
    asyncio.run(Synchronizer(bound(config),archive,client).apply(EXPORT,source))
    with archive.engine.connect() as c:
        current=dict(c.execute(select(media).where(media.c.group_id==EXPORT)).mappings().one())
    assert current['id']!=item['id'] and current['identity']=='changed' and current['state']=='pending'


def test_wrong_binding_date_never_changes_import(config,archive,monkeypatch):
    data,item=imported(archive)
    source=SimpleNamespace(id=1,date=datetime.fromtimestamp(data['created']+60,timezone.utc))
    with pytest.raises(ValueError,match='date_mismatch'):
        asyncio.run(Synchronizer(bound(config),archive,None).apply(EXPORT,source))
    with archive.engine.connect() as c:
        assert c.execute(select(media.c.identity).where(media.c.id==item['id'])).scalar_one().startswith('export:')
        assert c.execute(select(messages.c.sender_id).where(messages.c.group_id==EXPORT)).scalar_one()=='export:author'


def test_scan_uses_live_id_but_keeps_cursor_under_export_id(config,archive):
    imported(archive)
    client=SimpleNamespace(get_messages=AsyncMock(return_value=[]))
    asyncio.run(Synchronizer(bound(config),archive,client).scan(EXPORT))
    client.get_messages.assert_awaited_once_with(-1003,limit=1)
    assert archive.group(EXPORT)['history_complete']


def test_live_events_route_to_archive_and_ignore_other_groups(config,archive):
    imported(archive);archive.update_group(EXPORT,history_complete=True)
    class Client:
        handlers=[]
        def on(self,event):
            def register(handler):self.handlers.append(handler);return handler
            return register
    client=Client();sync=Synchronizer(bound(config),archive,client)
    sync.apply=AsyncMock();sync.register();sync.register()
    assert len(client.handlers)==4  # startup retries must not duplicate handlers
    event=SimpleNamespace(chat_id=-1003,message=SimpleNamespace(id=1),deleted_ids=[1])
    asyncio.run(client.handlers[0](event));sync.apply.assert_awaited_once_with(EXPORT,event.message)
    asyncio.run(client.handlers[2](SimpleNamespace(chat_id=-999,deleted_ids=[1])))
    assert archive.history(EXPORT)['items']
    asyncio.run(client.handlers[2](event))
    assert archive.history(EXPORT)['items']==[]


def test_bound_bot_delivery_uses_numeric_telegram_id(config,archive):
    imported(archive)
    archive.enqueue(EXPORT,'actor','Author','bound-send','hello')
    class HTTP:
        async def post(self,url,**kwargs):
            assert kwargs['json']['chat_id']=='-1003'
            return httpx.Response(200,json={'ok':True,'result':{'message_id':2}})
    asyncio.run(send_outbox(bound(config),archive,HTTP()))


def test_download_reuses_live_event_object_without_a_metadata_request(config,archive,monkeypatch):
    incoming=photo()
    monkeypatch.setattr('family_photo.telegram.normalize',AsyncMock(return_value=incoming))
    monkeypatch.setattr('family_photo.telegram.asyncio.sleep',AsyncMock())
    async def download(message,file):Path(file).write_bytes(b'photo-data');return file
    client=MediaClient()
    source=source_file(id=1,date=datetime.fromtimestamp(incoming['created'],timezone.utc),edit_date=None)
    sync=Synchronizer(config,archive,client)
    async def run():
        await sync.apply('-1001',source)
        await sync.downloads()
    asyncio.run(run())
    with archive.engine.connect() as c:
        item=c.execute(select(media).where(media.c.group_id=='-1001')).mappings().one()
        assert item['state']=='ready' and item['size']==10
    assert not sync.sources


def test_restarted_download_reads_only_the_bound_source_message(config,archive,monkeypatch):
    archive.add_group(EXPORT,'Bound',kind='supergroup');archive.ingest(EXPORT,photo())
    incoming=photo();source=source_file(id=1,date=datetime.fromtimestamp(incoming['created'],timezone.utc))
    monkeypatch.setattr('family_photo.telegram.normalize',AsyncMock(return_value=incoming))
    monkeypatch.setattr('family_photo.telegram.asyncio.sleep',AsyncMock())
    async def download(message,file):Path(file).write_bytes(b'photo-data');return file
    class Client(MediaClient):
        def iter_messages(self,gid,**args):
            assert gid==-1003 and args=={'min_id':0,'max_id':2,'limit':1}
            async def rows():yield source
            return rows()
    asyncio.run(Synchronizer(bound(config),archive,Client()).downloads())
    with archive.engine.connect() as c:assert c.execute(select(media.c.state).where(media.c.group_id==EXPORT)).scalar_one()=='ready'


def test_service_events_do_not_create_blank_messages_or_notifications(config,archive):
    source=SimpleNamespace(id=1,date=datetime.now(timezone.utc),action=object())
    asyncio.run(Synchronizer(config,archive,None).apply('-1001',source))
    assert archive.history('-1001')['items']==[]


@pytest.mark.parametrize('flood_type',['FloodWaitError','FloodPremiumWaitError'])
@pytest.mark.parametrize('pause_count',[1,2])
def test_real_telethon_download_resumes_the_failed_chunk_after_flood_wait(config,archive,tmp_path,flood_type,pause_count):
    from telethon import TelegramClient,types,errors,functions
    from telethon.sessions import StringSession
    body=b'a'*(512*1024)+b'b'*(128*1024)
    offsets=[];waits=[];streams=[]
    async def run():
        client=TelegramClient(StringSession(),123,'a'*32,flood_sleep_threshold=0)
        class Sender:
            paused=0
            def send(self,request,ordered=False):
                assert isinstance(request,functions.upload.GetFileRequest)
                offsets.append(request.offset)
                future=asyncio.get_running_loop().create_future()
                if request.offset==512*1024 and self.paused<pause_count:
                    self.paused+=1;future.set_exception(getattr(errors,flood_type)(request,capture=4))
                else:future.set_result(types.upload.File(type=types.storage.FileUnknown(),mtime=0,bytes=body[request.offset:request.offset+request.limit]))
                return future
        client._sender=Sender()
        download=client.iter_download
        def tracked(*args,**kwargs):
            stream=download(*args,**kwargs);streams.append(stream);return stream
        client.iter_download=tracked
        sync=Synchronizer(config,archive,client)
        async def wait(seconds):
            waits.append(seconds)
            client._flood_waited_requests.clear()
        sync.wait_flood=wait
        source=SimpleNamespace(document=types.InputDocumentFileLocation(id=1,access_hash=2,file_reference=b'test',thumb_size=''),file=SimpleNamespace(size=len(body)))
        path=await sync.download_media(source,tmp_path/'resumed')
        assert Path(path).read_bytes()==body
        client.session.close()
    asyncio.run(run())
    assert offsets==[0]+[512*1024]*(pause_count+1)  # max-size chunks reduce RPC count
    assert waits==[4*2**i for i in range(pause_count)]
    assert len(streams)==1 and streams[0]._sender is None
