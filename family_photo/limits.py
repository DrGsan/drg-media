"""One conservative RPC gate for this reader, including Telethon file chunks.

These are application limits, not a claim about Telegram's unpublished quotas.
Server deadlines always win, survive restarts, and are never retried early.
"""
import asyncio
import json
import logging
import os
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

from telethon import TelegramClient, errors, utils

RPC_INTERVAL = 2.0
HISTORY_INTERVAL = 10.0
MEDIA_INTERVAL = 5.0
CHUNK_SIZE = 512 * 1024
FLOOD_ERRORS = (errors.FloodWaitError, errors.FloodPremiumWaitError)
HISTORY_METHODS = {'GetHistoryRequest', 'GetMessagesRequest', 'SearchRequest'}
MEDIA_METHODS = {'GetFileRequest', 'GetCdnFileRequest', 'GetWebFileRequest', 'GetFileHashesRequest', 'GetCdnFileHashesRequest'}


def read_limits(config, strict=False):
    try:
        path = config.data_dir / '.telegram-limits.json'
        if not path.exists(): return {}
        if path.stat().st_size > 1024*1024: raise ValueError('oversized state')
        data = json.loads(path.read_text())
        if data.get('version') != 1 or not isinstance(data.get('pauses'), dict): raise ValueError('invalid state')
        if set(data['pauses'])-{'all','media'} or not isinstance(data.get('failures',{}),dict): raise ValueError('invalid scopes')
        for pause in data['pauses'].values():
            if not isinstance(pause,dict) or pause.get('kind') not in {'flood','premium'}: raise ValueError('invalid pause')
            if type(pause.get('seconds')) is not int or pause['seconds']<1: raise ValueError('invalid wait')
        for value in [data.get('next_rpc_at',0),data.get('next_history_at',0),data.get('next_media_at',0),
                      *[v.get('until') for v in data['pauses'].values()]]:
            if type(value) not in (int,float) or not 0 <= value < 10**12: raise ValueError('invalid deadline')
        return data
    except (OSError, ValueError, TypeError, AttributeError):
        if strict: raise RuntimeError('Telegram request limit state is unreadable; reader stays stopped') from None
        return {'invalid':True}


