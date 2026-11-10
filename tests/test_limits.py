"""Meter real Telethon RPC sends with a fake server/clock, never Telegram."""
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import functions,types,errors

from family_photo.limits import RpcLimits, LimitedTelegramClient, read_limits, RPC_INTERVAL, HISTORY_INTERVAL, MEDIA_INTERVAL


class Clock:
    def __init__(self):self.now=1700000000
    def time(self):return self.now
    async def sleep(self,seconds):self.now+=seconds;await asyncio.sleep(0)


def file_request():
    return functions.upload.GetFileRequest(location=types.InputDocumentFileLocation(id=1,access_hash=2,file_reference=b'fixture',thumb_size=''),offset=0,limit=512*1024)


def prepare(config,monkeypatch,handler):
    clock=Clock();monkeypatch.setattr('telethon.client.users.time.time',clock.time)
    client=LimitedTelegramClient(replace(config,api_id=123,api_hash="a"*32));client.session.set_dc(1,'127.0.0.1',80)
    client.rpc_limits=RpcLimits(config,clock.time,clock.sleep)
    trace=[]
    class Sender:
        def send(self,request,ordered=False):
            trace.append((type(request).__name__,clock.time()))
            future=asyncio.get_running_loop().create_future()
            try:future.set_result(handler(request,len(trace)))
            except Exception as error:future.set_exception(error)
            return future
    client._sender=Sender()
    return client,clock,trace


def response():return types.upload.File(type=types.storage.FileUnknown(),mtime=0,bytes=b'block')


def test_all_rpc_sends_are_single_flight_and_paced_including_a_library_batch(config,monkeypatch):
    client,clock,trace=prepare(config,monkeypatch,lambda *args:response())
    async def scenario():
        await asyncio.gather(*(client._call(client._sender,file_request()) for _ in range(4)))
        await client._call(client._sender,[file_request(),file_request()])
    try:asyncio.run(scenario())
    finally:client.session.close()
    assert len(trace)==6 and all(b[1]-a[1]>=MEDIA_INTERVAL for a,b in zip(trace,trace[1:]))
    assert read_limits(config)['next_rpc_at']>=trace[-1][1]+RPC_INTERVAL
    assert (config.data_dir/'.telegram-limits.json').stat().st_mode&0o777==0o600


def test_premium_wait_is_media_only_persisted_and_escalates_without_early_probes(config,monkeypatch):
    def handler(request,index):
        if isinstance(request,functions.upload.GetFileRequest):raise errors.FloodPremiumWaitError(request,17)
        return response()
    client,clock,trace=prepare(config,monkeypatch,handler)
    async def scenario():
        with pytest.raises(errors.FloodPremiumWaitError):await client._call(client._sender,file_request())
        first=read_limits(config)['pauses']['media']
        assert first['seconds']==17 and first['until']==clock.time()+300 and first['code']=='FLOOD_PREMIUM_WAIT'
        # Unrelated metadata and incoming text do not wait behind file cooldown.
        await client._call(client._sender,functions.help.GetConfigRequest())
        assert trace[-1][1]<first['until']
        # A new process reads the deadline; restarting does not clear it.
        client.rpc_limits=RpcLimits(config,clock.time,clock.sleep)
        with pytest.raises(errors.FloodPremiumWaitError):await client._call(client._sender,file_request())
        assert trace[-1][1]>=first['until']
        second=read_limits(config)['pauses']['media']
        assert second['failures']==2 and second['until']==clock.time()+900
        assert len([r for r in trace if r[0]=='GetFileRequest'])==2
    try:asyncio.run(scenario())
    finally:client.session.close()


def test_flood_wait_pauses_all_methods_and_server_deadline_always_wins(config,monkeypatch):
    def handler(request,index):
        if index==1:raise errors.FloodWaitError(request,7200)
        return response()
    client,clock,trace=prepare(config,monkeypatch,handler)
    async def scenario():
        with pytest.raises(errors.FloodWaitError):await client._call(client._sender,file_request())
        pause=read_limits(config)['pauses']['all']
        assert pause['until']>=pause['telegram_until']+5
        await client._call(client._sender,functions.help.GetConfigRequest())
        assert trace[-1][1]>=pause['until']
    try:asyncio.run(scenario())
    finally:client.session.close()
    assert len(trace)==2  # No extra Telethon retries escape the gate.


