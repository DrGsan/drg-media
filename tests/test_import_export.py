from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from PIL import Image
from sqlalchemy import func, select

from family_photo.api import create_app
from family_photo.db import messages, media
from family_photo.import_export import read_export, import_export, exported_date
from family_photo.telegram import Synchronizer


@pytest.fixture
def exported(tmp_path):
    root = tmp_path / 'export'
    (root / 'photos').mkdir(parents=True)
    Image.new('RGB', (64, 48), 'red').save(root / 'photos' / 'one.jpg')
    (root / 'video_files').mkdir()
    (root / 'video_files' / 'clip.mov').write_bytes(b'fixture video')
    (root / 'messages.html').write_text('''<!DOCTYPE html><html><head><script>throw Error('never execute')</script></head><body>
    <div class="page_header"><div class="name bold">Семейная группа</div></div>
    <div class="history"><div class="message service"><div>Дата</div></div>
    <div class="message default" id="message4"><div class="body">
      <div class="date details" title="21 апреля 2023, 00:30:40">00:30</div>
      <div class="from_name">Анна</div><a class="photo_wrap" href="photos/one.jpg"><img src="photos/one.jpg"></a>
      <div class="text">Первая строка<br>Вторая &amp; <a href="https://example.com">ссылка</a></div></div></div>
    <div class="message default joined" id="message5"><div class="body">
      <div class="date details" title="21 апреля 2023, 00:31:40">00:31</div>
      <div class="reply_to"><a href="#go_to_message4">Ответ</a></div><div class="text">Привет</div></div></div>
    <div class="message default joined" id="message6"><div class="body">
      <div class="date details" title="21 апреля 2023, 00:32:40">00:32</div>
      <a class="video_file_wrap" href="video_files/clip.mov"><img src="photos/one.jpg"></a></div></div>
    <div class="message default joined" id="message7"><div class="body">
      <div class="date details" title="21 апреля 2023, 00:33:40">00:33</div>
      <div class="media_wrap">Голосовое сообщение</div></div></div>
    </div></body></html>''', encoding='utf-8')
    return root


def test_html_preserves_sender_captions_replies_and_counts_only_original_media(exported):
    result = read_export(exported, 'Europe/Moscow')
    assert result.title == 'Семейная группа'
    assert result.report['messages'] == 4
    assert (result.report['photos'], result.report['videos'], result.report['unsupported_attachments']) == (1, 1, 1)
    assert result.report['service_events_skipped'] == 1
    assert result.records[0]['text'] == 'Первая строка\nВторая & ссылка'
    assert result.records[1]['sender'] == 'Анна' and result.records[1]['reply_to'] == 4
    assert result.records[0]['created'] == int(datetime(2023, 4, 20, 21, 30, 40, tzinfo=timezone.utc).timestamp())
    assert result.records[2]['_preview'] == exported / 'photos/one.jpg'
    assert read_export(exported).group_id == result.group_id


def test_visual_documents_round_videos_and_stickers_are_imported(exported, archive):
    page = exported / 'messages.html'
    extra = ''.join(f'''<div class="message default" id="message{mid}"><div class="date" title="2023-04-21T01:00:00"></div>
        <div class="from_name">Анна</div><div class="media_wrap">{link}</div></div>''' for mid, link in [
        (8, '<a class="media media_file" href="photos/one.jpg"></a>'),
        (9, '<a class="media media_file" href="video_files/clip.mov"><img src="photos/one.jpg"></a>'),
        (10, '<a class="media media_video" href="video_files/clip.mov"><img src="photos/one.jpg"></a>'),
        (11, '<div class="sticker_wrap"><a class="media media_file" href="photos/one.jpg"></a></div>')])
    page.write_text(page.read_text().replace('</body>', extra + '</body>'))
    source = read_export(exported)
    assert (source.report['photos'], source.report['videos'], source.report['stickers'], source.report['unsupported_attachments']) == (2, 3, 1, 1)
    result = import_export(source, archive)
    assert result['media_copied'] == 6
    with archive.engine.connect() as c:
        ids = set(c.execute(select(media.c.message_id)).scalars())
    assert {8, 9, 10, 11} <= ids
    assert archive.history(source.group_id)['items'][-1]['content_type']=='sticker'


