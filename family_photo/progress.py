"""Disposable worker telemetry; SQLite/S3 remain the source of truth.

Only the worker writes this private atomic file. Restart keeps the current batch,
while API responses discard stale transfer speeds and filter by one group.
"""
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path

from sqlalchemy import select, func
from .db import media, runtime, garbage


def read_progress(config):
    try:
        path = config.data_dir / '.sync-progress.json'
        if path.stat().st_size > 4 * 1024 * 1024: return {}
        value = json.loads(path.read_text())
        return value if value.get('version') == 1 else {}
    except (OSError, ValueError, AttributeError):
        return {}


class Progress:
    def __init__(self, config, archive):
        self.config, self.archive = config, archive
        self.lock = threading.RLock()
        old = read_progress(config)
        self.data = dict(version=1, groups={gid: {'batch': value.get('batch', {})}
                         for gid, value in old.get('groups', {}).items()}, cleanup=old.get('cleanup', {}))
        self.current = None
        self.anchor = None
        self.saved = 0

    def save(self, force=False):
        with self.lock:
            registry = getattr(self.archive, 'registry', None)
            if registry:
                known = {gid: value for gid, value in registry.read()['groups'].items() if not value['phase']}
                self.data['groups'] = {gid: value for gid, value in self.data['groups'].items() if gid in known}
                if self.current not in known: self.current = None
            if not force and time.monotonic() - self.saved < 1: return
            self.data['heartbeat'] = time.time()
            self.config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.NamedTemporaryFile(mode='w', prefix='.sync-progress-', dir=self.config.data_dir, delete=False) as output:
                path = Path(output.name)
                try:
                    os.chmod(path, 0o600)
                    json.dump(self.data, output)
                    output.flush()
                    os.replace(path, self.config.data_dir / '.sync-progress.json')
                finally:
                    path.unlink(missing_ok=True)
            self.saved = time.monotonic()

    def begin(self, item):
        with self.lock:
            gid = item['group_id']
            with self.archive.engine.connect() as c:
                rows = [dict(r) for r in c.execute(select(media.c.id, media.c.identity, media.c.state)
                    .where(media.c.group_id == gid, media.c.state != 'paused')).mappings()]
            group = self.data['groups'].setdefault(gid, {})
            batch = group.get('batch', {})
            members = batch.get('members', {})
            if not any(r['state'] != 'ready' and members.get(r['id']) == r['identity'] for r in rows):
                batch = {'members': {}, 'started_at': time.time()}
            batch['members'].update({r['id']: r['identity'] for r in rows if r['state'] != 'ready'})
            group['batch'] = batch
            group['current'] = dict(id=item['id'], stage='metadata', bytes=0, total=item['size'], speed=0)
            self.current = gid
            self.anchor = None
            self.save(True)

    def stage(self, stage, total=0):
        with self.lock:
            if not self.current: return
            current = self.data['groups'][self.current]['current']
            current.update(stage=stage, bytes=0, total=total, speed=0, wait_until=0, updated_at=time.time())
            self.anchor = (time.monotonic(), 0)
            self.save(True)

    def transfer(self, size, total=None, delta=False):
        with self.lock:
            if not self.current: return
            current = self.data['groups'][self.current]['current']
            if total is not None: current['total'] = total or 0
            size = max(0, current['bytes'] + size if delta else size)
            now = time.monotonic()
            if self.anchor is None: self.anchor = (now, size)
            elapsed = now - self.anchor[0]
            speed = (size - self.anchor[1]) / elapsed if elapsed >= .5 else 0
            current.update(bytes=size, speed=max(0, speed), updated_at=time.time())
            # Bound the averaging window so a resumed connection adapts quickly.
            if elapsed > 10: self.anchor = (now, size)
            self.save()

    def wait(self, seconds):
        with self.lock:
            if not self.current: return
            current = self.data['groups'][self.current]['current']
            current.update(wait_until=time.time() + seconds + 1, speed=0)
            self.anchor = None
            self.save(True)

    def resume(self):
        with self.lock:
            if not self.current: return
            current = self.data['groups'][self.current]['current']
            current.update(wait_until=0, speed=0, updated_at=time.time())
            self.anchor = (time.monotonic(), current['bytes'])
            self.save(True)

    def finish(self):
        with self.lock:
            if self.current in self.data['groups']: self.data['groups'][self.current].pop('current', None)
            self.current = None
            self.anchor = None
            self.save(True)

    def history(self, gid, wait_until=0):
        with self.lock:
            self.data['groups'].setdefault(gid, {})['history_wait_until'] = wait_until
            self.save(True)

    def cleaned(self, result):
        with self.lock:
            cleanup = self.data.setdefault('cleanup', {})
            for field in ('objects_removed', 'temporary_removed', 'temporary_bytes', 'references_preserved'):
                cleanup[field] = cleanup.get(field, 0) + result.get(field, 0)
            cleanup.update(checked_at=time.time(), errors=result.get('errors', 0))
            self.save(True)


