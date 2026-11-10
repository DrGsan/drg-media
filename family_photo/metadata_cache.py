"""Optional Redis metadata cache; database revisions are the source of truth."""
import hashlib
import json
import logging
import threading
import time
import uuid

from sqlalchemy import event, select

from .db import group_management

REVISION_ID = 'message-cache-generation'
TRACKED_TABLES = {'telegram_messages', 'telegram_site_parts', 'telegram_site_sends'}
MAX_ENTRY_BYTES = 512 * 1024
COOLDOWN_SECONDS = 5


def track_changes(engine):
    """Advance the generation in the SAME transaction, including worker writes.

    Before Redis is first enabled the UPDATE matches no rows. Existing revisions
    keep advancing even if the writer's Redis URL is empty or Redis is down.
    Do not invalidate in a commit event: it runs before the DBAPI commit.
    """
    @event.listens_for(engine, 'before_execute')
    def before_write(c, statement, multiparams, params, options):
        table = getattr(statement, 'table', None)
        if (not getattr(statement, 'is_dml', False) or table is None
                or table.name not in TRACKED_TABLES):
            return
        scope = c.get_nested_transaction() or c.get_transaction()
        if scope is not None and scope in c.info.get('message_cache_scopes', ()):
            return
        c.execute(group_management.update().where(group_management.c.id == REVISION_ID)
                  .values(value=uuid.uuid4().hex))
        scope = c.get_nested_transaction() or c.get_transaction()
        c.info.setdefault('message_cache_scopes', set()).add(scope)
        c.info['message_cache_dirty'] = True

    def reset(c):
        c.info.pop('message_cache_dirty', None)
        c.info.pop('message_cache_scopes', None)

    for name in ('begin', 'commit', 'rollback'):
        event.listen(engine, name, reset)


class MetadataCache:
    def __init__(self, engine, config):
        self.engine = engine
        self.url = config.redis_url
        self.ttl = config.redis_cache_ttl
        self.namespace = hashlib.sha256(config.service_token.encode()).hexdigest()[:24]
        self.client = None
        self.unavailable_until = 0
        self.state = 'configured' if self.url else 'disabled'
        self.hits = self.misses = self.failures = 0
        self.lock = threading.Lock()
        if self.url:
            # A restart also makes keys from a restored/externally maintained DB
            # unusable. This is an existing metadata row, not a schema change.
            from sqlalchemy.dialects import postgresql, sqlite
            insert = (postgresql if engine.dialect.name == 'postgresql' else sqlite).insert
            value = uuid.uuid4().hex
            with engine.begin() as c:
                c.execute(insert(group_management).values(id=REVISION_ID, value=value)
                          .on_conflict_do_update(index_elements=['id'], set_={'value': value}))

    def public(self):
        with self.lock:
            return {'configured': bool(self.url), 'state': self.state,
                    'ttl_seconds': self.ttl, 'hits': self.hits,
                    'misses': self.misses, 'failures': self.failures}

    def _client(self):
        with self.lock:
            if self.client is None:
                from redis import Redis
                from redis.backoff import NoBackoff
                from redis.retry import Retry
                self.client = Redis.from_url(
                    self.url, socket_connect_timeout=.05, socket_timeout=.05,
                    retry=Retry(NoBackoff(), 0), max_connections=4,
                    protocol=2, decode_responses=False,
                )
            return self.client

    def _failed(self, error):
        with self.lock:
            warn = time.monotonic() >= self.unavailable_until
            self.unavailable_until = time.monotonic() + COOLDOWN_SECONDS
            self.state = 'fallback'
            self.failures += 1
        if warn:
            # Exception strings and URLs can contain Redis credentials.
            logging.getLogger(__name__).warning(
                'Metadata cache unavailable (%s); using database', type(error).__name__)

    @staticmethod
    def _valid(value, ids):
        if not isinstance(value, list) or len(value) > len(ids):
            return False
        seen = set()
        for row in value:
            if (not isinstance(row, dict) or set(row) != {'id', 'edited', 'fingerprint'}
                    or type(row['id']) is not int or row['id'] not in ids or row['id'] in seen
                    or type(row['edited']) is not int
                    or (row['fingerprint'] is not None and not isinstance(row['fingerprint'], str))):
                return False
            seen.add(row['id'])
        return True

    def load(self, c, gid, ids, loader):
        if not self.url or time.monotonic() < self.unavailable_until or not ids:
            return loader()
        # Always consult the primary, even for a Redis hit. Atomic revision
        # updates make old entries inaccessible after edits/deletions/expiry.
        revision = c.execute(select(group_management.c.value)
                             .where(group_management.c.id == REVISION_ID)).scalar()
        if not revision or c.info.get('message_cache_dirty'):
            return loader()
        signature = hashlib.sha256(json.dumps([gid, sorted(set(ids))], separators=(',', ':')).encode()).hexdigest()
        key = f'family-photo:message-states:1:{self.namespace}:{revision}:{signature}'
        try:
            client = self._client()
            encoded = client.get(key)
        except Exception as error:
            self._failed(error)
            return loader()
        with self.lock:
            self.state = 'ready'
        if encoded is not None:
            try:
                value = json.loads(encoded) if len(encoded) <= MAX_ENTRY_BYTES else None
            except (TypeError, ValueError, UnicodeError):
                value = None
            if self._valid(value, set(ids)):
                with self.lock:
                    self.hits += 1
                return value
        with self.lock:
            self.misses += 1
        value = loader()
        # A READ COMMITTED loader may have observed a newer revision. Never
        # fill a newer key with an older result, or reuse a rolled-back value.
        after = c.execute(select(group_management.c.value)
                          .where(group_management.c.id == REVISION_ID)).scalar()
        if revision == after and not c.info.get('message_cache_dirty'):
            encoded = json.dumps(value, separators=(',', ':')).encode()
            if len(encoded) <= MAX_ENTRY_BYTES:
                try:
                    client.set(key, encoded, ex=self.ttl)
                except Exception as error:
                    self._failed(error)
        return value
