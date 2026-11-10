"""Service-token-only portal bot routes, including bounded raw uploads."""
import json
import re
from urllib.parse import quote
from flask import request,Response,abort
from sqlalchemy import select
from .db import site_uploads
from .site_messaging import MAX_FILE,MessagingError


def register_site_routes(app,archive,messaging,require_group):
    def actor():
        value=request.args.get('actor_id','')
        if not re.fullmatch('[a-f0-9]{32}',value):abort(422)
        return value

    @app.put('/v1/site-permissions')
    def site_permissions():
        revision=messaging.sync_permissions(request.get_json(silent=True))
        return {'ok':True,'revision':revision}

    @app.before_request
    def upload_limit():
        if request.method=='PUT' and '/site-uploads/' in request.path:request.max_content_length=MAX_FILE

    @app.put('/v1/groups/<gid>/site-uploads/<fid>')
    def site_upload(gid,fid):
        require_group(gid)
        return messaging.upload(gid,actor(),fid,request.stream,request.args.get('name',''),request.args.get('mode','file'),request.headers.get('X-DrG-Recording')),201

    @app.delete('/v1/groups/<gid>/site-uploads/<fid>')
    def site_discard(gid,fid):
        require_group(gid);messaging.discard_upload(gid,actor(),fid);return {'ok':True}

    @app.get('/v1/groups/<gid>/site-uploads/<fid>')
    def site_download(gid,fid):
        require_group(gid);uid=actor()
        with archive.engine.connect() as c:
            row=c.execute(select(site_uploads).where(site_uploads.c.group_id==gid,site_uploads.c.id==fid)).mappings().first()
        if not row or not row['send_id'] and row['actor_id']!=uid:abort(404)
        thumbnail=request.args.get('thumbnail')=='1';key=row['thumb_key'] if thumbnail else row['object_key']
        if not key:abort(404)
        value=json.loads(row['value']);size=archive.objects.size(key);start,end,code=0,size-1,200
        if header:=request.headers.get('Range'):
            match=re.fullmatch(r'bytes=(\d*)-(\d*)',header)
            if not match or not any(match.groups()):return Response(status=416,headers={'Content-Range':f'bytes */{size}'})
            first,last=match.groups();start=int(first) if first else max(0,size-int(last));end=min(int(last),end) if first and last else end
            if start>end or start>=size:return Response(status=416,headers={'Content-Range':f'bytes */{size}'})
            code=206
        mime='image/jpeg' if thumbnail else value['mime']
        headers={'Content-Length':str(end-start+1),'Accept-Ranges':'bytes','Content-Security-Policy':"sandbox; default-src 'none'"}
        if code==206:headers['Content-Range']=f'bytes {start}-{end}/{size}'
        if request.args.get('download')=='1' or value['kind']=='file':headers['Content-Disposition']="attachment; filename*=UTF-8''"+quote(value['name'],safe='')
        return Response(archive.objects.read(key,start,end),status=code,headers=headers,mimetype=mime)

    @app.get('/v1/groups/<gid>/site-sends')
    def site_pending(gid):require_group(gid);return {'items':messaging.list(gid,actor())}

    @app.post('/v1/groups/<gid>/site-sends')
    def site_send(gid):
        require_group(gid);uid=actor();data=request.get_json(silent=True)
        if not isinstance(data,dict) or set(data)-{'author','client_id','text','attachments','reply_to'}:abort(422)
        return messaging.create(gid,uid,data.get('author'),data.get('client_id'),data.get('text',''),data.get('attachments',[]),data.get('reply_to')),202

    @app.get('/v1/groups/<gid>/site-sends/<jid>')
    def site_delivery(gid,jid):
        require_group(gid);job=messaging.get(jid)
        if job['group_id']!=gid or job['actor_id']!=actor():abort(404)
        return messaging.public(job)

    @app.post('/v1/groups/<gid>/site-sends/<jid>/cancel')
    def site_cancel(gid,jid):require_group(gid);messaging.cancel(gid,actor(),jid);return {'ok':True}

    @app.put('/v1/groups/<gid>/site-messages/<int:mid>')
    def site_edit(gid,mid):
        require_group(gid);data=request.get_json(silent=True)
        if not isinstance(data,dict) or set(data)!={'text','revision','client_id'} or type(data['revision']) is not int or not re.fullmatch('[a-zA-Z0-9_-]{1,80}',str(data['client_id'])):abort(422)
        return messaging.edit(gid,actor(),mid,data['text'],data['revision'],data['client_id']),202
