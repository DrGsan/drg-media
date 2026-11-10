"""Owner-managed sources, durable seven-day deletion, and Telegram discovery."""
import hashlib
import json
import time
import uuid
from contextlib import contextmanager
from sqlalchemy import select, func
from .db import group_management, groups, messages, media, outbox, group_profiles, people, emoji_assets, garbage, expirations,site_sends,site_uploads,site_parts,bot_access

PERIOD = 7 * 86400


class GroupInactive(ValueError):
    pass


class Registry:
    def __init__(self, archive, config):
        self.archive, self.config = archive, config
        archive.registry = self
        with archive.objects.lease('registry/' + '0'*32):
            with archive.transaction() as c:
                if not c.execute(select(group_management.c.id).where(group_management.c.id == 'registry')).first():
                    catalog = {r['id']: dict(r) for r in c.execute(select(groups)).mappings()}
                    bindings = {config.archive_id(source): source for source in config.group_ids if config.archive_id(source) in catalog}
                    state = {'version': 1, 'groups': {}, 'discovery': {'items': [], 'requested': 0, 'checked': 0}, 'purged': {}}
                    for gid in set(catalog) | set(bindings):
                        state['groups'][gid] = {'telegram_id': bindings.get(gid),
                            'enabled': gid in bindings or catalog.get(gid, {}).get('kind') == 'export',
                            'delete_at': None, 'phase': None}
                    c.execute(group_management.insert().values(id='registry', value=json.dumps(state)))

    def read(self, c=None, lock=False):
        query = select(group_management.c.value).where(group_management.c.id == 'registry')
        if lock: query = query.with_for_update()
        if c is not None: return json.loads(c.execute(query).scalar_one())
        with self.archive.engine.connect() as connection: return self.read(connection)

    @contextmanager
    def edit(self):
        with self.archive.transaction() as c:
            state = self.read(c, lock=True)
            yield c, state
            self.save(c, state)

    @staticmethod
    def save(c, state):
        c.execute(group_management.update().where(group_management.c.id == 'registry').values(value=json.dumps(state)))

    def require_write(self, c, gid):
        value = self.read(c, lock=True)['groups'].get(gid)
        if not value or not value['enabled'] or value['delete_at'] is not None:
            raise GroupInactive('group_inactive')

    def active(self, gid):
        value = self.read()['groups'].get(gid)
        return bool(value and value['enabled'] and value['delete_at'] is None)

    def catalog(self):
        state = self.read()
        result = []
        catalog=self.archive.group_list(media_stats=True)
        from .connection import source_status,source_health
        health=source_health(self.archive)
        bot=getattr(self.archive,'site_messaging',None)
        capabilities=bot.capabilities(catalog) if bot else {}
        for group in catalog:
            gid = group['id']; control = state['groups'].get(gid)
            if not control: continue
            connection=source_status(self.archive,gid,group,state['groups'],health=health)
            result.append(group | control | {'connection':connection,**capabilities.get(gid,{})})
        return result

    def import_source(self, gid):
        # Explicit HTML import remains an operator command, never env discovery.
        with self.edit() as (_, state):
            if gid in state['purged']: raise ValueError('cleanup_pending')
            if gid not in state['groups']:
                state['groups'][gid] = {'telegram_id': None, 'enabled': True, 'delete_at': None, 'phase': None}

    def add(self, telegram_id):
        with self.edit() as (c, state):
            if any(gid == telegram_id or value['telegram_id'] == telegram_id for gid, value in state['groups'].items()):
                raise ValueError('already_connected')
            if telegram_id in state['purged']: raise ValueError('cleanup_pending')
            discovery = state['discovery']
            item = next((g for g in discovery['items'] if g['id'] == telegram_id), None)
            if not item or discovery['checked'] < time.time() - 900: raise ValueError('refresh_discovery')
            state['groups'][telegram_id] = {'telegram_id': telegram_id, 'enabled': True, 'delete_at': None, 'phase': None}
            c.execute(groups.insert().values(id=telegram_id, title=item['title'], kind=item['kind']))

    def copy(self, source, request_id):
        """One atomic metadata snapshot. All objects remain immutable and shared."""
        self.archive.expire_messages()
        with self.edit() as (c, state):
            for gid, control in state['groups'].items():
                if control.get('copy_request') == request_id and control.get('copy_source') == source:
                    return gid
            control = state['groups'].get(source)
            if not control or control['delete_at'] is not None or control.get('phase'):
                raise GroupInactive('group_inactive')
            if control.get('copy_source') or control.get('copy_created') is not None:
                raise ValueError('copy_of_copy')
            row = dict(c.execute(select(groups).where(groups.c.id == source)).mappings().one())
            gid = 'export:' + uuid.uuid4().hex[:24]
            stamp = int(time.time())
            pending = c.execute(select(func.count()).select_from(media).where(media.c.group_id == source, media.c.state != 'ready')).scalar_one()
            complete = row['history_complete'] and not pending
            state['groups'][gid] = dict(telegram_id=None, enabled=True, delete_at=None, phase=None,
                copy_source=source, copy_created=stamp, copy_complete=bool(complete), copy_request=request_id,
                copy_history_complete=row['history_complete'], copy_pending=pending)
            row.update(id=gid, title=row['title'] + ' · Копия ' + time.strftime('%d.%m.%Y %H:%M UTC', time.gmtime(stamp)),
                       kind='export', state='snapshot', history_complete=True, last_sync=stamp, retry_at=0)
            c.execute(groups.insert().values(**row))
            # Expiration timers and the sending queue deliberately belong only to the live source.
            for table, field in ((messages,'group_id'), (media,'group_id'), (group_profiles,'group_id'), (people,'group_id'), (emoji_assets,'group_id'), (outbox,'group_id')):
                rows = [dict(r) for r in c.execute(select(table).where(table.c[field] == source)).mappings()]
                if table is outbox: rows = [item for item in rows if item['state'] == 'delivered']
                for item in rows:
                    item[field] = gid
                    if table is outbox: item['id'] = uuid.uuid4().hex
                    if table is media:
                        item['id'] = uuid.uuid4().hex
                        if item['state'] != 'ready': item.update(state='paused', retry_at=0, error=None)
                    if table is emoji_assets and item['state'] != 'ready': item.update(state='paused', retry_at=0)
                for start in range(0, len(rows), 100): c.execute(table.insert(), rows[start:start+100])
            if getattr(self.archive,'site_messaging',None):self.archive.site_messaging.copy(c,source,gid)
            return gid

    def action(self, gid, action, stamp=None):
        stamp = int(time.time()) if stamp is None else stamp
        with self.edit() as (c, state):
            value = state['groups'].get(gid)
            if not value: raise KeyError(gid)
            if action == 'cancel':
                if value['delete_at'] is None or stamp >= value['delete_at'] or value['phase']:
                    raise ValueError('cancellation_expired')
                value['enabled'] = value.pop('previous_enabled', False)
                value['delete_at'] = None
            elif action == 'delete':
                if value['delete_at'] is None:
                    value['previous_enabled'] = value['enabled']
                    value['enabled'] = False
                    value['delete_at'] = stamp + PERIOD
            elif action in {'enable', 'disable'}:
                if value['delete_at'] is not None: raise ValueError('deletion_pending')
                value['enabled'] = action == 'enable'
                if value['enabled'] and value.get('telegram_id'):
                    # Force chronological catch-up after a pause.
                    c.execute(groups.update().where(groups.c.id == gid).values(last_sync=0, retry_at=0))
            else: raise ValueError('invalid_action')
            return dict(value)

    def request_discovery(self):
        with self.edit() as (_, state):
            discovery = state['discovery']
            if discovery['requested'] <= discovery['checked']:
                discovery['requested'] = max(int(time.time()), discovery['checked'] + 1)
                discovery.pop('error', None)
            return dict(discovery)

    def publish_discovery(self, items, error=None):
        with self.edit() as (_, state):
            discovery = state['discovery']
            if not error: discovery['items'] = items
            discovery['checked'] = max(int(time.time()), discovery['requested'])
            if error: discovery['error'] = error
            else: discovery.pop('error', None)

    def maintenance(self, stamp=None):
        stamp = int(time.time()) if stamp is None else stamp
        with self.edit() as (c, state):
            for gid, value in list(state['groups'].items()):
                if value['delete_at'] is None or stamp < value['delete_at']: continue
                if not value['phase']:
                    keys = set()
                    identities = set(c.execute(select(media.c.identity).where(media.c.group_id == gid)).scalars())
                    identities.update(c.execute(select(emoji_assets.c.document_id).where(emoji_assets.c.group_id == gid)).scalars())
                    for table, columns in ((messages, ('content_key',)), (media, ('object_key', 'thumb_key')),
                        (site_sends,('content_key','pending_key')), (site_uploads,('object_key','thumb_key')),
                        (outbox, ('content_key',)), (group_profiles, ('avatar_key',)), (people, ('avatar_key',)),
                        (emoji_assets, ('object_key', 'thumb_key'))):
                        for row in c.execute(select(*(table.c[field] for field in columns)).where(table.c.group_id == gid)):
                            keys.update(key for key in row if key)
                        c.execute(table.delete().where(table.c.group_id == gid))
                    c.execute(expirations.delete().where(expirations.c.group_id == gid))
                    c.execute(site_parts.delete().where(site_parts.c.group_id==gid))
                    c.execute(bot_access.delete().where(bot_access.c.group_id==gid))
                    for reaction in c.execute(select(group_management).where(group_management.c.id.like('react:%'))).mappings().all():
                        if json.loads(reaction['value']).get('group_id')==gid:c.execute(group_management.delete().where(group_management.c.id==reaction['id']))
                    for key in keys: self.archive.gc(c, key)
                    value.update(phase='purging', purge_keys=sorted(keys), identities=sorted(identities))
                # Successful object GC removes its intent; errors remain retryable.
                keys = value['purge_keys']
                if any(c.execute(select(garbage.c.key).where(garbage.c.key.in_(keys[start:start+500])).limit(1)).first() for start in range(0,len(keys),500)): continue
                self.clean_sidecars(gid, value['identities'], c)
                c.execute(groups.delete().where(groups.c.id == gid))
                del state['groups'][gid]
                state['purged'][gid] = hashlib.sha256((gid + ':' + str(value['delete_at'])).encode()).hexdigest()

    def clean_sidecars(self, gid, identities, c):
        from .downloads import DownloadCache
        # Shared file identities belonging to another archive keep their checkpoint.
        shared = set()
        for start in range(0,len(identities),500):
            shared.update(c.execute(select(media.c.identity).where(media.c.identity.in_(identities[start:start+500]))).scalars())
            shared.update(c.execute(select(emoji_assets.c.document_id).where(emoji_assets.c.document_id.in_(identities[start:start+500]))).scalars())
        DownloadCache(self.archive.objects).remove_identities(set(identities) - shared)
        from .progress import remove_group
        remove_group(self.config.data_dir, gid)


class ManagedConfig:
    """Read the registry on demand; environment is only a one-time import."""
    def __init__(self, config, registry):
        self.base, self.registry = config, registry

    def __getattr__(self, name): return getattr(self.base, name)

    @property
    def group_bindings(self):
        return tuple((v['telegram_id'], gid) for gid, v in self.registry.read()['groups'].items()
                     if v['enabled'] and v['delete_at'] is None and v['telegram_id'])

    @property
    def group_ids(self): return tuple(source for source, _ in self.group_bindings)

    @property
    def archive_group_ids(self): return tuple(gid for _, gid in self.group_bindings)

    def archive_id(self, telegram_id): return dict(self.group_bindings).get(str(telegram_id), str(telegram_id))

    def telegram_id(self, archive_id): return next((source for source, gid in self.group_bindings if gid == archive_id), archive_id)
