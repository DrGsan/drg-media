import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from telethon import types,errors
from family_photo.connection import record,source_status
from family_photo.telegram import Synchronizer
from family_photo.progress import snapshot
from conftest import message

GID='-1001'


def test_source_health_requires_own_fresh_confirmation_not_messages_or_worker(app,archive,monkeypatch):
    archive.heartbeat('online');archive.update_group(GID,state='online',history_complete=True)
    assert source_status(archive,GID)['state']=='checking'
    record(archive,GID,'working');assert source_status(archive,GID)['state']=='working'
    checked=source_status(archive,GID)['last_success']
    assert source_status(archive,GID,stamp=checked+901)['state']=='offline'
    archive.heartbeat('online');assert source_status(archive,GID,stamp=checked+121)['state']=='offline'
    now=time.time()
    monkeypatch.setattr('time.time',lambda:now+901);archive.heartbeat('online')
    assert source_status(archive,GID)['state']=='checking'
    monkeypatch.undo()
    # An empty but reachable source is healthy; no new messages are needed.
    record(archive,GID,'working');assert not archive.history(GID)['items']
    record(archive,GID,'unavailable');status=source_status(archive,GID)
    assert status['state']=='unavailable' and status['last_success']>=checked


def test_migration_is_explicit_permanent_and_preserves_history_avatar_and_source(app,archive,config,tmp_path):
    from PIL import Image
    archive.ingest(GID,message())
    avatar=tmp_path/'avatar.png';Image.new('RGB',(10,10),'red').save(avatar)
    archive.store_avatar(GID,avatar,'avatar-v1');key=archive.group_profile(GID)['avatar_key']
    entity=SimpleNamespace(title='Та же группа',photo=None,migrated_to=types.InputChannel(12345,999),deactivated=True)
    remote=SimpleNamespace(get_entity=AsyncMock(return_value=entity),download_profile_photo=AsyncMock())
    asyncio.run(Synchronizer(config,archive,remote).sync_group_profile(GID))
    status=source_status(archive,GID)
    assert status['state']=='migrated' and status['migrated_to']=='-1000000012345'
    assert archive.group_profile(GID)['avatar_key']==key
    assert len(archive.history(GID)['items'])==1 and archive.registry.read()['groups'][GID]['telegram_id']==GID
    record(archive,GID,'working');assert source_status(archive,GID)['state']=='migrated'
    assert snapshot(archive,GID)['state']=='migrated'
    archive.registry.publish_discovery([{'id':'-1000000012345','kind':'supergroup','title':'Новая группа'}]);archive.registry.add('-1000000012345')
    assert source_status(archive,GID)['migrated_group_id']=='-1000000012345'
    archive.registry.action('-1000000012345','disable');assert not source_status(archive,GID)['migrated_group_id']
    remote.download_profile_photo.assert_not_awaited()


def test_access_loss_and_recovery_are_independent_from_bot_and_avatar(app,archive,config):
    remote=SimpleNamespace(get_entity=AsyncMock(return_value=SimpleNamespace(left=True)))
    sync=Synchronizer(config,archive,remote);asyncio.run(sync.sync_group_profile(GID))
    assert source_status(archive,GID)['state']=='access_lost'
    archive.save_group_profile(GID,checked_at=0)
    remote.get_entity.return_value=SimpleNamespace(title='Семья',photo=None,left=False)
    asyncio.run(sync.sync_group_profile(GID));archive.heartbeat('online')
    assert source_status(archive,GID)['state']=='working'
    archive.save_group_profile(GID,checked_at=0)
    remote.get_entity.side_effect=errors.ChannelPrivateError(None)
    import pytest
    with pytest.raises(errors.ChannelPrivateError):asyncio.run(sync.sync_group_profile(GID))
    assert source_status(archive,GID)['state']=='access_lost'


def test_pause_disabled_copy_and_snapshot_overrides(app,archive,config):
    archive.heartbeat('online');record(archive,GID,'working')
    config.data_dir.joinpath('.telegram-limits.json').write_text(json.dumps({'version':1,'pauses':{'all':{'kind':'flood','seconds':60,'until':time.time()+60}}}))
    assert source_status(archive,GID)['state']=='paused'
    copy=archive.registry.copy(GID,'connection-copy')
    assert source_status(archive,copy)['state']=='copy'
    archive.registry.action(copy,'disable');assert source_status(archive,copy)['state']=='copy' and source_status(archive,copy)['hidden']
    archive.registry.action(GID,'disable');assert source_status(archive,GID)['state']=='disabled'
    archive.registry.action(GID,'delete');assert source_status(archive,GID)['state']=='deleting'


def test_service_migration_event_never_becomes_user_message(app,archive,config):
    source=SimpleNamespace(action=types.MessageActionChatMigrateTo(12345))
    asyncio.run(Synchronizer(config,archive,SimpleNamespace()).apply(GID,source))
    assert source_status(archive,GID)['state']=='migrated' and not archive.history(GID)['items']
