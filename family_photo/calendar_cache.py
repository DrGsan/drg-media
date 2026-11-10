"""Shared month-index cache, validated against transactional per-group revisions."""
import hashlib
import json
import threading
import time
import uuid
from collections import OrderedDict

from sqlalchemy import event, select
from sqlalchemy.dialects import postgresql, sqlite
from .db import group_management

GLOBAL = 'gallery-index-generation'
MAX_BYTES = 512 * 1024
TTL = 300


def group_key(gid):
    return 'gallery:' + hashlib.sha256(gid.encode()).hexdigest()[:32]


def track_calendar_changes(engine):
    insert = (postgresql if engine.dialect.name == 'postgresql' else sqlite).insert

    def write_revision(c, key):
        value = uuid.uuid4().hex
        c.execute(insert(group_management).values(id=key, value=value)
                  .on_conflict_do_update(index_elements=['id'], set_={'value': value}))

    # Also invalidates old Redis entries after restart, restore or rolling update.
    with engine.begin() as c:
        write_revision(c, GLOBAL)

    @event.listens_for(engine, 'before_execute')
    def changed(c, statement, multiparams, params, options):
        table = getattr(statement, 'table', None)
        if not getattr(statement, 'is_dml', False) or table is None:
            return
        if table.name not in {'telegram_media', 'telegram_messages'}:
            return
        values = getattr(statement, '_values', None)
        fields = {getattr(k, 'name', k) for k in values} if values is not None else set(params or {})
        relevant = {'created', 'group_id', 'kind', 'message_id'} if table.name == 'telegram_media' else {'created', 'group_id', 'deleted'}
        if statement.is_update and fields and not fields.intersection(relevant):
            return
        payloads = multiparams or [params or {}]
        compiled = statement.compile().params if statement.is_insert or values else {}
        gids = set()
        if not statement.is_insert:
            # Uses the same WHERE/binds as the write; no SQL-string parsing.
            lookup = select(table.c.group_id).where(*statement._where_criteria).distinct()
            for payload in payloads:
                gids.update(c.execute(lookup, payload).scalars())
        for payload in payloads:
            gid = (compiled | payload).get('group_id')
            if isinstance(gid, str):
                gids.add(gid)
        if not gids:
            return
        scope = c.get_nested_transaction() or c.get_transaction()
        seen = c.info.setdefault('calendar_cache_scopes', set())
        for gid in gids:
            if (scope, gid) not in seen:
                write_revision(c, group_key(gid))
                seen.add((scope, gid))

    def reset(c):
        c.info.pop('calendar_cache_scopes', None)

    for name in ('begin', 'commit', 'rollback'):
        event.listen(engine, name, reset)


class CalendarCache:
    def __init__(self, engine, metadata_cache):
        self.engine = engine
        self.shared = metadata_cache
        self.local = OrderedDict()
        self.lock = threading.Lock()
        self.flights = {}
        self.hits = self.misses = self.redis_hits = 0

    def public(self):
        with self.lock:
            return {'hits': self.hits, 'misses': self.misses, 'redis_hits': self.redis_hits,
                    'local_entries': len(self.local), 'max_local_entries': 64,
                    'redis_configured': bool(self.shared.url), 'ttl_seconds': TTL}

    def revision(self, gid):
        with self.engine.connect() as c:
            rows = dict(c.execute(select(group_management.c.id, group_management.c.value)
                                  .where(group_management.c.id.in_([GLOBAL, group_key(gid)]))).all())
        return [rows.get(GLOBAL, ''), rows.get(group_key(gid), '')]

    @staticmethod
    def decode(encoded, signature, zone):
        if not isinstance(encoded, bytes) or len(encoded) > MAX_BYTES:
            return None
        try:
            envelope = json.loads(encoded)
            if not isinstance(envelope, dict) or envelope.get('scope') != signature:
                return None
            data = envelope.get('data')
            if (isinstance(data, dict) and set(data) == {'items', 'version', 'timezone'}
                    and data['timezone'] == zone and isinstance(data['items'], list)
                    and isinstance(data['version'], str)
                    and hashlib.sha256(json.dumps(data['items'], sort_keys=True).encode()).hexdigest() == data['version']):
                return data
        except (ValueError, UnicodeError):
            pass

    def load(self, gid, zone, loader):
        revision = self.revision(gid)
        signature = hashlib.sha256(json.dumps([gid, zone, revision]).encode()).hexdigest()
        key = 'family-photo:calendar:1:' + self.shared.namespace + ':' + signature
        with self.lock:
            gate = self.flights.setdefault(key, threading.Lock())
        try:
            with gate:
                return self._load(gid, zone, loader, revision, key, signature)
        finally:
            with self.lock:
                if self.flights.get(key) is gate:
                    self.flights.pop(key, None)

    def _load(self, gid, zone, loader, revision, key, signature):
        now = time.monotonic()
        with self.lock:
            entry = self.local.get(key)
            if entry and now - entry[0] < TTL:
                self.local.move_to_end(key)
                self.hits += 1
                return self.decode(entry[1], signature, zone)
        use_redis = bool(self.shared.url) and now >= self.shared.unavailable_until
        encoded = None
        if use_redis:
            try:
                encoded = self.shared._client().get(key)
            except Exception as error:
                self.shared._failed(error)
                use_redis = False
        value = self.decode(encoded, signature, zone)
        if value is None:
            with self.lock:
                self.misses += 1
            value = loader()
            encoded = json.dumps({'scope': signature, 'data': value}, separators=(',', ':')).encode()
            # A concurrent transaction cannot fill a newer revision with old data.
            if revision != self.revision(gid) or len(encoded) > MAX_BYTES:
                return value
            if use_redis:
                try:
                    self.shared._client().set(key, encoded, ex=TTL)
                except Exception as error:
                    self.shared._failed(error)
        else:
            with self.lock:
                self.hits += 1
                self.redis_hits += 1
        with self.lock:
            self.local[key] = (now, encoded)
            self.local.move_to_end(key)
            while len(self.local) > 64:
                self.local.popitem(last=False)
        return value
