"""Source health is independent of worker liveness, media progress and bot rights."""
import time
from sqlalchemy import select
from .db import runtime
from .limits import read_limits

FRESH_SECONDS=900


def record(archive,gid,state,*,migrated_to=None,stamp=None):
    registry=getattr(archive,'registry',None)
    if not registry:return
    now=int(time.time() if stamp is None else stamp)
    with registry.edit() as (_,data):
        control=data['groups'].get(gid)
        if not control:return
        old=control.get('connection',{})
        # A confirmed migration is permanent; reading the legacy history must
        # never turn this archive back into a live source.
        if old.get('state')=='migrated' and state!='migrated':return
        value=dict(state=state,checked_at=now,last_success=now if state=='working' else old.get('last_success',0))
        if migrated_to:value['migrated_to']=str(migrated_to)
        control['connection']=value


def source_health(archive):
    with archive.engine.connect() as c:worker=c.execute(select(runtime)).mappings().first()
    return worker,read_limits(archive.objects.config)


def source_status(archive,gid,group=None,controls=None,stamp=None,*,health=None):
    now=int(time.time() if stamp is None else stamp)
    group=group or archive.group(gid) or {}
    registry=getattr(archive,'registry',None)
    controls=controls if controls is not None else registry.read()['groups'] if registry else {}
    control=controls.get(gid,{})
    value=dict(control.get('connection',{}))
    value.setdefault('checked_at',0);value.setdefault('last_success',0)
    if control.get('delete_at') is not None or control.get('phase'):value['state']='deleting'
    elif control.get('copy_created') is not None:value.update(state='copy',hidden=control.get('enabled') is False)
    elif group.get('kind')=='export':value.update(state='export',hidden=control.get('enabled') is False)
    elif control.get('enabled') is False:value['state']='disabled'
    elif value.get('state') not in {'migrated','access_lost'}:
        worker,limits=source_health(archive) if health is None else health
        pause=limits.get('pauses',{}).get('all',{})
        if worker and worker['state']=='login_required':value['state']='login_required'
        elif not worker or worker['heartbeat']<now-120:value['state']='offline'
        elif limits.get('invalid'):value['state']='unavailable'
        elif pause.get('until',0)>now:value.update(state='paused',wait_until=pause['until'])
        elif worker['state'] not in {'online','rate_limited'}:value['state']='checking'
        elif not value.get('state') or value['checked_at']<now-FRESH_SECONDS:value['state']='checking'
    if value.get('migrated_to'):
        value['migrated_group_id']=next((target for target,c in controls.items() if c.get('telegram_id')==value['migrated_to'] and c.get('enabled') and c.get('delete_at') is None),None)
    return value
