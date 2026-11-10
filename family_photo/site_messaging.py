"""Durable portal-owned bot sends. Attribution is a confirmed ID binding."""
import hashlib
import json
import mimetypes
import re
import time
import uuid
from contextlib import ExitStack
from pathlib import Path

import httpx
from sqlalchemy import select,and_,or_,func
from .db import bot_access,site_sends,site_uploads,site_parts,messages,media,group_management,groups,garbage

MAX_FILE=50*1024*1024
EDIT_SECONDS=86400
ACL_KEY='site-send-permissions'
PAUSE_KEY='site-bot-pause'


class MessagingError(ValueError):
    def __init__(self,reason,status=409):self.reason,self.status=reason,status;super().__init__(reason)


def render_message(text,author):
    rendered=author+':\n'+text if text else author
    return rendered,[{'type':'italic','offset':0,'length':len(author.encode('utf-16-le'))//2}]


class SiteMessaging:
    def __init__(self,archive,config):
        self.archive,self.config=archive,config
        archive.site_messaging=self
        from .bot_profile import BotProfile
        self.profile=BotProfile(self)

    def sync_permissions(self,data):
        if (not isinstance(data,dict) or set(data)!={'revision','actors'} or type(data['revision']) is not int
            or not isinstance(data['actors'],dict) or len(data['actors'])>10000):raise MessagingError('invalid_permissions',422)
        for actor,gids in data['actors'].items():
            if not re.fullmatch('[a-f0-9]{32}',actor) or not isinstance(gids,list) or len(gids)>100 or any(not isinstance(gid,str) or not re.fullmatch(r'\*|-\d{1,20}|export:[a-f0-9]{8,24}',gid) for gid in gids):raise MessagingError('invalid_permissions',422)
        with self.archive.transaction() as c:
            old=c.execute(select(group_management.c.value).where(group_management.c.id==ACL_KEY).with_for_update()).scalar()
            if old and json.loads(old)['revision']>data['revision']:return
            value=json.dumps(data|{'checked':int(time.time())})
            if old:c.execute(group_management.update().where(group_management.c.id==ACL_KEY).values(value=value))
            else:c.execute(group_management.insert().values(id=ACL_KEY,value=value))
        return data['revision']

    def permitted(self,c,gid,actor):
        raw=c.execute(select(group_management.c.value).where(group_management.c.id==ACL_KEY)).scalar()
        if not raw:return False
        acl=json.loads(raw);grants=acl['actors'].get(actor,[])
        return acl['checked']>time.time()-120 and (gid in grants or '*' in grants)

    def paused(self):
        with self.archive.engine.connect() as c:raw=c.execute(select(group_management.c.value).where(group_management.c.id==PAUSE_KEY)).scalar()
        return json.loads(raw).get('until',0)>time.time() if raw else False

    def pause(self,seconds):
        with self.archive.transaction() as c:
            old=c.execute(select(group_management.c.value).where(group_management.c.id==PAUSE_KEY).with_for_update()).scalar()
            until=max(int(time.time())+max(1,int(seconds)),json.loads(old).get('until',0) if old else 0)
            value=json.dumps({'until':until})
            if old:c.execute(group_management.update().where(group_management.c.id==PAUSE_KEY).values(value=value))
            else:c.execute(group_management.insert().values(id=PAUSE_KEY,value=value))

    def capability(self,gid,c=None):
        if not self.config.bot_token:return {'can_send':False,'bot_reason':'not_configured'}
        if c is None:
            with self.archive.engine.connect() as connection:return self.capability(gid,connection)
        group=c.execute(select(groups.c.kind).where(groups.c.id==gid)).mappings().first()
        if not group or group['kind']!='supergroup':return {'can_send':False,'bot_reason':'unsupported_group'}
        raw=c.execute(select(bot_access.c.value).where(bot_access.c.group_id==gid)).scalar()
        return self._capability(group['kind'],raw,time.time())

    def capabilities(self, catalog):
        """One permission snapshot for a group-list response, never an ACL cache."""
        if not self.config.bot_token:
            return {g['id']:{'can_send':False,'bot_reason':'not_configured'} for g in catalog}
        with self.archive.engine.connect() as c:
            values=dict(c.execute(select(bot_access.c.group_id,bot_access.c.value)).all())
        stamp=time.time()
        return {g['id']:self._capability(g['kind'],values.get(g['id']),stamp) for g in catalog}

    def _capability(self,kind,raw,stamp):
        if kind!='supergroup':return {'can_send':False,'bot_reason':'unsupported_group'}
        value=json.loads(raw) if raw else {}
        if value.get('token')!=hashlib.sha256(self.config.bot_token.encode()).hexdigest() or value.get('checked',0)<stamp-90:
            return {'can_send':False,'bot_reason':'checking'}
        return {k:v for k,v in value.items() if k not in {'token','bot_id','checked'}}|{'bot_checked_at':value.get('checked',0)}

    async def check_access(self,gid,http=None):
        group=self.archive.group(gid)
        if not self.config.bot_token or not group or group['kind']!='supergroup':return
        with self.archive.engine.connect() as c:raw=c.execute(select(bot_access.c.value).where(bot_access.c.group_id==gid)).scalar()
        if raw:
            cached=json.loads(raw)
            if cached.get('token')==hashlib.sha256(self.config.bot_token.encode()).hexdigest() and cached.get('checked',0)>time.time()-45 and 'can_send_video_notes' in cached:return
        value={'checked':int(time.time()),'token':hashlib.sha256(self.config.bot_token.encode()).hexdigest(),'can_send':False,'bot_reason':'unavailable'}
        try:
            async with httpx.AsyncClient(timeout=10,follow_redirects=False) as default:
                client=http or default
                async def call(method,body):
                    response=await client.post(f'https://api.telegram.org/bot{self.config.bot_token}/'+method,json=body)
                    data=response.json()
                    if response.status_code==429:self.pause(data.get('parameters',{}).get('retry_after',60))
                    if response.status_code!=200 or not data.get('ok'):
                        reason='unavailable'
                        description=data.get('description','').lower()
                        if response.status_code==429:reason='paused'
                        elif 'chat not found' in description or 'not a member' in description or 'kicked' in description:reason='not_member'
                        elif response.status_code==401:reason='invalid_token'
                        raise MessagingError(reason)
                    return data['result']
                me=await call('getMe',{})
                member=await call('getChatMember',{'chat_id':self.config.telegram_id(gid),'user_id':me['id']})
                status=member['status'];value['bot_id']=me['id']
                if status in {'left','kicked'} or status=='restricted' and not member.get('is_member'):
                    value['bot_reason']='not_member'
                else:
                    chat=await call('getChat',{'chat_id':self.config.telegram_id(gid)})
                    value['reaction_emojis']=[r['emoji'] for r in chat['available_reactions'] if r.get('type')=='emoji'] if isinstance(chat.get('available_reactions'),list) else None
                    permissions=member if status=='restricted' else {} if status in {'administrator','creator'} else chat.get('permissions',{})
                    value.update(can_send=permissions.get('can_send_messages',status in {'administrator','creator'}),can_send_photos=permissions.get('can_send_photos',status in {'administrator','creator'}),
                        can_send_video_notes=permissions.get('can_send_video_notes',status in {'administrator','creator'}),can_send_voice_notes=permissions.get('can_send_voice_notes',status in {'administrator','creator'}),can_send_animations=permissions.get('can_send_other_messages',status in {'administrator','creator'}),can_send_documents=permissions.get('can_send_documents',status in {'administrator','creator'}),can_send_videos=permissions.get('can_send_videos',status in {'administrator','creator'}),bot_reason=None)
                    if not value['can_send']:value['bot_reason']='restricted'
        except MessagingError as error:
            value['bot_reason']=error.reason
        except Exception:pass
        with self.archive.transaction() as c:
            old=c.execute(select(bot_access.c.group_id).where(bot_access.c.group_id==gid)).first()
            if old:c.execute(bot_access.update().where(bot_access.c.group_id==gid).values(value=json.dumps(value)))
            else:c.execute(bot_access.insert().values(group_id=gid,value=json.dumps(value)))

    def require_send(self,gid,actor,c=None):
        if c is None:
            with self.archive.engine.connect() as connection:return self.require_send(gid,actor,connection)
        self.archive.require_active(c,gid)
        if not self.permitted(c,gid,actor):raise MessagingError('send_not_allowed',403)
        if not self.capability(gid,c).get('can_send'):raise MessagingError('read_only',403)

    @staticmethod
    def file(row):
        value=json.loads(row['value'])
        return {k:value[k] for k in ('name','mime','kind','size','duration','waveform') if k in value}|{'id':row['id'],'state':'ready','url':f"/v1/groups/{row['group_id']}/site-uploads/{row['id']}",
            'has_thumbnail':bool(row['thumb_key']),'site_upload':True}

    def upload(self,gid,actor,fid,source,name,mode,recording=None):
        if not re.fullmatch('[a-f0-9]{32}',fid):raise MessagingError('invalid_upload',422)
        with self.archive.objects.lease('siteupload/'+fid):return self._upload(gid,actor,fid,source,name,mode,recording)

    def _upload(self,gid,actor,fid,source,name,mode,recording=None):
        self.require_send(gid,actor)
        from .recordings import parse,verified
        recording=parse(recording)
        if recording and mode!='preview':raise MessagingError('invalid_recording',422)
        if not re.fullmatch('[a-f0-9]{32}',fid) or mode not in {'preview','file'}:raise MessagingError('invalid_upload',422)
        name=Path(name.replace('\\','/')).name.strip()[:255]
        if not name or any(ord(ch)<32 for ch in name):raise MessagingError('invalid_filename',422)
        with self.archive.objects.workspace() as root:
            path=Path(root)/'original';digest=hashlib.sha256();size=0
            with path.open('wb') as output:
                while chunk:=source.read(1024*1024):
                    size+=len(chunk)
                    if size>MAX_FILE:raise MessagingError('file_too_large',413)
                    output.write(chunk);digest.update(chunk)
            if not size:raise MessagingError('empty_file',422)
            mime='application/octet-stream';kind='file';recording_value={}
            if mode=='preview':
                from PIL import Image
                try:
                    with Image.open(path) as image:
                        image.verify();mime=Image.MIME.get(image.format,'application/octet-stream')
                    if mime not in {'image/jpeg','image/png','image/webp','image/gif'}:raise ValueError()
                    kind='animation' if mime=='image/gif' else 'photo'
                except Exception:
                    # Reuse the existing bounded ffprobe validation for audio-only recordings.
                    import subprocess
                    try:
                        result=subprocess.run(['ffprobe','-v','error','-show_entries','stream=codec_type,codec_name,width,height:format=format_name,duration','-of','json',str(path)],capture_output=True,timeout=15,check=True)
                        probe=json.loads(result.stdout);streams=probe.get('streams',[]);formats=set(probe.get('format',{}).get('format_name','').split(','))
                        if streams and all(stream.get('codec_type')=='audio' for stream in streams):
                            if 'ogg' in formats and all(stream.get('codec_name')=='opus' for stream in streams):mime='audio/ogg'
                            elif 'webm' in formats and all(stream.get('codec_name')=='opus' for stream in streams):mime='audio/webm'
                            elif formats & {'mov','mp4','m4a'} and all(stream.get('codec_name')=='aac' for stream in streams):mime='audio/mp4'
                            else:raise ValueError()
                            kind='voice'
                        else:
                            if not any(stream.get('codec_type')=='video' for stream in streams):raise ValueError()
                            with path.open('rb') as header:
                                if header.read(12)[4:8]!=b'ftyp':
                                    if not(recording and recording['kind']=='video_note' and 'webm' in formats):raise ValueError()
                            mime='video/webm' if 'webm' in formats else 'video/mp4';kind='video'
                        if recording:recording_value=verified(recording,probe);kind=recording_value['kind']
                    except MessagingError:raise
                    except Exception:raise MessagingError('invalid_media',422)
                if recording and not recording_value:raise MessagingError('invalid_recording',422)
                if kind=='video_note' and (mime!='video/mp4' or any(stream.get('codec_name') not in {'h264','aac'} for stream in streams)):
                    converted=Path(root)/'recording.mp4'
                    subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(path),'-map_metadata','-1','-c:v','libx264','-preset','veryfast','-b:v','1000k','-pix_fmt','yuv420p','-c:a','aac','-b:a','64k','-movflags','+faststart',str(converted)],capture_output=True,timeout=120,check=True)
                    path=converted;mime='video/mp4';name=Path(name).stem+'.mp4';size=path.stat().st_size
                    if size>MAX_FILE:raise MessagingError('file_too_large',413)
                if kind=='photo' and size>10*1024*1024:raise MessagingError('photo_too_large',413)
            capability=self.capability(gid)
            if not capability.get('can_send_'+{'photo':'photos','video':'videos','animation':'animations','file':'documents','voice':'voice_notes','video_note':'video_notes'}[kind],False):raise MessagingError('attachment_not_allowed',403)
            value={'name':name,'kind':kind,'mime':mime,'size':size,'sha256':digest.hexdigest(),**recording_value}
            with self.archive.engine.connect() as c:old=c.execute(select(site_uploads).where(site_uploads.c.id==fid)).mappings().first()
            if old:
                if old['group_id']!=gid or old['actor_id']!=actor or json.loads(old['value'])!=value:raise MessagingError('upload_conflict')
                return self.file(old)
            with ExitStack() as stack:
                key=stack.enter_context(self.archive.upload('sitefiles'));thumb=None
                self.archive.objects.put_file(key,path,mime)
                if kind in {'photo','video','video_note','animation'}:
                    from .telegram import make_thumbnail
                    thumbnail=Path(root)/'preview.jpg'
                    make_thumbnail(path,thumbnail,kind)
                    thumb=stack.enter_context(self.archive.upload('thumbnails'))
                    self.archive.objects.put_file(thumb,thumbnail,'image/jpeg')
                with self.archive.transaction() as c:
                    self.require_send(gid,actor,c)
                    drafts=[json.loads(raw) for raw in c.execute(select(site_uploads.c.value).where(site_uploads.c.actor_id==actor,site_uploads.c.send_id.is_(None))).scalars()]
                    if len(drafts)>=20 or sum(item['size'] for item in drafts)+size>500*1024*1024:raise MessagingError('draft_limit',429)
                    c.execute(site_uploads.insert().values(id=fid,group_id=gid,actor_id=actor,send_id=None,object_key=key,thumb_key=thumb,value=json.dumps(value),created=int(time.time())))
                    c.execute(garbage.delete().where(garbage.c.key.in_([key,thumb])))
            with self.archive.engine.connect() as c:return self.file(c.execute(select(site_uploads).where(site_uploads.c.id==fid)).mappings().one())

    def discard_upload(self,gid,actor,fid):
        with self.archive.transaction() as c:
            row=c.execute(select(site_uploads).where(site_uploads.c.id==fid,site_uploads.c.group_id==gid,site_uploads.c.actor_id==actor)).mappings().first()
            if not row:return
            if row['send_id']:raise MessagingError('upload_in_use')
            self.archive.gc(c,row['object_key']);self.archive.gc(c,row['thumb_key']);c.execute(site_uploads.delete().where(site_uploads.c.id==fid))

    def get(self,jid,c=None):
        if c is None:
            with self.archive.engine.connect() as connection:return self.get(jid,connection)
        row=c.execute(select(site_sends).where(site_sends.c.id==jid)).mappings().first()
        if not row:raise MessagingError('message_not_found',404)
        return dict(row)|json.loads(row['value'])

    @staticmethod
    def save(c,job):
        columns=set(site_sends.c.keys())
        c.execute(site_sends.update().where(site_sends.c.id==job['id']).values(content_key=job['content_key'],pending_key=job.get('pending_key'),state=job['state'],created=job['created'],retry_at=job.get('retry_at',0),value=json.dumps({k:v for k,v in job.items() if k not in columns})))

    def public(self,job,c=None):
        if c is None:
            with self.archive.engine.connect() as connection:return self.public(job,connection)
        value=self.archive.objects.read_json(job['content_key'])
        files=[self.file(row) for row in c.execute(select(site_uploads).where(site_uploads.c.send_id==job['id']).order_by(site_uploads.c.created,site_uploads.c.id)).mappings()]
        order=job.get('attachments',[]);files.sort(key=lambda file:order.index(file['id']))
        return {k:job.get(k) for k in ('id','actor_id','author','state','created','edited','revision','error','reply_to')}|{
            'text':value['text'],'media':files,'message_ids':job.get('message_ids',[]),'editable_until':job.get('sent_at',job['created'])+EDIT_SECONDS}

    def create(self,gid,actor,author,client_id,text,attachments,reply_to=None):
        self.require_send(gid,actor)
        if (not isinstance(text,str) or len(text)>3500 or not isinstance(attachments,list) or len(attachments)>10
            or any(not isinstance(fid,str) or not re.fullmatch('[a-f0-9]{32}',fid) for fid in attachments)
            or len(set(attachments))!=len(attachments) or not(text.strip() or attachments)):raise MessagingError('invalid_message',422)
        if not isinstance(author,str) or not author.strip() or len(author)>100 or any(ord(ch)<32 for ch in author):raise MessagingError('invalid_author',422)
        if not isinstance(client_id,str) or not re.fullmatch('[a-zA-Z0-9_-]{1,80}',client_id):raise MessagingError('invalid_client_id',422)
        fingerprint=hashlib.sha256(json.dumps([text,attachments,reply_to]).encode()).hexdigest()
        with self.archive.objects.lease('sitequeue/'+hashlib.sha256((gid+actor+client_id).encode()).hexdigest()[:32]):
            with ExitStack() as stack:
                with self.archive.engine.connect() as c:
                    old=c.execute(select(site_sends.c.id).where(site_sends.c.group_id==gid,site_sends.c.actor_id==actor,site_sends.c.client_id==client_id)).scalar()
                    if old:
                        job=self.get(old,c)
                        if job['request_fingerprint']!=fingerprint:raise MessagingError('client_conflict')
                        return self.public(job,c)
                key=stack.enter_context(self.archive.upload('sitecontent'));self.archive.objects.put_json(key,{'text':text})
                with self.archive.transaction() as c:
                    self.require_send(gid,actor,c)
                    recording_header=False
                    for fid in attachments:
                        row=c.execute(select(site_uploads).where(site_uploads.c.id==fid,site_uploads.c.group_id==gid,site_uploads.c.actor_id==actor,site_uploads.c.send_id.is_(None))).mappings().first()
                        if not row or row['created']<time.time()-86400:raise MessagingError('attachment_unavailable',422)
                        kind=json.loads(row['value'])['kind'];recording_header=recording_header or kind=='video_note'
                        if not self.capability(gid,c).get('can_send_'+{'photo':'photos','video':'videos','animation':'animations','file':'documents','voice':'voice_notes','video_note':'video_notes'}[kind],False):raise MessagingError('attachment_not_allowed',403)
                    caption=bool(attachments) and not recording_header
                    if len(render_message(text,author)[0].encode('utf-16-le'))//2>(1024 if caption else 4096):raise MessagingError('caption_too_long' if caption else 'text_too_long',422)
                    if reply_to is not None and (type(reply_to) is not int or not c.execute(select(messages.c.id).where(messages.c.group_id==gid,messages.c.id==reply_to,messages.c.deleted==False)).first()):raise MessagingError('reply_unavailable',422)
                    queued=c.execute(select(func.count()).select_from(site_sends).where(site_sends.c.actor_id==actor,site_sends.c.state.in_(['queued','sending']))).scalar_one()
                    if queued>=20:raise MessagingError('queue_full',429)
                    jid=uuid.uuid4().hex;job={'id':jid,'group_id':gid,'actor_id':actor,'author':author,'client_id':client_id,'content_key':key,'pending_key':None,
                        'state':'queued','created':int(time.time()),'edited':0,'revision':0,'error':None,'reply_to':reply_to,'attachments':attachments,'message_ids':[],
                        'request_fingerprint':fingerprint,'retry_at':0,'next_part':0,'recording_header':recording_header}
                    c.execute(site_sends.insert().values(**{k:job[k] for k in ('id','group_id','actor_id','author','client_id','content_key')},value='{}'))
                    self.save(c,job)
                    c.execute(garbage.delete().where(garbage.c.key==key))
                    if attachments:c.execute(site_uploads.update().where(site_uploads.c.id.in_(attachments)).values(send_id=jid))
                    return self.public(job,c)

    def edit(self,gid,actor,mid,text,revision,client_id):
        if not isinstance(text,str) or len(text)>3500:raise MessagingError('invalid_text',422)
        with self.archive.objects.lease('siteedit/'+hashlib.sha256((gid+str(mid)).encode()).hexdigest()[:32]):
            with self.archive.upload('sitecontent') as key:
                self.archive.objects.put_json(key,{'text':text})
                with self.archive.transaction() as c:
                    self.require_send(gid,actor,c)
                    part=c.execute(select(site_parts).where(site_parts.c.group_id==gid,site_parts.c.message_id==mid)).mappings().first()
                    if not part or part['ordinal']!=0:raise MessagingError('message_not_found',404)
                    job=self.get(part['send_id'],c)
                    if job['actor_id']!=actor:raise MessagingError('not_author',403)
                    if job.get('edit_client_id')==client_id:
                        fingerprint=hashlib.sha256(json.dumps([text,revision]).encode()).hexdigest()
                        if job.get('edit_fingerprint')!=fingerprint:raise MessagingError('client_conflict')
                        if job['state'] in {'edit_queued','editing','delivered'}:return self.public(job,c)
                    if job.get('sent_at',job['created'])+EDIT_SECONDS<=time.time():raise MessagingError('edit_expired',403)
                    if job['revision']!=revision:raise MessagingError('edit_conflict')
                    if job['state'] not in {'delivered','edit_failed'}:raise MessagingError('message_busy')
                    if not(text.strip() or job['attachments']):raise MessagingError('invalid_text',422)
                    caption=bool(job['attachments']) and not job.get('recording_header',False)
                    if len(render_message(text,job['author'])[0].encode('utf-16-le'))//2>(1024 if caption else 4096):raise MessagingError('caption_too_long' if caption else 'text_too_long',422)
                    if job.get('pending_key'):self.archive.gc(c,job['pending_key'])
                    job.update(pending_key=key,state='edit_queued',edit_client_id=client_id,edit_fingerprint=hashlib.sha256(json.dumps([text,revision]).encode()).hexdigest(),error=None,retry_at=0)
                    self.save(c,job);c.execute(garbage.delete().where(garbage.c.key==key));return self.public(job,c)

    def list(self,gid,actor):
        result=[]
        with self.archive.engine.connect() as c:
            missing=select(site_parts.c.message_id).where(site_parts.c.send_id==site_sends.c.id,~select(messages.c.id).where(messages.c.group_id==site_parts.c.group_id,messages.c.id==site_parts.c.message_id).exists()).exists()
            query=select(site_sends.c.id).where(site_sends.c.group_id==gid,site_sends.c.actor_id==actor,site_sends.c.state!='cancelled',
                or_(site_sends.c.state.not_in(['delivered','edit_failed','edit_queued','editing']),missing)).order_by(site_sends.c.created.desc()).limit(100)
            for jid in c.execute(query):
                job=self.get(jid[0],c)
                if job['state']=='cancelled':continue
                if job['message_ids'] and all(c.execute(select(messages.c.id).where(messages.c.group_id==gid,messages.c.id==mid)).first() for mid in job['message_ids']) and job['state'] in {'delivered','edit_failed','edit_queued','editing'}:continue
                result.append(self.public(job,c))
        return sorted(result,key=lambda job:job['created'])[-100:]

    @staticmethod
    def attachment_index(job,ordinal):
        index=ordinal-int(job.get('recording_header',False))
        return index if 0<=index<len(job['attachments']) else None

    def decorate(self,c,row,result):
        part=c.execute(select(site_parts).where(site_parts.c.group_id==row['group_id'],site_parts.c.message_id==row['id'])).mappings().first()
        if not part or str(part['bot_id'])!=row['sender_id']:return
        job=self.get(part['send_id'],c)
        result['version']=self.version(c,row['group_id'],row['id'],row['fingerprint'])
        result.update(site_actor_id=job['actor_id'],site_send_id=job['id'],site_author=job['author'],site_revision=job['revision'],
            site_primary=part['ordinal']==0,
            site_editable_until=job.get('sent_at',job['created'])+EDIT_SECONDS,site_edit_state=job['state'],site_error=job.get('error'),sender=job['author'],sender_id='site:'+job['actor_id'],
            text=self.archive.objects.read_json(job['content_key'])['text'] if part['ordinal']==0 else '',edited=job['edited'],album_id='site:'+job['id'] if job['attachments'] else None)
        result['custom_emojis']=[]
        index=self.attachment_index(job,part['ordinal'])
        if index is not None:
            file=c.execute(select(site_uploads).where(site_uploads.c.id==job['attachments'][index])).mappings().first()
            if file:result['media']=[self.file(file)]

    def version(self,c,gid,mid,base):
        part=c.execute(select(site_parts).where(site_parts.c.group_id==gid,site_parts.c.message_id==mid)).mappings().first()
        if not part:return base
        sender=c.execute(select(messages.c.sender_id).where(messages.c.group_id==gid,messages.c.id==mid)).scalar()
        if str(part['bot_id'])!=sender:return base
        job=self.get(part['send_id'],c)
        return str(base)+':site:'+job['actor_id']+':'+str(job['revision'])

    def message_states(self,c,gid,ids):
        """Join confirmed bot bindings without a query for each notification."""
        rows=c.execute(select(messages.c.id,messages.c.edited,messages.c.fingerprint,
            messages.c.sender_id,site_parts.c.bot_id,site_sends.c.id.label('send_id'),
            site_sends.c.actor_id,site_sends.c.value).select_from(messages.outerjoin(site_parts,
                and_(site_parts.c.group_id==messages.c.group_id,site_parts.c.message_id==messages.c.id))
            .outerjoin(site_sends,site_sends.c.id==site_parts.c.send_id)).where(
                messages.c.group_id==gid,messages.c.deleted==False,messages.c.id.in_(ids))).mappings()
        result=[]
        for row in rows:
            version=row['fingerprint']
            if row['bot_id'] is not None and str(row['bot_id'])==row['sender_id']:
                if row['send_id'] is None:raise MessagingError('message_not_found',404)
                job={'actor_id':row['actor_id']}|json.loads(row['value'])
                version=str(version)+':site:'+job['actor_id']+':'+str(job['revision'])
            result.append({'id':row['id'],'edited':row['edited'],'fingerprint':version})
        return result

    def reuse_upload(self,c,gid,mid):
        part=c.execute(select(site_parts).where(site_parts.c.group_id==gid,site_parts.c.message_id==mid)).mappings().first()
        if not part:return
        row=c.execute(select(messages.c.sender_id).where(messages.c.group_id==gid,messages.c.id==mid)).first()
        if not row or str(part['bot_id'])!=row[0]:return
        job=self.get(part['send_id'],c)
        index=self.attachment_index(job,part['ordinal'])
        if index is None:return
        file=c.execute(select(site_uploads).where(site_uploads.c.id==job['attachments'][index])).mappings().first()
        item=c.execute(select(media).where(media.c.group_id==gid,media.c.message_id==mid)).mappings().first()
        if not file or not item:return
        value=json.loads(file['value'])
        if value['kind'] not in {'photo','video','video_note','animation'}:return
        if item['object_key']!=file['object_key']:self.archive.gc(c,item['object_key'])
        if item['thumb_key']!=file['thumb_key']:self.archive.gc(c,item['thumb_key'])
        c.execute(media.update().where(media.c.id==item['id']).values(object_key=file['object_key'],thumb_key=file['thumb_key'],
            size=value['size'],mime=value['mime'],name=value['name'],state='ready',error=None,retry_at=0))

    def cancel(self,gid,actor,jid):
        with self.archive.transaction() as c:
            job=self.get(jid,c)
            if job['group_id']!=gid or job['actor_id']!=actor:raise MessagingError('message_not_found',404)
            if job['state'] not in {'queued','failed','uncertain'}:raise MessagingError('message_busy')
            sent_files=max(0,job['next_part']-int(job.get('recording_header',False)))
            for fid in job['attachments'][sent_files:]:
                file=c.execute(select(site_uploads).where(site_uploads.c.id==fid)).mappings().first()
                if file:
                    self.archive.gc(c,file['object_key']);self.archive.gc(c,file['thumb_key']);c.execute(site_uploads.delete().where(site_uploads.c.id==fid))
            job.update(attachments=job['attachments'][:sent_files],state='delivered' if job['message_ids'] else 'cancelled',error='cancelled');self.save(c,job)

    def remove(self,c,jid):
        job=self.get(jid,c)
        for key in (job['content_key'],job.get('pending_key')):self.archive.gc(c,key)
        for row in c.execute(select(site_uploads).where(site_uploads.c.send_id==jid)).mappings():
            self.archive.gc(c,row['object_key']);self.archive.gc(c,row['thumb_key'])
        c.execute(site_uploads.delete().where(site_uploads.c.send_id==jid));c.execute(site_parts.delete().where(site_parts.c.send_id==jid));c.execute(site_sends.delete().where(site_sends.c.id==jid))

    def drop_message(self,c,gid,mid):
        part=c.execute(select(site_parts).where(site_parts.c.group_id==gid,site_parts.c.message_id==mid)).mappings().first()
        if not part:return
        job=self.get(part['send_id'],c)
        index=self.attachment_index(job,part['ordinal'])
        if index is not None:
            fid=job['attachments'][index]
            file=c.execute(select(site_uploads).where(site_uploads.c.id==fid)).mappings().first()
            if file:
                self.archive.gc(c,file['object_key']);self.archive.gc(c,file['thumb_key']);c.execute(site_uploads.delete().where(site_uploads.c.id==fid))
        c.execute(site_parts.delete().where(site_parts.c.group_id==gid,site_parts.c.message_id==mid))
        if not c.execute(select(site_parts.c.send_id).where(site_parts.c.send_id==job['id'])).first():self.remove(c,job['id'])

    def copy(self,c,source,target):
        for old in c.execute(select(site_sends.c.id).where(site_sends.c.group_id==source)).scalars():
            job=self.get(old,c)
            parts=[dict(row) for row in c.execute(select(site_parts).where(site_parts.c.send_id==old)).mappings()]
            parts=[part for part in parts if c.execute(select(messages.c.id).where(messages.c.group_id==target,messages.c.id==part['message_id'],messages.c.deleted==False)).first()]
            if not parts:continue
            copied=uuid.uuid4().hex;files=[]
            for ordinal,fid in enumerate(job['attachments']):
                if ordinal+int(job.get('recording_header',False)) not in {part['ordinal'] for part in parts}:files.append(fid);continue
                row=c.execute(select(site_uploads).where(site_uploads.c.id==fid)).mappings().first()
                if row:
                    new_id=uuid.uuid4().hex;files.append(new_id)
                    c.execute(site_uploads.insert().values(**(dict(row)|{'id':new_id,'group_id':target,'send_id':copied})))
                else:files.append(fid)
            job.update(id=copied,group_id=target,attachments=files,state='delivered',pending_key=None,error=None)
            c.execute(site_sends.insert().values(**{k:job[k] for k in ('id','group_id','actor_id','author','client_id','content_key')},value='{}'));self.save(c,job)
            c.execute(site_parts.insert(),[part|{'group_id':target,'send_id':copied} for part in parts])

    def maintain(self):
        with self.archive.transaction() as c:
            deleted=list(c.execute(select(site_parts.c.group_id,site_parts.c.message_id).join(messages,and_(messages.c.group_id==site_parts.c.group_id,messages.c.id==site_parts.c.message_id)).where(messages.c.deleted==True)))
            for gid,mid in deleted:self.drop_message(c,gid,mid)
            for row in c.execute(select(site_uploads).where(site_uploads.c.send_id.is_(None),site_uploads.c.created<time.time()-86400)).mappings():
                self.archive.gc(c,row['object_key']);self.archive.gc(c,row['thumb_key']);c.execute(site_uploads.delete().where(site_uploads.c.id==row['id']))
            for jid in list(c.execute(select(site_sends.c.id).where(site_sends.c.state.in_(['cancelled','failed','uncertain','queued']),site_sends.c.created<time.time()-86400)).scalars()):
                job=self.get(jid,c)
                if job['created']<time.time()-86400:
                    if not job['message_ids']:self.remove(c,jid)
                    else:
                        sent_files=max(0,job['next_part']-int(job.get('recording_header',False)))
                        for fid in job['attachments'][sent_files:]:
                            file=c.execute(select(site_uploads).where(site_uploads.c.id==fid)).mappings().first()
                            if file:
                                self.archive.gc(c,file['object_key']);self.archive.gc(c,file['thumb_key']);c.execute(site_uploads.delete().where(site_uploads.c.id==fid))
                        job.update(attachments=job['attachments'][:sent_files],state='delivered');self.save(c,job)

    async def tick(self,http=None):
        with self.archive.objects.lease('siteworker/'+'0'*32,blocking=False) as acquired:
            if not acquired:return
            self.recover()
            self.maintain()
            if self.paused():return
            await self.profile.tick(http)
            if self.paused():return
            for gid in self.config.archive_group_ids:
                await self.check_access(gid,http)
                if self.paused():return
            with self.archive.engine.connect() as c:ids=list(c.execute(select(site_sends.c.id).where(site_sends.c.state.in_(['queued','edit_queued']),site_sends.c.retry_at<=time.time()).order_by(site_sends.c.created).limit(1)).scalars())
            from .site_reactions import BotReactions
            reactions=await BotReactions(self).tick(http)
            if self.paused():return bool(reactions)
            for jid in ids:
                job=self.get(jid)
                if job['state'] in {'queued','edit_queued'} and job.get('retry_at',0)<=time.time():
                    await self.send(jid,http);return True
            return bool(reactions)

    async def send(self,jid,http=None):
        with self.archive.transaction() as c:
            job=self.get(jid,c);editing=job['state']=='edit_queued'
            if job['state'] not in {'queued','edit_queued'}:return
            try:
                self.require_send(job['group_id'],job['actor_id'],c)
                if editing and job.get('sent_at',job['created'])+EDIT_SECONDS<=time.time():raise MessagingError('edit_expired',403)
            except MessagingError as error:
                if editing:self.archive.gc(c,job.get('pending_key'));job['pending_key']=None
                job.update(state='edit_failed' if editing else 'failed',error=error.reason);self.save(c,job);return
            raw=c.execute(select(bot_access.c.value).where(bot_access.c.group_id==job['group_id'])).scalar();bot_id=json.loads(raw)['bot_id']
            job.update(state='editing' if editing else 'sending',error=None);self.save(c,job)
        try:
            text=self.archive.objects.read_json(job['pending_key'] if editing else job['content_key'])['text']
            async with httpx.AsyncClient(timeout=60,follow_redirects=False) as default:
                client=http or default
                start=0 if editing else job['next_part'];count=1 if editing else max(1,len(job['attachments'])+int(job.get('recording_header',False)))
                for index in range(start,count):
                    with self.archive.engine.connect() as c:self.require_send(job['group_id'],job['actor_id'],c)
                    file=None
                    attachment_index=self.attachment_index(job,index)
                    if attachment_index is not None:
                        with self.archive.engine.connect() as c:file=dict(c.execute(select(site_uploads).where(site_uploads.c.id==job['attachments'][attachment_index])).mappings().one())
                    body={'chat_id':self.config.telegram_id(job['group_id'])}
                    # Kept in one formatter so presentation can change independently.
                    rendered,entities=render_message(text if index==0 else '',job['author'])
                    field='caption' if file else 'text';body[field]=rendered
                    entity_field='caption_entities' if file else 'entities';body[entity_field]=entities
                    if editing:
                        body['message_id']=job['message_ids'][0];method='editMessageCaption' if file else 'editMessageText'
                        response=await client.post(f'https://api.telegram.org/bot{self.config.bot_token}/'+method,json=body)
                    elif file:
                        value=json.loads(file['value'])
                        if not self.capability(job['group_id']).get('can_send_'+{'photo':'photos','video':'videos','animation':'animations','file':'documents','voice':'voice_notes','video_note':'video_notes'}[value['kind']]):raise MessagingError('attachment_not_allowed',403)
                        field={'photo':'photo','video':'video','animation':'animation','file':'document','voice':'voice','video_note':'video_note'}[value['kind']]
                        method={'photo':'sendPhoto','video':'sendVideo','animation':'sendAnimation','file':'sendDocument','voice':'sendVoice','video_note':'sendVideoNote'}[value['kind']]
                        # Spooled on disk and removed even after cancellation/crash.
                        with self.archive.objects.workspace() as root:
                            path=Path(root)/'upload'
                            with path.open('wb') as output:
                                for chunk in self.archive.objects.read(file['object_key']):output.write(chunk)
                            upload_name=value['name'];upload_mime=value['mime']
                            if value['kind']=='voice' and value['mime']=='audio/webm':
                                # Telegram voice accepts Opus/Ogg or M4A; keep the uploaded original.
                                import subprocess
                                converted=Path(root)/'voice.ogg'
                                subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(path),'-vn','-c:a','libopus','-b:a','64k','-f','ogg',str(converted)],capture_output=True,timeout=60,check=True)
                                path=converted;upload_name='Голосовое.ogg';upload_mime='audio/ogg'
                            if value['kind']=='video_note':
                                body.pop('caption',None);body.pop('caption_entities',None);body['duration']=str(max(1,round(value['duration'])));body['length']=str(value['length'])
                            else:body[entity_field]=json.dumps(entities)
                            if job.get('reply_to'):body['reply_parameters']=json.dumps({'message_id':job['reply_to'],'allow_sending_without_reply':False})
                            with path.open('rb') as stream:
                                response=await client.post(f'https://api.telegram.org/bot{self.config.bot_token}/'+method,data=body,files={field:(upload_name,stream,upload_mime)})
                    else:
                        if job.get('reply_to'):body['reply_parameters']={'message_id':job['reply_to'],'allow_sending_without_reply':False}
                        response=await client.post(f'https://api.telegram.org/bot{self.config.bot_token}/sendMessage',json=body)
                    data=response.json()
                    if response.status_code==429:
                        self.pause(data.get('parameters',{}).get('retry_after',60))
                        with self.archive.transaction() as c:
                            job=self.get(jid,c);job.update(state='edit_queued' if editing else 'queued',retry_at=int(time.time())+max(1,int(data.get('parameters',{}).get('retry_after',60))));self.save(c,job)
                        return
                    not_modified=editing and response.status_code==400 and 'message is not modified' in data.get('description','').lower()
                    if not not_modified and (response.status_code!=200 or not data.get('ok')):
                        raise MessagingError('telegram_rejected' if 400<=response.status_code<500 else 'delivery_unknown',response.status_code)
                    remote=data.get('result') or {'message_id':job['message_ids'][0]}
                    with self.archive.transaction() as c:
                        job=self.get(jid,c)
                        if editing:
                            self.archive.gc(c,job['content_key']);job.update(content_key=job['pending_key'],pending_key=None,edited=int(time.time()),revision=job['revision']+1,state='delivered')
                        else:
                            mid=remote['message_id']
                            existing=c.execute(select(site_parts.c.send_id).where(site_parts.c.group_id==job['group_id'],site_parts.c.message_id==mid)).scalar()
                            if existing and existing!=jid:raise MessagingError('delivery_unknown',503)
                            if not existing:
                                c.execute(site_parts.insert().values(group_id=job['group_id'],message_id=mid,send_id=jid,ordinal=index,bot_id=bot_id))
                            self.reuse_upload(c,job['group_id'],mid)
                            job['message_ids'].append(mid);job.update(next_part=index+1,state='delivered' if index+1==count else 'sending')
                            job.setdefault('sent_at',int(remote.get('date',time.time())))
                        self.save(c,job)
        except (httpx.ConnectError,httpx.ConnectTimeout):
            with self.archive.transaction() as c:
                job=self.get(jid,c);job.update(state='edit_queued' if editing else 'queued',retry_at=int(time.time())+30);self.save(c,job)
        except Exception as error:
            with self.archive.transaction() as c:
                job=self.get(jid,c);known=isinstance(error,MessagingError) and 400<=error.status<500
                if editing and known:
                    self.archive.gc(c,job.get('pending_key'));job['pending_key']=None
                job.update(state=('edit_failed' if known else 'edit_queued') if editing else 'failed' if known else 'uncertain',
                    retry_at=int(time.time())+30,error=error.reason if isinstance(error,MessagingError) else 'delivery_unknown');self.save(c,job)

    def recover(self):
        with self.archive.transaction() as c:
            for jid in list(c.execute(select(site_sends.c.id).where(site_sends.c.state.in_(['sending','editing']))).scalars()):
                job=self.get(jid,c)
                if job['state'] in {'sending','editing'}:
                    job.update(state='edit_queued' if job['state']=='editing' else 'uncertain',error='worker_interrupted');self.save(c,job)
