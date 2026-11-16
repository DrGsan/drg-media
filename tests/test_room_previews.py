"""The room catalog needs one bounded batch, not a history call per group."""
import asyncio
from sqlalchemy import event
import pytest
from conftest import message, photo
from test_site_messaging import messaging, send, echo


@pytest.mark.parametrize('total', [1, 10, 50])
def test_room_previews_match_histories_without_per_group_sql(archive, app, total):
    catalog = []
    for index in range(total):
        gid = str(-2000-index)
        archive.registry.import_source(gid)
        archive.add_group(gid, 'Synthetic '+str(index), kind='supergroup')
        archive.ingest(gid, photo(1, text='Text '+str(index)))
        archive.ingest(gid, message(2, text='Latest '+str(index)))
        archive.ingest(gid, message(3, text='Deleted'))
        archive.delete_messages(gid, [3])
        catalog.append({'id': gid, 'latest_message_id': 2})
    calls = []
    def counted(c, cur, sql, params, context, many): calls.append(sql)
    event.listen(archive.engine, 'before_cursor_execute', counted)
    try: result = archive.room_previews(catalog)
    finally: event.remove(archive.engine, 'before_cursor_execute', counted)
    assert len(calls) <= 10
    for group in catalog:
        latest = archive.history(group['id'], limit=1)['items'][-1]
        assert result[group['id']] == {key: latest[key] for key in ('id','created','sender','text')} | {
            'content_type': latest.get('content_type'), 'media': []}
    assert archive.room_previews([{'id':'-1001','latest_message_id':0}]) == {'-1001':None}


def test_catalog_previews_are_opt_in_and_follow_active_groups(client, archive):
    archive.ingest('-1001', photo(1))
    plain = client.get('/v1/groups').json['items']
    assert all('last_message' not in group for group in plain)
    groups = client.get('/v1/groups?previews=1').json['items']
    assert groups[0]['last_message']['media'] == [{'kind':'photo','name':'photo.jpg'}]
    assert groups[1]['last_message'] is None
    archive.registry.action('-1001', 'disable')
    assert [g['id'] for g in client.get('/v1/groups?previews=1').json['items']] == ['-1002']


def test_catalog_uses_site_author_and_original_text(client, archive, messaging):
    service, bot = messaging
    job = send(service, 'Site text')
    asyncio.run(service.send(job['id'], bot))
    echo(archive, service.get(job['id']))
    latest = archive.history('-1001', limit=1)['items'][-1]
    preview = next(g['last_message'] for g in client.get('/v1/groups?previews=1').json['items'] if g['id']=='-1001')
    assert preview['sender'] == latest['sender'] == 'Анна'
    assert preview['text'] == latest['text'] == 'Site text'