def test_history_has_additional_spacing_and_nested_resolution_is_metered(config,monkeypatch):
    client,clock,trace=prepare(config,monkeypatch,lambda *args:response())
    history=lambda:functions.messages.GetHistoryRequest(peer=types.InputPeerChat(1),offset_id=0,offset_date=None,
        add_offset=0,limit=100,max_id=0,min_id=0,hash=0)
    async def scenario():
        await client._call(client._sender,history())
        await client._call(client._sender,history())
        request=file_request();resolved=False
        async def resolve(*args):
            nonlocal resolved
            if not resolved:
                resolved=True
                await client._call(client._sender,functions.help.GetConfigRequest())
        request.resolve=resolve
        await client._call(client._sender,request)
    try:asyncio.run(scenario())
    finally:client.session.close()
    assert trace[1][1]-trace[0][1]>=HISTORY_INTERVAL
    assert trace[-1][1]-trace[-2][1]>=RPC_INTERVAL


def test_failed_state_write_releases_single_flight_lock(config,monkeypatch):
    gate=RpcLimits(config);monkeypatch.setattr(gate,'save',lambda:(_ for _ in ()).throw(OSError('disk full')))
    async def scenario():
        with pytest.raises(OSError):
            async with gate.slot(file_request()):raise AssertionError('must not send without persisted budget')
        assert not gate.lock.locked()
    asyncio.run(scenario())


def test_corrupt_cooldown_state_fails_closed(config):
    (config.data_dir/'.telegram-limits.json').write_text('{bad')
    assert read_limits(config)=={'invalid':True}
    with pytest.raises(RuntimeError,match='reader stays stopped'):RpcLimits(config)


def test_short_file_flood_keeps_extra_backoff_on_files_only_across_restart(config):
    clock=Clock();gate=RpcLimits(config,clock.time,clock.sleep)
    gate.rejected(file_request(),errors.FloodWaitError(None,3))
    state=read_limits(config)
    assert state['pauses']['all']['until']==clock.time()+8
    assert state['pauses']['media']['until']==clock.time()+60
    clock.now+=8
    restarted=RpcLimits(config,clock.time,clock.sleep)
    assert restarted.delay(functions.help.GetConfigRequest())==0
    assert restarted.delay(file_request())==52


def test_legacy_file_backoff_is_split_without_shortening_server_or_file_wait(config):
    clock=Clock();gate=RpcLimits(config,clock.time,clock.sleep)
    gate.rejected(file_request(),errors.FloodWaitError(None,9))
    legacy=gate.data['pauses'].pop('media')
    legacy.update(scope='all',until=clock.time()+21600)
    gate.data['pauses']['all']=legacy;gate.save()
    restarted=RpcLimits(config,clock.time,clock.sleep)
    assert restarted.delay(functions.help.GetConfigRequest())==14
    assert restarted.delay(file_request())==21600


def test_dc_switch_retry_gets_a_new_metered_slot(config,monkeypatch):
    def handler(request,index):
        if index==1:raise errors.NetworkMigrateError(request,2)
        return response()
    client,clock,trace=prepare(config,monkeypatch,handler)
    async def switch(dc):client.session.set_dc(dc,'127.0.0.1',80)
    client._switch_dc=switch
    client.is_user_authorized=AsyncMock(return_value=False)
    async def scenario():await client._call(client._sender,functions.help.GetConfigRequest())
    try:asyncio.run(scenario())
    finally:client.session.close()
    assert len(trace)==2 and trace[1][1]-trace[0][1]>=RPC_INTERVAL


def test_media_cooldown_waiter_does_not_lock_out_metadata(config,monkeypatch):
    def handler(request,index):
        if isinstance(request,functions.upload.GetFileRequest):raise errors.FloodPremiumWaitError(request,17)
        return response()
    client,clock,trace=prepare(config,monkeypatch,handler)
    async def scenario():
        with pytest.raises(errors.FloodPremiumWaitError):await client._call(client._sender,file_request())
        until=read_limits(config)['pauses']['media']['until']
        blocked=asyncio.create_task(client._call(client._sender,file_request()))
        await client._call(client._sender,functions.help.GetConfigRequest())
        metadata=next(row for row in trace if row[0]=='GetConfigRequest')
        assert metadata[1]<until
        with pytest.raises(errors.FloodPremiumWaitError):await blocked
        assert trace[-1][1]>=until
    try:asyncio.run(scenario())
    finally:client.session.close()


