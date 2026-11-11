import hmac
import hashlib
import json
import os
import re
import time
import threading
from pathlib import Path

from flask import Flask, jsonify, request, Response, abort
from sqlalchemy import select, and_, text, func
from werkzeug.exceptions import HTTPException
from .config import Config
from .db import database, groups, messages, media, runtime, outbox, metadata, people, emoji_assets,site_parts,site_sends
from .objects import Objects
from .archive import Archive
from . import __version__


def create_app(config=None, archive=None):
    background = config is None
    config = config or Config.from_env()
    config.validate()
    os.umask(0o077)
    config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    config.data_dir.chmod(0o700)
    archive = archive or Archive(database(config.database_url), Objects(config))
    from .metadata_cache import MetadataCache
    metadata_cache = MetadataCache(archive.engine, config)
    from .calendar_cache import CalendarCache
    calendar_cache = CalendarCache(archive.engine, metadata_cache)
    from .group_management import Registry, ManagedConfig, GroupInactive
    registry = getattr(archive, 'registry', None) or Registry(archive, config)
    config = ManagedConfig(config, registry)
    archive.objects.config = config
    from .site_messaging import SiteMessaging,MessagingError
    messaging=SiteMessaging(archive,config)
    from .site_reactions import BotReactions
    reactions=BotReactions(messaging)
    from .exports import Exports
    exports = Exports(archive, registry)
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 32 * 1024
    app.extensions["archive"] = archive
    app.extensions['metadata_cache'] = metadata_cache
    app.extensions['calendar_cache'] = calendar_cache
    app.extensions["exports"] = exports
    app.extensions['site_messaging']=messaging
    from .queue_polling import IdlePoller
    message_poll=IdlePoller((2,5,10))
    export_poll=IdlePoller((2,5,15,30))
    app.extensions['queue_pollers']={'messages':message_poll,'exports':export_poll}

    @app.errorhandler(MessagingError)
    def messaging_error(error):return jsonify(detail=error.reason),error.status

    @app.before_request
    def authenticate():
        if request.path != "/healthz":
            supplied = request.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied.encode(), ("Bearer " + config.service_token).encode()):
                return jsonify(detail="Unauthorized"), 401

    @app.after_request
    def private(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.method in {'POST','PUT','PATCH','DELETE'} and response.status_code<400:
            if request.path.startswith('/v1/exports'):export_poll.wake()
            elif request.path.startswith('/v1/admin/groups'):
                message_poll.wake();export_poll.wake()
            elif request.path.startswith('/v1/site-') or '/site-' in request.path:
                message_poll.wake()
        return response

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(detail=error.description), error.code

    @app.errorhandler(Exception)
    def unavailable(error):
        # Exceptions may contain signed storage URLs or Telegram credentials.
        app.logger.error("Archive request failed (%s)", type(error).__name__)
        return jsonify(detail="Archive storage is unavailable"), 503

    @app.errorhandler(GroupInactive)
    def inactive(error):
        return jsonify(detail='Group is disabled or pending deletion'), 404

    @app.put('/v1/site-reactions')
    def reaction_snapshots():
        request.max_content_length=2*1024*1024
        return reactions.snapshots(request.get_json(silent=True))

    @app.get('/v1/admin/groups')
    def managed_groups():
        registry.maintenance()
        return {'items': registry.catalog(), 'purged': registry.read()['purged'], 'server_time': int(time.time())}

    @app.get('/v1/admin/discovery')
    def discovery():
        return registry.read()['discovery']

    @app.post('/v1/admin/discovery')
    def refresh_discovery():
        return registry.request_discovery()

    @app.post('/v1/admin/groups')
    def add_group():
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) != {'telegram_id'} or not isinstance(data['telegram_id'], str) or not re.fullmatch(r'-[1-9]\d{0,19}', data['telegram_id']):
            abort(422, 'Invalid Telegram ID')
        try: registry.add(data['telegram_id'])
        except ValueError as error: abort(409, str(error))
        return {'ok': True}, 201

    @app.post('/v1/admin/groups/<gid>/<action>')
    def group_action(gid, action):
        data = request.get_json(silent=True)
        if data not in ({}, None): abort(422, 'Group IDs cannot be changed')
        if action not in {'enable', 'disable', 'delete', 'cancel'}: abort(404)
        try: value = registry.action(gid, action)
        except KeyError: abort(404)
        except ValueError as error: abort(409, str(error))
        return {'ok': True, **value}

    @app.post('/v1/admin/purged/ack')
    def acknowledge_purged():
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) != {'purged'} or not isinstance(data['purged'], dict): abort(422)
        with registry.edit() as (_, state):
            for gid, token in data['purged'].items():
                if state['purged'].get(gid) == token: state['purged'].pop(gid, None)
        return {'ok': True}

    @app.post('/v1/admin/groups/<gid>/copy')
    def copy_group(gid):
        data = request.get_json(silent=True)
        if not isinstance(data,dict) or set(data) != {'request_id'} or not isinstance(data['request_id'],str) or not re.fullmatch(r'[a-f0-9]{32}',data['request_id']): abort(422)
        try: copied = registry.copy(gid,data['request_id'])
        except GroupInactive: raise
        except ValueError as error: abort(409,str(error))
        return {'id':copied},201

    def export_actor():
        actor = request.args.get('actor_id','')
        if not re.fullmatch(r'[a-f0-9]{32}',actor): abort(422)
        return actor

    def export_job(jid, ready=False):
        try: job = exports.read(jid)
        except KeyError: abort(404)
        if job['actor_id'] != export_actor(): abort(404)
        if ready and (job['state'] != 'ready' or job['expires'] <= time.time() or not all(registry.active(g) for g in job['groups'])): abort(410)
        return job

    @app.get('/v1/exports')
    def list_exports():
        exports.maintain()
        return {'items':exports.list(export_actor()),'server_time':int(time.time())}

    @app.post('/v1/exports')
    def prepare_export():
        data=request.get_json(silent=True)
        if not isinstance(data,dict) or set(data) != {'groups','videos','request_id'}: abort(422)
        gids=data['groups']
        if not isinstance(gids,list) or not 1<=len(gids)<=100 or any(not isinstance(g,str) for g in gids) or len(set(gids))!=len(gids) or type(data['videos']) is not bool or not isinstance(data['request_id'],str) or not re.fullmatch(r'[a-f0-9]{32}',data['request_id']): abort(422)
        try: job=exports.create(export_actor(),sorted(gids),data['videos'],data['request_id'])
        except GroupInactive: raise
        except ValueError as error: abort(409,str(error))
        return job,202

    @app.get('/v1/exports/<jid>')
    def get_export(jid):
        return exports.public(export_job(jid))

    @app.get('/v1/admin/exports')
    def all_exports():
        from .db import export_jobs
        with archive.engine.connect() as c:
            titles=dict(c.execute(select(groups.c.id,groups.c.title)).all())
            items=[]
            for jid in c.execute(select(export_jobs.c.id)).scalars():
                job=exports.read(jid,c)
                if job['state'] in {'queued','running','ready'}:
                    items.append(exports.public(job)|{'actor_id':job['actor_id'],'group_titles':[titles.get(gid,'Семейная группа') for gid in job['groups']]})
            return {'items':items}

    @app.post('/v1/exports/<jid>/cancel')
    def cancel_export(jid):
        export_job(jid)
        exports.retire(jid,'cancelled')
        return {'ok':True}

    @app.get('/v1/exports/<jid>/download')
    def download_export(jid):
        job=export_job(jid,True)
        size=job['size']; start=0; end=size-1; code=200
        if header:=request.headers.get('Range'):
            match=re.fullmatch(r'bytes=(\d*)-(\d*)',header)
            if not match or not any(match.groups()): return Response(status=416,headers={'Content-Range':f'bytes */{size}'})
            first,last=match.groups()
            start=int(first) if first else max(0,size-int(last))
            end=min(int(last),size-1) if first and last else size-1
            if start>end or start>=size: return Response(status=416,headers={'Content-Range':f'bytes */{size}'})
            code=206
        headers={'Accept-Ranges':'bytes','Content-Length':str(end-start+1),'Content-Disposition':f'attachment; filename="drg-media-{jid}.zip"','ETag':'"'+jid+'"'}
        if code==206: headers['Content-Range']=f'bytes {start}-{end}/{size}'
        def stream():
            for block in archive.objects.read(job['object_key'],start,end):
                current=exports.read(jid)
                if current['state']!='ready' or current['expires']<=time.time() or not all(registry.active(g) for g in job['groups']): return
                yield block
        return Response(stream(),status=code,headers=headers,mimetype='application/zip')

    if background:
        def send_site_messages():
            import asyncio
            while True:
                try:worked=asyncio.run(messaging.tick())
                except Exception as error:
                    app.logger.warning('Bot work postponed (%s)',type(error).__name__)
                    worked=True  # Retry failures at the original cadence.
                message_poll.wait(worked)
        threading.Thread(target=send_site_messages,name='family-site-messages',daemon=True).start()
        def maintain():
            while True:
                try:
                    archive.expire_messages()
                    registry.maintenance()
                    exports.maintain()
                    archive.objects.clean_temporary()
                    archive.collect_garbage()
                except Exception as error:
                    app.logger.warning('Group cleanup postponed (%s)', type(error).__name__)
                time.sleep(5)
        threading.Thread(target=maintain, name='family-group-cleanup', daemon=True).start()
        def prepare():
            while True:
                try:worked=exports.tick()
                except Exception as error:
                    app.logger.warning('Photo export postponed (%s)',type(error).__name__)
                    worked=True
                export_poll.wait(worked)
        threading.Thread(target=prepare,name='drg-media-exports',daemon=True).start()

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/v1/status")
    def status():
        archive.objects.check()
        with archive.engine.connect() as c:
            worker = c.execute(select(runtime)).mappings().first()
        try:
            reader_id = json.loads((config.data_dir / '.telegram-account.json').read_text()).get('id')
            if type(reader_id) is not int or reader_id <= 0:
                reader_id = None
        except (OSError, ValueError, AttributeError):
            reader_id = None
        return {"service": "family-photo", "service_version": __version__, "api_version": 1, "storage": config.storage,
                "reader_id": reader_id,
                "worker": dict(worker) if worker else None,
                "worker_online": bool(worker and worker["heartbeat"] > time.time() - 120
                                      and worker["state"] in {"online", "rate_limited"}),
                "can_send": bool(config.bot_token), "group_management": 1, "photo_exports": 1, "group_copies": 1, "export_notifications": 1,"bot_messaging":1,"connection_status":1,"bot_reactions":1,
                "bot_profile": messaging.profile.public(), "gallery_versions": 1, "gallery_index": 1,
                "metadata_cache": metadata_cache.public(), "calendar_cache": calendar_cache.public()}

    storage_cache = None
    storage_checked = 0
    storage_lock = threading.Lock()

    @app.get('/v1/storage')
    def storage():
        nonlocal storage_cache, storage_checked
        with storage_lock:
            if storage_cache is not None and time.monotonic() - storage_checked < 60:
                return dict(storage_cache)
            usage = archive.objects.usage()
            if archive.engine.dialect.name == 'postgresql':
                with archive.engine.connect() as c:
                    database_bytes = sum(c.execute(text('SELECT COALESCE(pg_total_relation_size(to_regclass(:name)),0)'),
                                                  {'name': table}).scalar_one() for table in metadata.tables)
            else:
                database_bytes = 0
                location = archive.engine.url.database
                if location and location != ':memory:':
                    for suffix in ('', '-wal', '-shm'):
                        try:
                            database_bytes += Path(location + suffix).stat().st_size
                        except FileNotFoundError:
                            pass
            storage_cache = usage | {'database_bytes': database_bytes,
                                     'total_bytes': usage['objects_bytes'] + database_bytes,
                                     'checked_at': int(time.time())}
            storage_checked = time.monotonic()
            return dict(storage_cache)

    @app.get("/v1/groups")
    def list_groups():
        archive.expire_messages()
        controls=registry.read()["groups"]
        from .connection import source_status,source_health
        catalog=archive.group_list()
        health=source_health(archive)
        capabilities=messaging.capabilities(catalog)
        return {"items": [group | {'connection':source_status(archive,group['id'],group,controls,health=health)} | {k:v for k,v in controls.get(group['id'],{}).items() if k in {'copy_source','copy_created','copy_complete','copy_pending','copy_history_complete'}} | capabilities[group['id']]
                          for group in catalog
                          if controls.get(group['id'],{}).get('enabled') and controls[group['id']]['delete_at'] is None]}

    def require_group(gid):
        archive.expire_messages()
        from flask import abort
        group = archive.group(gid)
        if not group or not registry.active(gid):
            abort(404)

    def number(name, default=0, maximum=2**53-1):
        from flask import abort
        raw = request.args.get(name, str(default))
        if not raw.isdigit() or not 0 <= int(raw) <= maximum:
            abort(422, "Invalid pagination")
        return int(raw)

    @app.get('/v1/groups/<gid>/sync')
    def sync_progress(gid):
        require_group(gid)
        from .progress import snapshot
        return snapshot(archive, gid)

    @app.get('/v1/groups/<gid>/unread')
    def unread(gid):
        require_group(gid)
        after = number('after')
        excluded = request.args.get('exclude_sender', '')
        actor_excluded=request.args.get('exclude_actor','')
        if actor_excluded and not re.fullmatch('[a-f0-9]{32}',actor_excluded):abort(422)
        if excluded and not re.fullmatch(r'-?\d{1,20}', excluded):
            abort(422, 'Invalid sender')
        with archive.engine.connect() as c:
            query = select(func.count()).select_from(messages).where(messages.c.group_id == gid,
                messages.c.deleted == False, messages.c.id > after)
            if excluded:
                query = query.where(messages.c.sender_id != excluded)
            if actor_excluded:
                own=select(site_parts.c.message_id).join(site_sends,site_sends.c.id==site_parts.c.send_id).where(site_parts.c.group_id==gid,site_sends.c.actor_id==actor_excluded)
                query=query.where(messages.c.id.not_in(own))
            return {'unread': c.execute(query).scalar_one()}

    @app.post('/v1/groups/<gid>/message-states')
    def message_states(gid):
        require_group(gid)
        data = request.get_json(silent=True)
        ids = data.get('ids') if isinstance(data, dict) else None
        if not isinstance(ids, list) or len(ids) > 1000 or any(type(mid) is not int or not 0 < mid <= 2**53-1 for mid in ids):
            abort(422, 'Invalid message IDs')
        with archive.engine.connect() as c:
            return {'items': metadata_cache.load(c,gid,ids,lambda:messaging.message_states(c,gid,ids))}

    @app.get('/v1/groups/<gid>/updates')
    def updates(gid):
        require_group(gid)
        after, limit = number('after'), max(1, number('limit', 100, 100))
        with archive.engine.connect() as c:
            rows = c.execute(select(messages).where(messages.c.group_id == gid,
                messages.c.deleted == False, messages.c.id > after).order_by(messages.c.id).limit(limit)).mappings()
            return {'items': [archive.serialize(c, row) for row in rows]}

    @app.get('/v1/groups/<gid>/avatar')
    def avatar(gid):
        require_group(gid)
        profile = archive.group_profile(gid)
        if not profile.get('avatar_key') or (request.args.get('v') and request.args['v'] != profile['avatar_identity']):
            abort(404)
        return Response(archive.objects.read(profile['avatar_key']), mimetype='image/jpeg')

    @app.get('/v1/groups/<gid>/people/<sid>/avatar')
    def person_avatar(gid,sid):
        require_group(gid)
        if not re.fullmatch(r'-?\d{1,20}',sid):abort(404)
        with archive.engine.connect() as c:
            value=c.execute(select(people).where(people.c.group_id==gid,people.c.sender_id==sid)).mappings().first()
        if not value or not value['avatar_key'] or (request.args.get('v') and request.args['v']!=value['avatar_identity']):abort(404)
        return Response(archive.objects.read(value['avatar_key']),mimetype='image/jpeg')

    @app.get('/v1/groups/<gid>/emoji/<eid>')
    def emoji_asset(gid,eid):
        require_group(gid)
        if not re.fullmatch(r'\d{1,20}',eid):abort(404)
        with archive.engine.connect() as c:
            value=c.execute(select(emoji_assets).where(emoji_assets.c.group_id==gid,emoji_assets.c.document_id==eid,emoji_assets.c.state=='ready')).mappings().first()
        if not value or not value['object_key']:abort(404)
        if request.args.get('animation')=='1':
            if value['mime']!='application/x-tgsticker':abort(404)
            from .stickers import read_animation
            try:return jsonify(read_animation(archive.objects,value['object_key']))
            except (ValueError,OSError,EOFError):abort(422,'Emoji animation unavailable')
        thumbnail=request.args.get('thumbnail')=='1';key=value['thumb_key'] if thumbnail else value['object_key']
        if not key:abort(404)
        return Response(archive.objects.read(key),mimetype='image/webp' if thumbnail else value['mime'])

    @app.get("/v1/groups/<gid>/messages")
    def history(gid):
        require_group(gid)
        return archive.history(gid, number("before"), max(1, number("limit", 50, 100)))

    @app.get("/v1/groups/<gid>/messages/<int:mid>")
    def one_message(gid, mid):
        require_group(gid)
        with archive.engine.connect() as c:
            row = c.execute(select(messages).where(and_(messages.c.group_id == gid,
                messages.c.id == mid, messages.c.deleted == False))).mappings().first()
            if not row:
                return jsonify(detail="Message unavailable"), 404
            return archive.serialize(c, row)

    def gallery_query(gid):
        return select(media, messages.c.album_id, messages.c.sender).join(messages,
            and_(messages.c.group_id == media.c.group_id, messages.c.id == media.c.message_id)
        ).where(and_(media.c.group_id == gid, messages.c.deleted == False, media.c.kind.in_(['photo','video'])))

    def gallery_window(gid):
        query = gallery_query(gid)
        until = number('until', 0, 253402300799)
        return query.where(media.c.created < until) if until else query

    @app.get('/v1/groups/<gid>/media-index')
    def gallery_index(gid):
        require_group(gid)
        from .calendar_index import calendar_zone, month_bounds
        from zoneinfo import ZoneInfoNotFoundError
        zone = request.args.get('timezone', 'UTC')
        try:
            calendar_zone(zone)
        except (ValueError, ZoneInfoNotFoundError):
            abort(422, 'Invalid time zone')
        if archive.engine.dialect.name == 'postgresql':
            bucket = func.to_char(func.timezone(zone, func.to_timestamp(media.c.created)), 'YYYY-MM')
        else:
            bucket = func.fp_calendar_month(media.c.created, zone)
        query = gallery_query(gid).with_only_columns(bucket.label('month'), func.count().label('count'))
        query = query.where(bucket.is_not(None)).group_by(bucket).order_by(bucket.desc())
        def load():
            with archive.engine.connect() as c:
                rows = list(c.execute(query).mappings())
            items = []
            for row in rows:
                start, end = month_bounds(row['month'], zone)
                items.append({'key': row['month'], 'count': row['count'], 'start': start, 'end': end})
            version = hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()
            return {'items': items, 'timezone': zone, 'version': version}
        return calendar_cache.load(gid, zone, load)

    @app.get('/v1/groups/<gid>/media-version')
    def gallery_version(gid):
        require_group(gid)
        through=number('through')
        query=gallery_window(gid).order_by(media.c.message_id.desc())
        # Only inspect the visible window and newer media. No object/S3 reads,
        # writes, schema changes or scans of the group's unloaded old history.
        query=query.where(media.c.message_id>=through) if through else query
        with archive.engine.connect() as c:
            rows=list(c.execute(query.limit(10001 if through else 61)).mappings())
            older=bool(c.execute(gallery_window(gid).where(media.c.message_id<through).limit(1)).first()) if through else len(rows)>60
        if len(rows)>10000:return {'version':None}
        digest=hashlib.sha256()
        for row in (rows if through else rows[:60]):
            visible=archive.serial_media(row)|{k:row[k] for k in ('album_id','sender','identity','object_key','thumb_key')}
            digest.update(json.dumps(visible,sort_keys=True,separators=(',',':')).encode())
            digest.update(b'\n')
        digest.update(b'more' if older else b'end')
        return {'version':digest.hexdigest()}

    @app.get("/v1/groups/<gid>/media")
    def gallery(gid):
        require_group(gid)
        before = number("before")
        limit = max(1, number("limit", 60, 100))
        with archive.engine.connect() as c:
            query = gallery_window(gid)
            if before:
                query = query.where(media.c.message_id < before)
            rows = list(c.execute(query.order_by(media.c.message_id.desc()).limit(limit + 1)).mappings())
            return {"items": [archive.serial_media(row) | {"album_id": row["album_id"], "sender": row["sender"]}
                              for row in rows[:limit]], "more": len(rows) > limit}

    @app.get("/v1/groups/<gid>/media/<fid>")
    def download(gid, fid):
        require_group(gid)
        with archive.engine.connect() as c:
            row = c.execute(select(media).where(and_(media.c.group_id == gid, media.c.id == fid,
                media.c.state == "ready"))).mappings().first()
        if not row:
            return jsonify(detail="Media unavailable"), 404
        if request.args.get('animation')=='1':
            if row['kind']!='sticker' or row['mime']!='application/x-tgsticker':abort(404)
            from .stickers import read_animation
            try:return jsonify(read_animation(archive.objects,row['object_key']))
            except (ValueError, OSError, EOFError):return jsonify(detail='Sticker animation unavailable'),422
        thumbnail = request.args.get("thumbnail") == "1"
        audio = request.args.get("audio") == "1"
        if audio and row["kind"] not in {"voice", "audio"}: abort(404)
        if thumbnail and row["kind"] in {"voice", "audio"}: abort(404)
        key = row["thumb_key"] if thumbnail or audio else row["object_key"]
        if not key:
            return jsonify(detail="Thumbnail unavailable"), 404
        size = archive.objects.size(key)
        start, end, status = 0, size - 1, 200
        if header := request.headers.get("Range"):
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", header)
            if not match or not any(match.groups()):
                return Response(status=416, headers={"Content-Range": f"bytes */{size}"})
            first, last = match.groups()
            if first:
                start = int(first)
                end = min(int(last), size - 1) if last else size - 1
            else:
                start = max(0, size - int(last))
            if start > end or start >= size:
                return Response(status=416, headers={"Content-Range": f"bytes */{size}"})
            status = 206
        headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1),
                   "Content-Security-Policy": "sandbox; default-src 'none'"}
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        # Always attachment for non-browser media; names never become paths or headers.
        if request.args.get("download") == "1":
            from urllib.parse import quote
            headers["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(
                fid + '-audio.m4a' if audio else fid + '-preview.jpg' if thumbnail else row["name"], safe='')
        return Response(archive.objects.read(key, start, end), status=status, headers=headers,
                        mimetype="audio/mp4" if audio else ("image/webp" if row['kind']=='sticker' else "image/jpeg") if thumbnail else row["mime"])

    @app.post("/v1/groups/<gid>/messages")
    def send(gid):
        require_group(gid)
        if not config.bot_token:
            return jsonify(detail="Telegram bot is not configured"), 503
        if archive.group(gid)['kind'] != 'supergroup':
            # Basic groups have per-account message ID namespaces. Never reply
            # through a bot using the reader account's unrelated message IDs.
            return jsonify(detail="Bot replies require a Telegram supergroup"), 422
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) - {"actor_id", "author", "client_id", "text", "reply_to"}:
            return jsonify(detail="Invalid message"), 422
        for field, maximum in (("actor_id", 100), ("author", 100), ("client_id", 80), ("text", 3500)):
            value = data.get(field)
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                return jsonify(detail="Invalid " + field), 422
        if any(ord(x) < 32 for x in data["author"]):
            return jsonify(detail="Invalid author"), 422
        reply = data.get("reply_to")
        if reply is not None and (type(reply) is not int or not 0 < reply < 2**53):
            return jsonify(detail="Invalid reply"), 422
        try:
            return archive.enqueue(gid, data["actor_id"], data["author"], data["client_id"], data["text"], reply), 202
        except ValueError as error:
            return jsonify(detail=str(error)), 409

    @app.get("/v1/groups/<gid>/outbox/<oid>")
    def delivery(gid, oid):
        require_group(gid)
        with archive.engine.connect() as c:
            row = c.execute(select(outbox).where(and_(outbox.c.group_id == gid, outbox.c.id == oid))).mappings().first()
        if not row:
            return jsonify(detail="Delivery unavailable"), 404
        return {key: row[key] for key in ("id", "actor_id", "state", "message_id", "error")}

    @app.get("/v1/groups/<gid>/outbox")
    def pending_deliveries(gid):
        require_group(gid)
        actor = request.args.get('actor_id', '')
        if not actor or len(actor) > 100:
            return jsonify(detail="Invalid actor"), 422
        result = []
        with archive.engine.connect() as c:
            archived = select(messages.c.id).where(and_(messages.c.group_id == outbox.c.group_id,
                messages.c.id == outbox.c.message_id)).exists()
            rows = c.execute(select(outbox).where(and_(outbox.c.group_id == gid,
                outbox.c.actor_id == actor, ~and_(outbox.c.state == 'delivered', archived)))
                .order_by(outbox.c.created.desc()).limit(100)).mappings()
            for row in rows:
                result.append({key: row[key] for key in ('id', 'state', 'message_id', 'error', 'created')} |
                              {'text': archive.objects.read_json(row['content_key'])['text']})
        return {'items': result}

    from .site_messaging_api import register_site_routes
    register_site_routes(app,archive,messaging,require_group)
    return app