@pytest.mark.parametrize('value', ['21.04.2023 00:30:40', '21 April 2023, 00:30:40', '2023-04-21T00:30:40'])
def test_export_dates_have_explicit_timezone(value):
    assert exported_date(value, ZoneInfo('Europe/Moscow')) == 1682026240


def test_reimport_reuses_objects_and_changed_media_replaces_original(exported, archive):
    source = read_export(exported, 'Europe/Moscow')
    first = import_export(source, archive)
    second = import_export(source, archive)
    assert first['media_copied'] == 2 and second['media_reused'] == 2 and second['media_copied'] == 0
    with archive.engine.connect() as c:
        assert c.execute(select(func.count()).select_from(messages)).scalar_one() == 4
        assert c.execute(select(func.count()).select_from(media)).scalar_one() == 2
        item = dict(c.execute(select(media).where(media.c.message_id == 4)).mappings().one())
    assert b''.join(archive.objects.read(item['object_key'])) == (exported / 'photos/one.jpg').read_bytes()
    Image.new('RGB', (64, 48), 'blue').save(exported / 'photos/one.jpg')
    third = import_export(source, archive)
    assert third['media_copied'] == 1 and third['media_reused'] == 1
    archive.delete_messages(source.group_id, [4])
    import_export(source, archive)
    assert [r['id'] for r in archive.history(source.group_id)['items']] == [5, 6, 7]


def test_interrupted_file_copy_resumes_without_duplicates(exported, archive, monkeypatch):
    source = read_export(exported)
    original = archive.objects.put_file
    def fail(key, *args, **kwargs):
        if key.startswith('media/'):
            raise OSError('simulated interruption')
        return original(key, *args, **kwargs)
    monkeypatch.setattr(archive.objects, 'put_file', fail)
    with pytest.raises(OSError):
        import_export(source, archive)
    assert archive.group(source.group_id)['state'] == 'importing'
    monkeypatch.setattr(archive.objects, 'put_file', original)
    result = import_export(source, archive)
    assert result['media_copied'] == 2
    assert archive.group(source.group_id)['state'] == 'imported'
    assert len(archive.history(source.group_id)['items']) == 4


@pytest.mark.parametrize('reference', ['../outside.jpg', '%2e%2e/outside.jpg', '/etc/passwd', 'https://example.com/image.jpg', 'photos/missing.jpg'])
def test_invalid_attachment_fails_during_preflight(exported, reference):
    page = exported / 'messages.html'
    page.write_text(page.read_text().replace('href="photos/one.jpg"', 'href="' + reference + '"'))
    with pytest.raises(ValueError):
        read_export(exported)


def test_symlink_cannot_escape_export(exported, tmp_path):
    outside = tmp_path / 'private.jpg'
    outside.write_bytes(b'private')
    photo = exported / 'photos/one.jpg'
    photo.unlink()
    photo.symlink_to(outside)
    with pytest.raises(ValueError):
        read_export(exported)


def test_exports_are_readable_without_live_group_ids_but_cannot_send(exported, archive, config):
    source = read_export(exported)
    import_export(source, archive)
    isolated = replace(config, group_ids=())
    client = create_app(isolated, archive).test_client()
    headers = {'Authorization': 'Bearer ' + config.service_token}
    groups = client.get('/v1/groups', headers=headers).json['items']
    assert [g['id'] for g in groups] == [source.group_id]
    assert groups[0]['can_send'] is False
    history = client.get(f'/v1/groups/{source.group_id}/messages', headers=headers)
    assert history.status_code == 200 and len(history.json['items']) == 4
    assert client.post(f'/v1/groups/{source.group_id}/messages', headers=headers,
        json={'actor_id':'a', 'author':'Анна', 'client_id':'a', 'text':'hello'}).status_code == 422
    assert client.get('/v1/groups/-1001/messages', headers=headers).status_code == 404


def test_worker_never_downloads_offline_archive_media(exported, archive, config):
    import asyncio
    source = read_export(exported)
    archive.add_group(source.group_id, source.title, kind='export')
    record = {k:v for k,v in source.records[0].items() if not k.startswith('_')}
    record['media'] = dict(identity='export:fixture',kind='photo',mime='image/jpeg',name='one.jpg',size=1)
    archive.ingest(source.group_id, record)
    client = AsyncMock()
    asyncio.run(Synchronizer(config, archive, client).downloads())
    client.get_messages.assert_not_called()


