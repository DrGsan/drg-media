from sqlalchemy import (MetaData, Table, Column, String, BigInteger, Integer,
                        Boolean, Text, UniqueConstraint, create_engine, event)
from sqlalchemy.pool import StaticPool

metadata = MetaData()
groups = Table("telegram_groups", metadata,
    Column("id", String(32), primary_key=True), Column("title", Text, nullable=False),
    Column("kind", String(16), nullable=False, default="unknown"),
    Column("cursor", BigInteger, nullable=False, default=0),
    Column("history_complete", Boolean, nullable=False, default=False),
    Column("last_sync", BigInteger, nullable=False, default=0),
    Column("retry_at", BigInteger, nullable=False, default=0),
    Column("last_reconcile", BigInteger, nullable=False, default=0),
    Column("reconcile_cursor", BigInteger, nullable=False, default=0),
    Column("state", String(40), nullable=False, default="waiting"))
messages = Table("telegram_messages", metadata,
    Column("group_id", String(32), primary_key=True), Column("id", BigInteger, primary_key=True),
    Column("created", BigInteger, nullable=False, index=True), Column("edited", BigInteger, nullable=False, default=0),
    Column("sender_id", String(40), nullable=False), Column("sender", Text, nullable=False),
    Column("album_id", String(40)), Column("reply_to", BigInteger),
    Column("content_key", String(200)), Column("fingerprint", String(64)),
    Column("deleted", Boolean, nullable=False, default=False),
    Column("missing_since", BigInteger, nullable=False, default=0))
media = Table("telegram_media", metadata,
    Column("id", String(32), primary_key=True), Column("group_id", String(32), nullable=False, index=True),
    Column("message_id", BigInteger, nullable=False), Column("created", BigInteger, nullable=False),
    Column("identity", String(80), nullable=False), Column("kind", String(16), nullable=False),
    Column("mime", String(120), nullable=False), Column("name", Text, nullable=False),
    Column("size", BigInteger, nullable=False, default=0), Column("object_key", String(200)),
    Column("thumb_key", String(200)), Column("state", String(20), nullable=False, default="pending"),
    Column("attempts", Integer, nullable=False, default=0), Column("retry_at", BigInteger, nullable=False, default=0),
    Column("error", String(80)), UniqueConstraint("group_id", "message_id"))
outbox = Table("telegram_outbox", metadata,
    Column("id", String(32), primary_key=True), Column("group_id", String(32), nullable=False),
    Column("actor_id", String(100), nullable=False), Column("author", Text, nullable=False),
    Column("client_id", String(80), nullable=False), Column("content_key", String(200), nullable=False),
    Column("fingerprint", String(64), nullable=False), Column("reply_to", BigInteger),
    Column("created", BigInteger, nullable=False), Column("message_id", BigInteger),
    Column("state", String(24), nullable=False, default="queued"), Column("error", String(80)),
    Column("retry_at", BigInteger, nullable=False, default=0),
    UniqueConstraint("group_id", "actor_id", "client_id"))
garbage = Table("object_gc", metadata, Column("key", String(200), primary_key=True),
    Column("created", BigInteger, nullable=False, default=0))
runtime = Table("runtime", metadata, Column("id", Integer, primary_key=True),
    Column("heartbeat", BigInteger, nullable=False), Column("state", String(40), nullable=False))

# Additive metadata tables keep existing message/media schemas and IDs intact.
group_profiles = Table("telegram_group_profiles", metadata,
    Column("group_id", String(32), primary_key=True), Column("avatar_key", String(200)),
    Column("avatar_identity", String(80)), Column("checked_at", BigInteger, nullable=False, default=0))


# Reusable presentation caches. Original message/media rows and IDs are unchanged.
people = Table("telegram_people", metadata,
    Column("group_id", String(32), primary_key=True), Column("sender_id", String(40), primary_key=True),
    Column("name", Text, nullable=False, default=""), Column("avatar_key", String(200)),
    Column("avatar_identity", String(80)), Column("checked_at", BigInteger, nullable=False, default=0))
emoji_assets = Table("telegram_emoji_assets", metadata,
    Column("group_id", String(32), primary_key=True), Column("document_id", String(32), primary_key=True),
    Column("mime", String(120), nullable=False, default=""), Column("object_key", String(200)), Column("thumb_key", String(200)),
    Column("state", String(20), nullable=False, default="pending"),
    Column("retry_at", BigInteger, nullable=False, default=0))


group_management = Table("group_management", metadata,
    Column("id", String(40), primary_key=True), Column("value", Text, nullable=False))



# TTL belongs to the live source only; frozen snapshots never inherit timers.
expirations = Table("message_expirations", metadata,
    Column("group_id", String(32), primary_key=True), Column("message_id", BigInteger, primary_key=True),
    Column("expires", BigInteger, nullable=False, index=True))
export_jobs = Table("photo_exports", metadata,
    Column("id", String(32), primary_key=True), Column("actor_id", String(100), nullable=False, index=True),
    Column("value", Text, nullable=False), Column("object_key", String(200)))
export_files = Table("photo_export_files", metadata,
    Column("job_id", String(32), primary_key=True), Column("ordinal", Integer, primary_key=True),
    Column("object_key", String(200), nullable=False, index=True), Column("name", Text, nullable=False),
    Column("size", BigInteger, nullable=False))

# Bot operations are separate from imported Telegram history. Immutable blobs
# remain under the same object GC and group deletion rules as originals.
bot_access = Table('telegram_bot_access', metadata,
    Column('group_id',String(32),primary_key=True), Column('value',Text,nullable=False))
site_sends = Table('telegram_site_sends',metadata,
    Column('id',String(32),primary_key=True), Column('group_id',String(32),nullable=False,index=True),
    Column('actor_id',String(100),nullable=False), Column('client_id',String(80),nullable=False),
    Column('author',Text,nullable=False), Column('content_key',String(200),nullable=False),
    Column('pending_key',String(200)), Column('value',Text,nullable=False),
    Column('state',String(24),nullable=False,default='queued',index=True),
    Column('created',BigInteger,nullable=False,default=0,index=True),
    Column('retry_at',BigInteger,nullable=False,default=0),
    UniqueConstraint('group_id','actor_id','client_id'))
site_uploads = Table('telegram_site_uploads',metadata,
    Column('id',String(32),primary_key=True), Column('group_id',String(32),nullable=False,index=True),
    Column('actor_id',String(100),nullable=False), Column('send_id',String(32),index=True),
    Column('object_key',String(200),nullable=False), Column('thumb_key',String(200)),
    Column('value',Text,nullable=False), Column('created',BigInteger,nullable=False))
site_parts = Table('telegram_site_parts',metadata,
    Column('group_id',String(32),primary_key=True), Column('message_id',BigInteger,primary_key=True),
    Column('send_id',String(32),nullable=False,index=True), Column('ordinal',Integer,nullable=False),
    Column('bot_id',BigInteger,nullable=False))


def database(url):
    options = {"pool_pre_ping": True}
    if url.startswith("sqlite:"):
        options["connect_args"] = {"check_same_thread": False, "timeout": 30}
        if url.endswith(":memory:"):
            options["poolclass"] = StaticPool
    engine = create_engine(url, **options)
    if url.startswith("sqlite:"):
        @event.listens_for(engine, "connect")
        def setup(connection, _):
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=30000")
            from .calendar_index import month_key
            connection.create_function('fp_calendar_month', 2, month_key, deterministic=True)
    metadata.create_all(engine)
    from .metadata_cache import track_changes
    track_changes(engine)
    from .calendar_cache import track_calendar_changes
    track_calendar_changes(engine)
    return engine
