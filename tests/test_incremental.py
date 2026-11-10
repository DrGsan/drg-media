import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from telethon import TelegramClient, types, functions
from telethon.sessions import StringSession

from conftest import photo
from family_photo.db import media
from family_photo.limits import CHUNK_SIZE
from family_photo.telegram import Synchronizer


@pytest.mark.parametrize('damage', ['none', 'uncommitted_tail', 'corrupt_prefix'])
def test_real_download_survives_restart_and_does_not_refetch_completed_file(config, archive, tmp_path, damage):
    body = b'a'*CHUNK_SIZE + b'b'*123
    offsets = []

    async def scenario():
        source = SimpleNamespace(document=types.InputDocumentFileLocation(id=99, access_hash=2,
            file_reference=b'test', thumb_size=''), file=SimpleNamespace(size=len(body)))

        def client(interrupt=False):
            value = TelegramClient(StringSession(), 123, 'a'*32)
            class Sender:
                def send(self, request, ordered=False):
                    assert isinstance(request, functions.upload.GetFileRequest)
                    offsets.append(request.offset)
                    future = asyncio.get_running_loop().create_future()
                    if interrupt and request.offset == CHUNK_SIZE:
                        future.set_exception(ConnectionError('process interrupted'))
                    else:
                        future.set_result(types.upload.File(type=types.storage.FileUnknown(), mtime=0,
                            bytes=body[request.offset:request.offset+request.limit]))
                    return future
            value._sender = Sender()
            return value

        first = client(True)
        with archive.objects.workspace('download-') as temporary:
            with pytest.raises(ConnectionError):
                await Synchronizer(config, archive, first).download_media(source, Path(temporary)/'original')
        first.session.close()
        partial = next((config.data_dir/'.telegram-downloads').glob('*/original'))
        if damage == 'uncommitted_tail':
            with partial.open('ab') as output: output.write(b'uncommitted bytes')
        elif damage == 'corrupt_prefix':
            with partial.open('r+b') as output: output.write(b'corrupt')
        # Ordinary startup garbage cleanup must preserve the durable checkpoint.
        archive.objects.clean_temporary(startup=True)
        second = client()
        restarted = Synchronizer(config, archive, second)
        path = await restarted.download_media(source, tmp_path/'resumed')
        assert Path(path).read_bytes() == body
        before = list(offsets)
        again = await Synchronizer(config, archive, second).download_media(source, tmp_path/'again')
        assert Path(again).read_bytes() == body and offsets == before
        # Ready-cache cleanup cannot invalidate the uploader's local hard link.
        assert restarted.download_cache.clean({'99'})['temporary_removed'] == 1
        assert Path(again).read_bytes() == body
        second.session.close()

    asyncio.run(scenario())
    assert offsets == [0, CHUNK_SIZE] + ([0, CHUNK_SIZE] if damage == 'corrupt_prefix' else [CHUNK_SIZE])


def test_ready_media_survives_repeated_reconciliation_and_worker_restart(config, archive, monkeypatch):
    data = photo()
    archive.ingest('-1001', data)
    with archive.engine.connect() as connection:
        item = dict(connection.execute(select(media).where(media.c.group_id == '-1001')).mappings().one())
    path = config.data_dir/'source'; path.write_bytes(b'photo-data')
    key = 'media/'+'a'*32
    archive.objects.put_file(key, path)
    archive.attach_download(item, key, None, 10)
    archive.update_group('-1001', cursor=1, history_complete=True)
    source = SimpleNamespace(id=1, date=datetime.fromtimestamp(data['created'], timezone.utc))
    monkeypatch.setattr('family_photo.telegram.normalize', AsyncMock(return_value=data))
    telegram = SimpleNamespace(get_messages=AsyncMock(return_value=[source]))

    async def scenario():
        for _ in range(2):
            restarted = Synchronizer(config, archive, telegram)
            restarted.can_read = AsyncMock(return_value=True)
            restarted.download_media = AsyncMock(side_effect=AssertionError('ready original must not be downloaded'))
            await restarted.apply('-1001', source)
            await restarted.reconcile('-1001')
            await restarted.downloads()
            restarted.download_media.assert_not_called()
            with archive.engine.connect() as connection:
                current = connection.execute(select(media).where(media.c.id == item['id'])).mappings().one()
                assert current['state'] == 'ready' and current['object_key'] == key
            archive.update_group('-1001', reconcile_cursor=0)
    asyncio.run(scenario())