def snapshot(archive, gid):
    now = time.time()
    data = read_progress(archive.objects.config)
    group = archive.group(gid)
    info = data.get('groups', {}).get(gid, {})
    with archive.engine.connect() as c:
        rows = [dict(r) for r in c.execute(select(media.c.id, media.c.identity, media.c.kind, media.c.name,
            media.c.size, media.c.state, media.c.retry_at, media.c.error, media.c.message_id, media.c.created).where(media.c.group_id == gid)).mappings()]
        worker = c.execute(select(runtime)).mappings().first()
        cleanup_pending = c.execute(select(func.count()).select_from(garbage).where(garbage.c.created <= int(now)-300)).scalar_one()
    online = bool(worker and worker['heartbeat'] > now-90 and worker['state'] in {'online', 'rate_limited', 'connecting'})
    fresh = online and data.get('heartbeat', 0) > now-90
    stopped = sorted((r for r in rows if r['state'] == 'paused'),key=lambda r:(r['created'],r['message_id']),reverse=True)
    telegram_id=archive.objects.config.telegram_id(gid)
    channel=re.fullmatch(r'-100([1-9]\d*)',telegram_id) if group['kind']=='supergroup' else None
    def source(row):
        return dict(created=row['created'],message_id=row['message_id'],group_title=group['title'],
                    telegram_url=f'https://t.me/c/{channel[1]}/{row["message_id"]}' if channel else None)
    pending = [r for r in rows if r['state'] not in {'ready','paused'}]
    current = info.get('current') if fresh else None
    active = next((r for r in rows if current and r['id'] == current.get('id')), None)
    if active and active['state'] == 'paused': active = None
    # A WEBM original may be committed before its preview; it is still in work.
    if active and active['state'] == 'ready': pending.append(active)
    members = info.get('batch', {}).get('members', {})
    if pending and not any(members.get(r['id']) == r['identity'] for r in pending): members = {}
    members = members | {r['id']: r['identity'] for r in pending}
    batch = [r for r in rows if r['state'] != 'paused' and members.get(r['id']) == r['identity']]
    complete = sum(r['state'] == 'ready' and r is not active for r in batch)
    public = None
    eta = None
    if active:
        stage = current.get('stage', 'metadata')
        size, total = max(0, current.get('bytes', 0)), max(0, current.get('total', 0))
        # A transfer with no callback for 15 seconds no longer has a credible rate.
        speed = current.get('speed', 0) if current.get('updated_at', 0) > now-15 else 0
        wait_until = current.get('wait_until', 0)
        if wait_until > now: speed = 0
        if stage in {'download','restoring'} and total: active['size'] = total
        public = dict(id=active['id'], kind=active['kind'], name=active['name'][:255], stage=stage,
                      bytes=size, total=total, speed=round(speed), wait_until=wait_until,**source(active))
        if stage == 'download' and speed > 0 and wait_until <= now and all(r['size'] > 0 for r in pending):
            remaining = sum(r['size'] for r in pending if r['id'] != active['id']) + max(0, total-size)
            if total: eta = round(remaining/speed)
    from .limits import read_limits, RPC_INTERVAL, HISTORY_INTERVAL, MEDIA_INTERVAL
    limits = read_limits(archive.objects.config)
    pauses = limits.get('pauses', {})
    candidates = [p for scope,p in pauses.items() if p.get('until',0)>now and (scope=='all' or pending)]
    pause = max(candidates,key=lambda p:p['until']) if candidates else None
    if limits.get('invalid'): pause = dict(kind='invalid',until=0)
    if pause and public and public['stage'] in {'metadata','download'}:
        public.update(speed=0,wait_until=max(public['wait_until'],pause['until']))
        eta=None
    retries = [r['retry_at'] for r in pending if r['retry_at'] > now]
    state = ('login_required' if worker and worker['state']=='login_required' else
             'idle' if group['kind']=='export' and group['history_complete'] and not pending else
             'offline' if not online else 'paused' if pause else 'unavailable' if group['state'] in {'unavailable', 'access_lost'} else
             'active' if active else 'retry' if pending and len(retries) == len(pending) else
             'queued' if pending else 'scanning' if not group['history_complete'] else 'idle')
    from .connection import source_status
    connection=source_status(archive,gid,group)
    if connection['state']=='migrated':state='migrated'
    elif connection['state']=='access_lost':state='access_lost'
    return dict(connection=connection,state=state, server_time=now, pending=len(pending), failed=sum(r['state']=='failed' for r in pending),
                stopped=len(stopped), stopped_files=[dict(name=r['name'],**source(r)) for r in stopped[:20]],
                rate_limited=sum(r['error'] in {'TelegramRateLimit','FloodWaitError','FloodPremiumWaitError'} for r in pending),
                pause=pause, policy=dict(rpc_interval_seconds=RPC_INTERVAL,history_interval_seconds=HISTORY_INTERVAL,media_interval_seconds=limits.get('media_interval_seconds',MEDIA_INTERVAL)),
                retry_at=min(retries) if retries else 0, current=public,
                batch=dict(total=len(batch), completed=complete,
                           percent=round(100*complete/len(batch), 1) if batch else None),
                eta_seconds=eta, history_wait_until=info.get('history_wait_until',0) if fresh else 0, history_complete=group['history_complete'], last_sync=group['last_sync'],
                cleanup=data.get('cleanup', {}) | {'pending': cleanup_pending})


def remove_group(root, gid):
    path = root / '.sync-progress.json'
    try: value = json.loads(path.read_text())
    except (OSError, ValueError): return
    value.get('groups', {}).pop(gid, None)
    with tempfile.NamedTemporaryFile(mode='w', prefix='.sync-progress-', dir=root, delete=False) as output:
        temporary = Path(output.name)
        try:
            os.chmod(temporary, 0o600)
            json.dump(value, output)
            output.flush()
            os.replace(temporary, path)
        finally: temporary.unlink(missing_ok=True)
