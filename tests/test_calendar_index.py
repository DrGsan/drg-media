from datetime import datetime, timezone
import pytest
from sqlalchemy import event
from family_photo.db import messages, media
from conftest import photo


def stamp(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp())


def test_index_has_all_months_without_reading_objects_and_seek_skips_newer_history(client, archive, monkeypatch):
    for mid, date in [(1, '2017-09-02'), (2, '2018-06-01'), (3, '2018-06-28'), (4, '2026-11-01')]:
        archive.ingest('-1001', photo(mid, created=stamp(date)))
    archive.ingest('-1002', photo(50, created=stamp('2010-01-01')))
    monkeypatch.setattr(archive.objects, 'read', lambda *a: pytest.fail('index reads originals'))
    monkeypatch.setattr(archive.objects, 'read_json', lambda *a: pytest.fail('index reads content blobs'))
    index = client.get('/v1/groups/-1001/media-index?timezone=UTC').json
    assert [(x['key'], x['count']) for x in index['items']] == [('2026-11', 1), ('2018-06', 2), ('2017-09', 1)]
    june = index['items'][1]
    page = client.get(f"/v1/groups/-1001/media?until={june['end']}&limit=2").json
    assert [f['message_id'] for f in page['items']] == [3, 2]
    assert page['more'] is True
    assert client.get(f"/v1/groups/-1001/media?until={june['end']}&before=2").json['items'][0]['message_id'] == 1
    assert client.get('/v1/status').json['gallery_index'] == 1
    assert client.get('/v1/groups/-999/media-index').status_code == 404
    assert client.application.test_client().get('/v1/groups/-1001/media-index').status_code == 401
    assert client.get('/v1/groups/-1001/media-index?timezone=invalid-zone').status_code == 422
    assert client.get('/v1/groups/-1001/media?until=bad').status_code == 422


def test_months_follow_device_timezone_and_dst(client, archive):
    archive.ingest('-1001', photo(1, created=stamp('2026-03-01T00:30:00')))
    utc = client.get('/v1/groups/-1001/media-index?timezone=UTC').json['items']
    la = client.get('/v1/groups/-1001/media-index?timezone=America/Los_Angeles').json['items']
    assert utc[0]['key'] == '2026-03'
    assert la[0]['key'] == '2026-02'
    archive.ingest('-1001', photo(2, created=stamp('2026-03-20T12:00:00')))
    march = client.get('/v1/groups/-1001/media-index?timezone=America/Los_Angeles').json['items'][0]
    assert march['end'] - march['start'] == 31 * 86400 - 3600


def test_cache_reuses_index_per_group_and_invalidates_transactionally(client, archive):
    archive.ingest('-1001', photo(1, created=stamp('2026-01-01')))
    queries = []
    @event.listens_for(archive.engine, 'before_cursor_execute')
    def count(c, cursor, sql, params, context, many):
        if 'GROUP BY' in sql.upper() and 'telegram_media' in sql:
            queries.append(sql)
    url = '/v1/groups/-1001/media-index?timezone=UTC'
    assert client.get(url).json['items'][0]['count'] == 1
    assert client.get(url).json['items'][0]['count'] == 1
    assert len(queries) == 1
    archive.ingest('-1002', photo(1))
    client.get(url)
    assert len(queries) == 1, 'another group must not invalidate this index'
    with archive.engine.begin() as c:
        c.execute(media.update().where(media.c.group_id == '-1001').values(state='ready'))
    client.get(url)
    assert len(queries) == 1, 'download progress does not affect the calendar'
    with pytest.raises(RuntimeError), archive.engine.begin() as c:
        c.execute(messages.update().where(messages.c.group_id == '-1001').values(deleted=True))
        raise RuntimeError('rollback')
    assert client.get(url).json['items'][0]['count'] == 1
    assert len(queries) == 1
    archive.delete_messages('-1001', [1])
    assert client.get(url).json['items'] == []
    assert len(queries) == 2


def test_cache_uses_shared_redis_and_survives_outage(client, app, archive, monkeypatch):
    from family_photo.calendar_cache import CalendarCache
    shared = app.extensions['metadata_cache']
    data = {}
    class Redis:
        def get(self, key): return data.get(key)
        def set(self, key, value, ex): data[key] = value
    monkeypatch.setattr(shared, 'url', 'redis://test.invalid')
    monkeypatch.setattr(shared, '_client', lambda: Redis())
    archive.ingest('-1001', photo(1))
    first = client.get('/v1/groups/-1001/media-index').json
    assert len(data) == 1
    cache = CalendarCache(archive.engine, shared)
    assert cache.load('-1001', 'UTC', lambda: pytest.fail('Redis hit recomputes index')) == first
    class Broken:
        def get(self, key): raise OSError('unavailable')
    monkeypatch.setattr(shared, '_client', lambda: Broken())
    archive.delete_messages('-1001', [1])
    assert client.get('/v1/groups/-1001/media-index').json['items'] == []


def test_simultaneous_misses_are_coalesced_and_dates_invalidate(client, app, archive):
    import hashlib
    import json
    import time
    from concurrent.futures import ThreadPoolExecutor
    from threading import Lock
    cache = app.extensions['calendar_cache']
    calls = []
    lock = Lock()
    def load():
        with lock: calls.append(1)
        time.sleep(.05)
        return {'items': [], 'version': hashlib.sha256(json.dumps([], sort_keys=True).encode()).hexdigest(), 'timezone': 'UTC'}
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda _: cache.load('-1001', 'UTC', load), range(8)))
    assert len(calls) == 1
    assert all(v == values[0] for v in values)
    archive.ingest('-1001', photo(1, created=stamp('2026-01-01')))
    assert client.get('/v1/groups/-1001/media-index').json['items'][0]['key'] == '2026-01'
    with archive.engine.begin() as c:
        c.execute(media.update().where(media.c.group_id == '-1001').values(created=stamp('2025-02-01')))
    assert client.get('/v1/groups/-1001/media-index').json['items'][0]['key'] == '2025-02'


def test_ten_thousand_photos_and_five_hundred_cached_requests(client, app, archive):
    from concurrent.futures import ThreadPoolExecutor
    rows = [{'group_id': '-1001', 'id': i + 1, 'created': stamp(f'{2010+i%15}-01-01'), 'sender_id': '1', 'sender': 'Family'} for i in range(10000)]
    assets = [{'id': f'{i+1:032x}', 'group_id': '-1001', 'message_id': i+1, 'created': row['created'], 'identity': str(i+1), 'kind': 'photo', 'mime': 'image/jpeg', 'name': 'photo.jpg'} for i, row in enumerate(rows)]
    with archive.engine.begin() as c:
        c.execute(messages.insert(), rows)
        c.execute(media.insert(), assets)
    result = client.get('/v1/groups/-1001/media-index?timezone=UTC').json
    assert sum(m['count'] for m in result['items']) == 10000
    cache = app.extensions['calendar_cache']
    with ThreadPoolExecutor(max_workers=16) as pool:
        replies = list(pool.map(lambda _: cache.load('-1001', 'UTC', lambda: pytest.fail('hot cache recomputed')), range(500)))
    assert all(reply == result for reply in replies)