def test_restart_keeps_cursor_and_uses_cached_groups_without_recent_history_scan(config, archive):
    archive.update_group('-1001', cursor=1250, history_complete=True)
    archive.update_group('-1002', cursor=987, history_complete=True)
    telegram = SimpleNamespace(connect=AsyncMock(), is_user_authorized=AsyncMock(return_value=True),
        get_me=AsyncMock(return_value=SimpleNamespace(id=42)), get_input_entity=AsyncMock(), catch_up=AsyncMock(),
        get_messages=AsyncMock(side_effect=lambda gid, **kwargs: [SimpleNamespace(id={-1001:1250,-1002:987}[gid])]))
    sync = Synchronizer(config, archive, telegram)
    sync.register = lambda: None
    async def stop():
        assert all(archive.group(gid)['history_complete'] for gid in config.group_ids)
        await sync.scan('-1001')
        await sync.scan('-1002')
    sync.sync_loop = stop
    asyncio.run(sync._run())
    assert archive.group('-1001')['cursor'] == 1250 and archive.group('-1002')['cursor'] == 987
    assert all(archive.group(gid)['history_complete'] for gid in config.group_ids)
    assert telegram.get_messages.await_count == 2  # only each group's current tip
    assert [call.args[0] for call in telegram.get_input_entity.await_args_list] == [-1001,-1002]
    assert not sync.catching_up


def test_first_group_discovery_stops_when_only_missing_hash_is_found(config, archive):
    reads = []
    async def cached(gid):
        if gid == -1002: raise ValueError('no access hash yet')
    def dialogs():
        async def rows():
            reads.append(-1001)
            yield SimpleNamespace(id=-1001, name='Already known', entity=SimpleNamespace(megagroup=True))
            reads.append(-1002)
            yield SimpleNamespace(id=-1002, name='Discovered', entity=SimpleNamespace(megagroup=True))
            raise AssertionError('do not scan the remaining account dialogs')
        return rows()
    telegram = SimpleNamespace(get_input_entity=cached, iter_dialogs=dialogs)
    asyncio.run(Synchronizer(config, archive, telegram).resolve_groups())
    assert archive.group('-1002')['title'] == 'Discovered' and reads == [-1001,-1002]


def test_restart_suppresses_live_gaps_until_chronological_scan_catches_up(config, archive):
    archive.ingest('-1001', photo(1)); archive.update_group('-1001', cursor=1, history_complete=True)
    handlers = []
    class Client:
        def on(self, event):
            def register(handler): handlers.append(handler); return handler
            return register
        async def get_messages(self, gid, **kwargs): return [SimpleNamespace(id=3)]
        def iter_messages(self, gid, **kwargs):
            assert kwargs['min_id'] == 1
            async def rows():
                yield SimpleNamespace(id=2)
                yield SimpleNamespace(id=3)
            return rows()
    sync = Synchronizer(config, archive, Client()); sync.register(); sync.catching_up = {'-1001'}
    sync.apply = AsyncMock()
    async def scenario():
        await handlers[0](SimpleNamespace(chat_id=-1001, message=SimpleNamespace(id=3)))
        sync.apply.assert_not_awaited()
        await sync.scan('-1001')
        assert [call.args[1].id for call in sync.apply.await_args_list] == [2,3]
        assert archive.group('-1001')['cursor'] == 3 and not sync.catching_up
        await handlers[0](SimpleNamespace(chat_id=-1001, message=SimpleNamespace(id=4)))
        assert sync.apply.await_args.args[1].id == 4
    asyncio.run(scenario())


def test_regular_history_check_and_deep_reconciliation_do_not_run_every_loop(config, archive, monkeypatch):
    now = 2000000000
    monkeypatch.setattr('family_photo.telegram.time.time', lambda: now)
    archive.ingest('-1001', photo())
    archive.update_group('-1001', history_complete=True, cursor=1, last_sync=now-60, last_reconcile=now-3600)
    telegram = SimpleNamespace(get_messages=AsyncMock(side_effect=AssertionError('unnecessary history read')))
    sync = Synchronizer(config, archive, telegram)
    asyncio.run(sync.scan('-1001'))
    asyncio.run(sync.reconcile('-1001'))
    telegram.get_messages.assert_not_called()


def test_abandoned_checkpoint_expires_but_active_download_is_never_cleaned(config, archive, monkeypatch):
    import os
    from family_photo.downloads import DownloadCache
    cache = DownloadCache(archive.objects)
    location = types.InputDocumentFileLocation(id=99, access_hash=2, file_reference=b'test', thumb_size='')
    with cache.open(location, CHUNK_SIZE*2, config.data_dir/'fallback') as checkpoint:
        checkpoint.write(b'a'*CHUNK_SIZE)
        directory = checkpoint.directory
        os.utime(directory, (1,1))
        monkeypatch.setattr('family_photo.downloads.time.time', lambda: 10*86400)
        assert cache.clean(set())['temporary_removed'] == 0
        assert directory.exists()
    assert cache.clean(set())['temporary_removed'] == 1
    assert not directory.exists()