def test_import_namespace_cannot_be_a_live_group(exported):
    with pytest.raises(ValueError):
        read_export(exported, archive_id='-1001')


def test_export_group_avatar_is_private_idempotent_and_not_a_gallery_photo(exported, archive, client):
    page=exported/'messages.html'
    page.write_text(page.read_text().replace('<div class="page_header">','<div class="page_header"><img class="userpic" src="photos/one.jpg">'))
    source=read_export(exported)
    assert source.avatar==exported/'photos/one.jpg'
    import_export(source,archive)
    first=archive.group_profile(source.group_id)
    assert first['avatar_key'] and first['avatar_identity'].startswith('export:')
    import_export(source,archive)
    assert archive.group_profile(source.group_id)['avatar_key']==first['avatar_key']
    assert client.get(f'/v1/groups/{source.group_id}/avatar').status_code==200
    with archive.engine.connect() as c:
        assert c.execute(select(func.count()).select_from(media).where(media.c.group_id==source.group_id)).scalar()==2
    page.write_text(page.read_text().replace('class="userpic" src="photos/one.jpg"','class="userpic" src="../outside.jpg"'))
    with pytest.raises(ValueError):read_export(exported)


def test_html_bursts_include_one_second_video_gap_and_split_at_ten():
    from family_photo.import_export import infer_export_albums
    def row(mid,**kw):return dict(id=mid,created=1700000000,sender_id='a',text='',_file='fixture',_kind='photo')|kw
    rows=[row(1,text='Caption'),row(2,created=1700000001)]
    infer_export_albums(rows);assert rows[0]['album_id']==rows[1]['album_id']
    rows=[row(i) for i in range(1,23)];infer_export_albums(rows)
    assert [sum(r.get('album_id')=='export:'+str(i) for r in rows) for i in (1,11,21)]==[10,10,2]
    for changes in ({'created':1700000004},{'sender_id':'b'},{'text':'Different caption'},{'reply_to':8},{'id':4}):
        rows=[row(1),row(2,**changes)];infer_export_albums(rows)
        assert not any(r.get('album_id') for r in rows)
    rows=[row(1),dict(id=2,created=1700000000,sender_id='a',text='Separate text'),row(3)]
    infer_export_albums(rows);assert not any(r.get('album_id') for r in rows)


def test_cli_import_registers_new_export_after_existing_registry(config,archive,client,exported,monkeypatch):
    from family_photo.cli import main
    from family_photo.config import Config
    from family_photo.group_management import Registry
    import sys
    monkeypatch.setattr(Config,'from_env',lambda:config)
    monkeypatch.setattr(sys,'argv',['family-photo','import-export',str(exported)])
    main()
    source=read_export(exported)
    state=archive.registry.read()
    assert source.group_id in state['groups']
    assert state['groups'][source.group_id]['enabled']
    assert client.get('/v1/groups/'+source.group_id+'/messages').status_code==200


def test_html_animations_are_chat_media_and_never_video_or_photo(exported,archive):
    page=exported/'messages.html'
    page.write_text(page.read_text().replace('class="video_file_wrap"','class="animated_wrap"'))
    source=read_export(exported)
    assert source.report['animations']==1 and source.report['videos']==0
    result=import_export(source,archive)
    assert result['media_copied']==2
    with archive.engine.connect() as c:assert c.execute(select(media.c.kind).where(media.c.message_id==6)).scalar_one()=='animation'
    # Re-import reuses the original, just as a frozen snapshot does.
    assert import_export(source,archive)['media_reused']==2


def test_html_photo_with_false_gif_extension_is_still_a_jpeg(exported):
    (exported/'photos/one.gif').write_bytes((exported/'photos/one.jpg').read_bytes())
    page=exported/'messages.html';page.write_text(page.read_text().replace('photos/one.jpg','photos/one.gif'))
    result=read_export(exported)
    assert result.records[0]['_kind']=='photo' and result.records[0]['_mime']=='image/jpeg'
    assert result.report['animations']==0
