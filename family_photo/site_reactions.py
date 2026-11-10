"""One bot reaction mirrors eligible portal votes, through an idempotent queue.

State lives in the existing JSON registry table. No media or extra S3 objects.
The site periodically supplies complete snapshots, including empty removals.
"""
import hashlib
import json
import re
import time
from collections import Counter
import httpx
from sqlalchemy import select
from .db import group_management,messages,bot_access
from .site_messaging import ACL_KEY,MessagingError

PREFIX='react:'
EMOJIS=('👍','❤️','😂','🔥','👏','😢','😮','🎉','🤔','👀','✅','🙏','😁','😍','💯','🤝','🥰','💔','😎','😡','👎','👌','😱','🫡')

def key(gid,mid):return PREFIX+hashlib.sha256((gid+':'+str(mid)).encode()).hexdigest()[:32]

def winner(counts,current,allowed=None,rejected=()):
    choices={e:n for e,n in counts.items() if n>0 and e not in rejected and (allowed is None or e in allowed)}
    if not choices:return None
    leaders={e for e,n in choices.items() if n==max(choices.values())}
    return current if current in leaders else next(e for e in EMOJIS if e in leaders)

class BotReactions:
    def __init__(self,messaging):self.messaging=messaging;self.archive=messaging.archive

    @staticmethod
    def read(c,gid,mid):
        raw=c.execute(select(group_management.c.value).where(group_management.c.id==key(gid,mid))).scalar()
        return json.loads(raw) if raw else None

    @staticmethod
    def save(c,job):
        ident=key(job['group_id'],job['message_id'])
        c.execute(group_management.update().where(group_management.c.id==ident).values(value=json.dumps(job)))

    @staticmethod
    def public(job):return {k:job.get(k) for k in ('group_id','message_id','revision','state','emoji','error','limited')}

    def snapshots(self,data):
        if not isinstance(data,dict) or set(data)!={'revision','items'} or type(data['revision']) is not int or not 0<data['revision']<=2**53-1 or not isinstance(data['items'],list) or len(data['items'])>50:raise MessagingError('invalid_reactions',422)
        seen=set()
        for item in data['items']:
            if not isinstance(item,dict) or set(item)!={'group_id','message_id','votes'} or not isinstance(item['group_id'],str) or not re.fullmatch(r'-\d{1,20}|export:[a-f0-9]{8,24}',item['group_id']) or type(item['message_id']) is not int or not 0<item['message_id']<=2**53-1 or not isinstance(item['votes'],dict) or len(item['votes'])>500:raise MessagingError('invalid_reactions',422)
            pair=(item['group_id'],item['message_id'])
            if pair in seen:raise MessagingError('invalid_reactions',422)
            seen.add(pair)
            for uid,emojis in item['votes'].items():
                if not re.fullmatch('[a-f0-9]{32}',uid) or not isinstance(emojis,list) or not 1<=len(emojis)<=3 or len(set(emojis))!=len(emojis) or any(e not in EMOJIS for e in emojis):raise MessagingError('invalid_reactions',422)
        result=[]
        # Same registry lock as permission updates and group lifecycle mutations.
        with self.archive.registry.edit() as (c,registry):
            for item in data['items']:
                gid,mid=item['group_id'],item['message_id']
                row=c.execute(select(messages.c.deleted).where(messages.c.group_id==gid,messages.c.id==mid)).first()
                if not row or row[0]:
                    c.execute(group_management.delete().where(group_management.c.id==key(gid,mid)))
                    result.append(dict(group_id=gid,message_id=mid,revision=data['revision'],state='gone'));continue
                job=self.read(c,gid,mid)
                if job and job['revision']>data['revision']:result.append(self.public(job));continue
                if not job:
                    job=dict(group_id=gid,message_id=mid,revision=0,votes={},state='queued',emoji=None,applied=None,bot_id=None,retry_at=0,error=None,rejected=[])
                    c.execute(group_management.insert().values(id=key(gid,mid),value='{}'))
                if item['votes']!=job['votes']:
                    job.update(votes=item['votes'],state='queued',rejected=[],error=None)
                job['revision']=data['revision'];self.save(c,job);result.append(self.public(job))
                if not job['votes'] and job['state']=='delivered' and job.get('applied') is None:c.execute(group_management.delete().where(group_management.c.id==key(gid,mid)))
        return {'items':result}

    def counts(self,c,job):
        raw=c.execute(select(group_management.c.value).where(group_management.c.id==ACL_KEY)).scalar()
        acl=json.loads(raw) if raw else {}
        if acl.get('checked',0)<time.time()-120:return None
        counts=Counter()
        for uid,emojis in job['votes'].items():
            grants=acl.get('actors',{}).get(uid,[])
            if '*' in grants or job['group_id'] in grants:counts.update(emojis)
        return counts

    async def tick(self,http=None):
        # Called under the bot worker lease; Bot API set is safe to repeat after
        # an ambiguous response or crash, unlike sending a new text message.
        with self.archive.engine.connect() as c:
            ids=list(c.execute(select(group_management.c.id).where(group_management.c.id.like(PREFIX+'%')).order_by(group_management.c.id)).scalars())
        sent=0
        for ident in ids:
            with self.archive.registry.edit() as (c,registry):
                raw=c.execute(select(group_management.c.value).where(group_management.c.id==ident)).scalar()
                if not raw:continue
                job=json.loads(raw);gid,mid=job['group_id'],job['message_id']
                control=registry['groups'].get(gid)
                row=c.execute(select(messages.c.deleted).where(messages.c.group_id==gid,messages.c.id==mid)).first()
                if not control or control.get('phase') or not row or row[0]:
                    c.execute(group_management.delete().where(group_management.c.id==ident));continue
                if not control['enabled'] or control.get('delete_at') is not None or control.get('copy_created') is not None or control.get('connection',{}).get('state')=='migrated':continue
                if job.get('retry_at',0)>time.time():continue
                counts=self.counts(c,job)
                if counts is None:continue
                cap=self.messaging.capability(gid,c)
                if not cap.get('can_send'):continue
                bot=json.loads(c.execute(select(bot_access.c.value).where(bot_access.c.group_id==gid)).scalar())
                if job.get('bot_id') is not None and job['bot_id']!=bot['bot_id']:
                    job.update(applied=None,emoji=None,state='queued',uncertain=False,bot_id=bot['bot_id'])
                desired=winner(counts,job.get('applied'),cap.get('reaction_emojis'),job.get('rejected',[]))
                limited=bool(counts) and desired is None
                if job.get('state')=='delivered' and job.get('applied')==desired:
                    if job.get('limited')!=limited:job['limited']=limited;self.save(c,job)
                    continue
                job['limited']=limited
                # Retry ambiguous sends even if a newer snapshot now wants the
                # previous value; that request may already have reached Telegram.
                if job.get('state')!='sending' and job.get('applied')==desired and not job.get('error') and not job.get('uncertain'):
                    job.update(state='delivered',emoji=desired);self.save(c,job);continue
                job.update(state='sending',emoji=desired,bot_id=bot['bot_id'],uncertain=True);self.save(c,job)
            try:
                async with httpx.AsyncClient(timeout=15,follow_redirects=False) as default:
                    response=await (http or default).post('https://api.telegram.org/bot'+self.messaging.config.bot_token+'/setMessageReaction',json={'chat_id':self.messaging.config.telegram_id(gid),'message_id':mid,'reaction':[{'type':'emoji','emoji':desired}] if desired else []})
                data=response.json();code=response.status_code
                if code==429:self.messaging.pause(data.get('parameters',{}).get('retry_after',60))
                error=None if code==200 and data.get('ok') else 'telegram_unavailable' if code>=500 else 'telegram_rejected'
                description=data.get('description','').lower()
                if code==400 and any(text in description for text in ('reaction_not_modified','reaction not modified','message is not modified')):error=None
                if 'reaction_invalid' in description or 'reaction is invalid' in description:error='reaction_unavailable'
                if 'message to react not found' in description or 'message_id_invalid' in description:error='message_unavailable'
                if code==429:error='telegram_pause'
            except Exception:error='telegram_unavailable';data={};code=0
            with self.archive.registry.edit() as (c,registry):
                current=self.read(c,gid,mid)
                if not current:continue
                if not error:
                    current.update(applied=desired,uncertain=False,bot_id=job['bot_id'],state='delivered' if current['revision']==job['revision'] else 'queued',error=None,retry_at=0)
                else:
                    current.update(state='failed' if error=='message_unavailable' else 'queued',error=error,retry_at=int(time.time())+max(5,int(data.get('parameters',{}).get('retry_after',300 if code==400 else 30))))
                    if error=='reaction_unavailable' and desired:current['rejected']=sorted(set(current.get('rejected',[])+[desired]));current['retry_at']=0
                self.save(c,current)
            sent+=1
            if self.messaging.paused() or sent>=5:return sent
        return sent

    def decorate(self,c,row,result):
        job=self.read(c,row['group_id'],row['id'])
        if not job:return
        result['bot_reaction_sync']=self.public(job)
        # Only remove a positively identified mirrored bot vote. Other Telegram
        # users and incomplete author lists are never guessed or decremented.
        for reaction in result.get('reactions',[]):
            people=reaction.get('people',[])
            own=[p for p in people if str(p.get('id'))==str(job.get('bot_id'))]
            if own and reaction.get('emoji') in {job.get('applied'),job.get('emoji')}:
                reaction['people']=[p for p in people if p not in own]
                reaction['count']=max(0,reaction['count']-1)
        result['reactions']=[r for r in result.get('reactions',[]) if r.get('count',0)>0]