def test_scoped_status_shows_persisted_pause_and_suppresses_stale_eta(config,archive,client):
    from conftest import photo
    from family_photo.progress import Progress
    from sqlalchemy import select
    from family_photo.db import media
    archive.ingest('-1001',photo());archive.update_group('-1001',history_complete=True,state='online')
    archive.update_group('-1002',history_complete=True,state='online');archive.heartbeat('online')
    with archive.engine.connect() as db:item=dict(db.execute(select(media)).mappings().one())
    progress=Progress(config,archive);progress.begin(item);progress.stage('download',10);progress.transfer(5)
    pause=RpcLimits(config).rejected(file_request(),errors.FloodPremiumWaitError(None,17))
    state=client.get('/v1/groups/-1001/sync').json
    assert state['state']=='paused' and state['pause']['kind']=='premium'
    assert state['current']['speed']==0 and state['eta_seconds'] is None
    assert state['current']['wait_until']>=pause['until']
    assert client.get('/v1/groups/-1002/sync').json['pause'] is None
    # Worker restart retains cooldown even if disposable transfer progress is lost.
    (config.data_dir/'.sync-progress.json').unlink()
    assert client.get('/v1/groups/-1001/sync').json['pause']==state['pause']


def test_fifo_prevents_a_periodic_history_scan_from_starving_older_file_metadata(config):
    gate=RpcLimits(config);order=[]
    # Use short real intervals to exercise competing sleepers at the same due
    # time, rather than a fake clock that advances separately in each task.
    async def scenario():
        loop=asyncio.get_running_loop()
        gate.clock=lambda:loop.time()
        def delay(request):return max(0,gate.data.get('next_rpc_at',0)-loop.time())
        gate.delay=delay
        async def execute(name,repeat=1):
            for _ in range(repeat):
                request=functions.help.GetConfigRequest()
                async with gate.slot(request):
                    order.append(name)
                    gate.data['next_rpc_at']=loop.time()+.02
        # The scan is queued first once; its follow-up must join behind the
        # already waiting file. This remains true at a shared due timestamp.
        gate.data['next_rpc_at']=loop.time()+.02
        await asyncio.wait_for(asyncio.gather(execute('scan',4),execute('file')),2)
        assert order[:2]==['scan','file']
        assert not gate.waiters and not gate.lock.locked()
    asyncio.run(scenario())


def test_file_pacing_learns_server_wait_and_survives_restart(config):
    gate=RpcLimits(config)
    gate.rejected(file_request(),errors.FloodWaitError(None,9))
    assert read_limits(config)['media_interval_seconds']==11
    restarted=RpcLimits(config)
    assert restarted.data['media_interval_seconds']==11
    restarted.rejected(file_request(),errors.FloodWaitError(None,4))
    assert read_limits(config)['media_interval_seconds']==11  # a shorter wait does not erase learning
    restarted.rejected(file_request(),errors.FloodWaitError(None,7200))
    assert read_limits(config)['media_interval_seconds']==60  # cooldown still respects all 7200 seconds


def test_worker_waits_persisted_global_pause_before_even_connecting(config,archive,monkeypatch):
    from family_photo.telegram import Synchronizer
    clock=Clock();monkeypatch.setattr('family_photo.telegram.time.time',clock.time)
    RpcLimits(config,clock.time,clock.sleep).rejected(file_request(),errors.FloodWaitError(None,9))
    telegram=SimpleNamespace(connect=AsyncMock(),is_user_authorized=AsyncMock(return_value=False))
    sync=Synchronizer(config,archive,telegram);sync.register=lambda:None
    async def wait(seconds):
        assert telegram.connect.await_count==0
        assert seconds>=14  # Telegram's 9 seconds plus the five-second margin.
        clock.now+=seconds+1
    sync.wait_flood=wait
    with pytest.raises(RuntimeError,match='Telegram login required'):asyncio.run(sync._run())
    assert telegram.connect.await_count==1
