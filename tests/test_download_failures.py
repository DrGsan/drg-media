"""Exercise real download iteration against fake Telegram replies only."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select
from telethon import TelegramClient, types, errors, functions
from telethon.sessions import StringSession

from conftest import photo
from family_photo.db import media
from family_photo.limits import CHUNK_SIZE
from family_photo.telegram import Synchronizer


def prepare(config,archive,monkeypatch,handler,body_size=4):
    data=photo(1);data['media'].update(size=body_size)
    archive.ingest('-1001',data)
    source=SimpleNamespace(id=1,normalized=data,
        document=types.InputDocumentFileLocation(id=1,access_hash=2,file_reference=b'test',thumb_size=''),
        file=SimpleNamespace(size=body_size))
    sent=[]
    monkeypatch.setattr('family_photo.telegram.normalize',AsyncMock(return_value=data))
    monkeypatch.setattr('family_photo.telegram.ephemeral',lambda _:False)
    monkeypatch.setattr('family_photo.telegram.make_thumbnail',lambda *a:None)
    monkeypatch.setattr('family_photo.telegram.asyncio.sleep',AsyncMock())
    def reader():
        client=TelegramClient(StringSession(),123,'a'*32,flood_sleep_threshold=0)
        class Sender:
            def send(self,request,ordered=False):
                assert isinstance(request,functions.upload.GetFileRequest)
                sent.append(request.offset);future=asyncio.get_running_loop().create_future()
                try:future.set_result(handler(request,len(sent)))
                except Exception as error:future.set_exception(error)
                return future
        client._sender=Sender();sync=Synchronizer(config,archive,client)
        sync.sources={('-1001',1):source}
        async def wait(seconds):client._flood_waited_requests.clear()
        sync.wait_flood=wait
        return sync,client
    return reader,sent


def row(archive):
    with archive.engine.connect() as c:return dict(c.execute(select(media)).mappings().one())


def due(archive):
    with archive.transaction() as c:c.execute(media.update().values(retry_at=0))


def test_exactly_five_file_rejections_stop_across_reader_restart_and_later_scans(config,archive,monkeypatch):
    def reject(request,count):raise errors.FloodWaitError(request,4)
    make,sent=prepare(config,archive,monkeypatch,reject)
    async def run():
        sync,client=make();await sync.downloads();client.session.close()
        assert len(sent)==3 and row(archive)['attempts']==3
        due(archive)
        sync,client=make();await sync.downloads();client.session.close()
        assert len(sent)==5 and row(archive)['attempts']==5
        assert row(archive)['state']=='paused' and row(archive)['error']=='auto:FloodWaitError'
        assert row(archive)['object_key'] is None
        archive.ingest('-1001',photo(1,text='Changed caption'))
        assert row(archive)['state']=='paused' and row(archive)['attempts']==5
        due(archive)
        for _ in range(3):
            sync,client=make();await sync.downloads();client.session.close()
        assert len(sent)==5
        assert archive.history('-1001')['items'][0]['media'][0]['state']=='paused'
    asyncio.run(run())


def test_metadata_flood_wait_does_not_count_as_a_failed_file(config,archive,monkeypatch):
    make,sent=prepare(config,archive,monkeypatch,lambda *_:None)
    async def run():
        for _ in range(7):
            due(archive);sync,client=make();sync.sources.clear()
            async def metadata(*args,**kwargs):
                raise errors.FloodWaitError(None,4)
                yield
            monkeypatch.setattr(client,'iter_messages',metadata)
            await sync.downloads();client.session.close()
        assert sent==[] and row(archive)['attempts']==0 and row(archive)['state']=='pending'
    asyncio.run(run())


def test_connection_failures_and_storage_errors_do_not_mark_telegram_file_unavailable(config,archive,monkeypatch):
    def disconnected(request,count):raise ConnectionError('offline')
    make,sent=prepare(config,archive,monkeypatch,disconnected)
    async def run():
        for _ in range(7):
            due(archive);sync,client=make();await sync.downloads();client.session.close()
        assert len(sent)==7 and row(archive)['attempts']==0 and row(archive)['state']=='failed'
        sync,client=make();item=row(archive)
        for _ in range(7):assert not sync.record_file_failure(item,ValueError('storage error'),count=False)
        assert row(archive)['attempts']==0 and row(archive)['state']=='failed'
        client.session.close()
    asyncio.run(run())


def test_new_confirmed_bytes_reset_the_consecutive_failure_count(config,archive,monkeypatch):
    body=b'a'*CHUNK_SIZE+b'b'*4
    def reply(request,count):
        if count in (2,3):raise errors.FloodWaitError(request,4)
        return types.upload.File(type=types.storage.FileUnknown(),mtime=0,bytes=body[request.offset:request.offset+request.limit])
    make,sent=prepare(config,archive,monkeypatch,reply,len(body))
    with archive.transaction() as c:c.execute(media.update().values(attempts=4))
    async def run():
        sync,client=make();await sync.downloads();client.session.close()
    asyncio.run(run())
    assert sent==[0,CHUNK_SIZE,CHUNK_SIZE,CHUNK_SIZE]
    saved=row(archive)
    assert saved['state']=='ready' and saved['attempts']==0
    assert b''.join(archive.objects.read(saved['object_key']))==body