class RpcLimits:
    def __init__(self, config, clock=None, sleep=None):
        self.config, self.clock, self.sleep = config, clock or time.time, sleep or asyncio.sleep
        self.data = read_limits(config, strict=True) or dict(version=1,pauses={},failures={},next_rpc_at=0,next_history_at=0,next_media_at=0)
        # Learn a conservative file pacing floor from observed server waits.
        previous=max((p.get('seconds',0) for p in self.data['pauses'].values() if p.get('method') in MEDIA_METHODS),default=0)
        self.data['media_interval_seconds']=max(MEDIA_INTERVAL,min(60,previous+2),self.data.get('media_interval_seconds',0))
        # Older readers applied the extra file backoff to every RPC. Preserve
        # the actual server deadline globally, but keep our longer file delay
        # on transfers only, so live messages can continue afterwards.
        pause=self.data['pauses'].get('all',{})
        if pause.get('method') in MEDIA_METHODS and pause.get('kind')=='flood' and 'telegram_until' in pause:
            server_until=pause['telegram_until']+5
            if pause['until']>server_until:
                if self.data['pauses'].get('media',{}).get('until',0)<pause['until']:
                    self.data['pauses']['media']=pause|{'scope':'media'}
                self.data['pauses']['all']=pause|{'until':server_until}
        self.save()
        self.lock = asyncio.Lock()
        self.owner = None
        self.depth = 0
        self.waiters = []

    def save(self):
        self.config.data_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
        with tempfile.NamedTemporaryFile(mode='w',prefix='.telegram-limits-',dir=self.config.data_dir,delete=False) as output:
            path=Path(output.name)
            try:
                os.chmod(path,0o600);json.dump(self.data,output)
                output.flush();os.fsync(output.fileno())
                os.replace(path,self.config.data_dir/'.telegram-limits.json')
            finally:path.unlink(missing_ok=True)

    def delay(self, request):
        now=self.clock();name=type(request).__name__
        scopes=['all']+(['media'] if name in MEDIA_METHODS else [])
        due=max([self.data.get('next_rpc_at',0),
                 self.data.get('next_history_at',0) if name in HISTORY_METHODS else 0,
                 self.data.get('next_media_at',0) if name in MEDIA_METHODS else 0,
                 *[self.data['pauses'].get(scope,{}).get('until',0) for scope in scopes]])
        return max(0,due-now)

    @asynccontextmanager
    async def slot(self,request):
        # FIFO among currently eligible requests. Without tickets a periodic
        # history scan can repeatedly win the same deadline and starve a file.
        task=asyncio.current_task();nested=self.owner is task
        ticket=(object(),request)
        if not nested:self.waiters.append(ticket)
        entered=False
        try:
            while True:
                while delay:=self.delay(request):await self.sleep(min(delay,30))
                first=next((w for w in self.waiters if not self.delay(w[1])),None)
                if not nested and first is not ticket:
                    await self.sleep(.05)
                    continue
                if not nested:await self.lock.acquire()
                if not self.delay(request):break
                if not nested:self.lock.release()
            if not nested:
                self.waiters.remove(ticket)
                self.owner=task
            self.depth+=1
            entered=True
            name=type(request).__name__;now=self.clock()
            self.data['next_rpc_at']=now+RPC_INTERVAL
            if name in HISTORY_METHODS:self.data['next_history_at']=now+HISTORY_INTERVAL
            if name in MEDIA_METHODS:self.data['next_media_at']=now+self.data['media_interval_seconds']
            self.data['last_method']=name
            self.data['last_request_at']=now
            self.save()
            yield
        finally:
            if not nested and ticket in self.waiters:self.waiters.remove(ticket)
            if entered:
                self.depth-=1
                if not nested:self.owner=None;self.lock.release()

    def rejected(self,request,error):
        now=self.clock();key=str(request.CONSTRUCTOR_ID)
        count=self.data.setdefault('failures',{}).get(key,0)+1
        self.data['failures'][key]=min(count,20)
        premium=isinstance(error,errors.FloodPremiumWaitError)
        file_method=type(request).__name__ in MEDIA_METHODS
        scope='media' if premium or file_method else 'all'
        steps=(300,900,1800,3600,10800,21600) if premium else (60,300,900,1800,3600,10800,21600)
        seconds=max(1,int(error.seconds))
        until=max(now+seconds+5,now+steps[min(count-1,len(steps)-1)],self.data['pauses'].get(scope,{}).get('until',0))
        record=dict(kind='premium' if premium else 'flood',scope=scope,method=type(request).__name__,
                    code='FLOOD_PREMIUM_WAIT' if premium else 'FLOOD_WAIT',seconds=seconds,
                    telegram_until=now+seconds,until=until,observed_at=now,failures=count)
        self.data['pauses'][scope]=record
        if file_method and not premium:
            server_until=now+seconds+5
            if self.data['pauses'].get('all',{}).get('until',0)<=server_until:
                self.data['pauses']['all']=record|{'scope':'all','until':server_until}
        if type(request).__name__ in MEDIA_METHODS:
            self.data['media_interval_seconds']=max(self.data['media_interval_seconds'],min(60,seconds+2))
            self.data['next_media_at']=max(self.data.get('next_media_at',0),now+self.data['media_interval_seconds'])
        self.save()
        return record

    def succeeded(self,request):
        key=str(request.CONSTRUCTOR_ID)
        if self.data.get('failures',{}).get(key):
            self.data['failures'].pop(key,None);self.save()


class LimitedTelegramClient(TelegramClient):
    def __init__(self, config):
        self.rpc_limits=RpcLimits(config)
        super().__init__(str(config.data_dir/'telegram'),config.api_id,config.api_hash,
            catch_up=True,sequential_updates=True,flood_sleep_threshold=0,
            request_retries=0,raise_last_call_error=True,connection_retries=5,device_model='Media Server')

    async def _call(self,sender,request,ordered=False,flood_sleep_threshold=None):
        if utils.is_list_like(request):
            # A library batch must not bypass the per-RPC budget with one slot.
            return [await self._call(sender,r,ordered,flood_sleep_threshold) for r in request]
        # Resolve first: entity resolution may itself issue a metered RPC. The
        # parent must obtain its own slot after that request, not before it.
        await request.resolve(self,utils)
        for attempt in range(3):
            dc=self.session.dc_id
            async with self.rpc_limits.slot(request):
                try:
                    result=await super()._call(sender,request,ordered,flood_sleep_threshold)
                except FLOOD_ERRORS as error:
                    pause=self.rpc_limits.rejected(request,error)
                    logging.getLogger('family_photo.worker').warning('Telegram %s: %ss, next attempt in %ss (%s)',
                        pause['code'],pause['seconds'],round(pause['until']-self.rpc_limits.clock()),pause['method'])
                    raise
                except (ValueError,errors.PhoneMigrateError,errors.NetworkMigrateError,errors.UserMigrateError):
                    # With hidden retries disabled, Telethon may finish a
                    # successful DC switch without resending. Give the retry
                    # its own metered slot (important for first-time login).
                    if attempt<2 and self.session.dc_id!=dc:
                        sender=self._sender
                        continue
                    raise
                self.rpc_limits.succeeded(request)
                return result
