import asyncio
import json
import time
import httpx
import pytest
from sqlalchemy import select
from conftest import message
from test_site_messaging import messaging,ACTOR,OTHER,GID
from family_photo.site_reactions import BotReactions,winner,key
from family_photo.db import group_management,bot_access
from family_photo.group_management import PERIOD

THIRD='c'*32

def queue(value,archive,votes,revision=1,mid=1):
    if not archive.history(GID)['items']:archive.ingest(GID,message(mid))
    return BotReactions(value).snapshots({'revision':revision,'items':[{'group_id':GID,'message_id':mid,'votes':votes}]})['items'][0]

def read(value,archive):
    with archive.engine.connect() as c:return BotReactions(value).read(c,GID,1)

def calls(bot):return [kw['json']['reaction'] for method,kw in bot.calls if method=='setMessageReaction']

def test_month_old_votes_change_majority_keep_ties_and_clear_without_age_limit(messaging,archive):
    value,bot=messaging
    archive.ingest(GID,message(1,created=int(time.time())-40*86400))
    value.sync_permissions({'revision':2,'actors':{ACTOR:[GID],OTHER:[GID],THIRD:[GID]}})
    queue(value,archive,{ACTOR:['👍']});asyncio.run(value.tick(bot));assert calls(bot)==[[{'type':'emoji','emoji':'👍'}]]
    queue(value,archive,{ACTOR:['👍'],OTHER:['❤️']},2);asyncio.run(value.tick(bot));assert len(calls(bot))==1
    queue(value,archive,{ACTOR:['👍'],OTHER:['❤️'],THIRD:['❤️']},3);asyncio.run(value.tick(bot));assert calls(bot)[-1]==[{'type':'emoji','emoji':'❤️'}]
    queue(value,archive,{},4);asyncio.run(value.tick(bot));assert calls(bot)[-1]==[]
    asyncio.run(value.tick(bot));assert len(calls(bot))==3
    assert queue(value,archive,{},5)['state']=='delivered' and read(value,archive) is None

def test_allowed_reactions_stale_snapshots_and_rejected_emoji_fallback(messaging,archive):
    value,bot=messaging
    assert winner({'👍':1,'❤️':3},'❤️',['👍'])=='👍'
    assert winner({'👍':2,'❤️':2},'🔥')=='👍'
    assert winner({'❤️':2},None,[]) is None
    queue(value,archive,{ACTOR:['🔥']},9)
    queue(value,archive,{ACTOR:['👍']},8)
    assert read(value,archive)['votes']=={ACTOR:['🔥']}
    original=bot.post
    async def rejecting(url,**kwargs):
        if url.endswith('/setMessageReaction') and kwargs['json']['reaction']==[{'type':'emoji','emoji':'🔥'}]:
            return httpx.Response(400,json={'ok':False,'description':'Bad Request: REACTION_INVALID'})
        return await original(url,**kwargs)
    bot.post=rejecting
    queue(value,archive,{ACTOR:['🔥','👍']},10);asyncio.run(value.tick(bot));asyncio.run(value.tick(bot))
    assert calls(bot)[-1]==[{'type':'emoji','emoji':'👍'}]

def test_ambiguous_retry_and_newer_empty_snapshot_still_clear_remote(messaging,archive):
    value,bot=messaging
    queue(value,archive,{ACTOR:['👍']})
    bot.failure=httpx.ReadTimeout('lost response');asyncio.run(value.tick(bot))
    assert read(value,archive)['error']=='telegram_unavailable'
    queue(value,archive,{},2)
    with archive.transaction() as c:
        job=read(value,archive);job['retry_at']=0;BotReactions.save(c,job)
    bot.failure=None;asyncio.run(value.tick(bot))
    assert calls(bot)[-1]==[] and read(value,archive)['state']=='delivered'

def test_flood_wait_persists_and_permission_revocation_removes_eligible_vote(messaging,archive):
    value,bot=messaging;queue(value,archive,{ACTOR:['👍']})
    original=bot.post
    async def limited(url,**kwargs):
        if url.endswith('/setMessageReaction'):return httpx.Response(429,json={'ok':False,'parameters':{'retry_after':60}})
        return await original(url,**kwargs)
    bot.post=limited;asyncio.run(value.tick(bot));assert value.paused()
    job=read(value,archive);assert job['retry_at']>=time.time()+58 and job['error']=='telegram_pause'
    bot.post=original
    with archive.transaction() as c:
        c.execute(group_management.delete().where(group_management.c.id=='site-bot-pause'))
        job['retry_at']=0;BotReactions.save(c,job)
    asyncio.run(value.tick(bot));assert calls(bot)[-1]==[{'type':'emoji','emoji':'👍'}]
    value.sync_permissions({'revision':3,'actors':{}});asyncio.run(value.tick(bot));assert calls(bot)[-1]==[]

def test_disabled_copy_and_stale_acl_pause_work_and_deletion_cleans_queue(messaging,archive):
    value,bot=messaging;queue(value,archive,{ACTOR:['👍']})
    archive.registry.action(GID,'disable');asyncio.run(value.tick(bot));assert not calls(bot)
    archive.registry.action(GID,'enable')
    with archive.transaction() as c:
        raw=c.execute(select(group_management.c.value).where(group_management.c.id=='site-send-permissions')).scalar()
        acl=json.loads(raw);acl['checked']=0;c.execute(group_management.update().where(group_management.c.id=='site-send-permissions').values(value=json.dumps(acl)))
    asyncio.run(value.tick(bot));assert not calls(bot)
    archive.delete_messages(GID,[1]);asyncio.run(value.tick(bot));assert read(value,archive) is None
    assert queue(value,archive,{ACTOR:['👍']},3)['state']=='gone'

def test_serialization_removes_only_verified_mirror_not_real_telegram_voters(messaging,archive):
    value,bot=messaging;queue(value,archive,{ACTOR:['👍']});asyncio.run(value.tick(bot))
    archive.ingest(GID,message(1,reactions=[{'emoji':'👍','count':3,'people':[{'id':'77'},{'id':'42'},{'id':'43'}],'complete':True},{'emoji':'❤️','count':2,'people':[],'complete':False}]))
    reactions=archive.history(GID)['items'][0]['reactions']
    assert reactions[0]['count']==2 and {p['id'] for p in reactions[0]['people']}=={'42','43'}
    assert reactions[1]['count']==2

def test_snapshot_api_validation_and_group_purge(messaging,archive,client):
    value,bot=messaging;queue(value,archive,{ACTOR:['👍']})
    assert client.put('/v1/site-reactions',json={'revision':2,'items':[{'group_id':GID,'message_id':True,'votes':{}}]}).status_code==422
    assert client.put('/v1/site-reactions',json={'revision':2,'items':[{'group_id':GID,'message_id':1,'votes':{'forged':['👍']}}]}).status_code==422
    archive.registry.action(GID,'delete',stamp=0);archive.registry.maintenance(stamp=PERIOD)
    assert read(value,archive) is None
