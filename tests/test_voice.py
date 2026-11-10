import asyncio
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from family_photo.db import media
from family_photo.telegram import normalize, make_thumbnail, Synchronizer


def voice():
    document = SimpleNamespace(id=987, attributes=[])
    return SimpleNamespace(id=34, date=datetime.now(timezone.utc), sender_id=4,
        get_sender=AsyncMock(return_value=None), voice=document, document=document,
        file=SimpleNamespace(mime_type='audio/ogg', name='voice.ogg', size=123, ext='.ogg', duration=2))


def test_voice_becomes_downloadable_without_losing_duration():
    value = asyncio.run(normalize(voice()))
    assert value['content_type'] == 'voice' and value['duration'] == 2
    assert value['media'] == dict(identity='987', kind='voice', mime='audio/ogg', name='voice.ogg', size=123)


def test_voice_preview_is_scoped_ranged_and_preserves_original(client, archive, tmp_path):
    row = asyncio.run(normalize(voice()))
    archive.ingest('-1001', row)
    with archive.engine.connect() as c: item = dict(c.execute(select(media)).mappings().one())
    original = tmp_path/'voice.ogg'; original.write_bytes(b'original-ogg')
    preview = tmp_path/'voice.m4a'; preview.write_bytes(b'playable-aac')
    archive.objects.put_file('media/'+'a'*32, original, 'audio/ogg')
    archive.objects.put_file('thumbnails/'+'b'*32, preview, 'audio/mp4')
    archive.attach_download(item, 'media/'+'a'*32, 'thumbnails/'+'b'*32, original.stat().st_size)
    entry = client.get('/v1/groups/-1001/messages').json['items'][0]['media'][0]
    assert entry['has_audio_preview'] and not entry['has_thumbnail']
    url = '/v1/groups/-1001/media/'+item['id']
    assert client.get(url).data == b'original-ogg'
    response = client.get(url+'?audio=1', headers={'Range':'bytes=2-6'})
    assert response.status_code == 206 and response.data == b'ayabl'
    assert response.mimetype == 'audio/mp4'
    assert response.headers['Content-Range'] == 'bytes 2-6/12'
    assert client.get(url+'?thumbnail=1').status_code == 404
    assert client.get('/v1/groups/-1002/media/'+item['id']+'?audio=1').status_code == 404
    assert client.get(url+'?audio=1', headers={'Authorization':'Bearer wrong'}).status_code == 401
    archive.delete_messages('-1001', [34])
    assert client.get(url+'?audio=1').status_code == 404


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='ffmpeg unavailable')
def test_real_ogg_transcodes_to_aac_without_modifying_source(tmp_path):
    source = tmp_path/'voice.ogg'; target = tmp_path/'voice.m4a'
    subprocess.run(['ffmpeg','-nostdin','-loglevel','error','-f','lavfi','-i','sine=frequency=440:duration=0.2','-c:a','libopus',str(source)],check=True)
    before = source.read_bytes()
    make_thumbnail(source, target, 'voice')
    assert source.read_bytes() == before
    probe = subprocess.run(['ffprobe','-v','error','-select_streams','a','-show_entries','stream=codec_name','-of','csv=p=0',str(target)],check=True,capture_output=True,text=True)
    assert probe.stdout.strip() == 'aac'


def test_reader_uses_existing_media_queue_for_voice(config, archive, tmp_path, monkeypatch):
    message = voice()
    async def scenario():
        sync = Synchronizer(config, archive, AsyncMock())
        await sync.apply('-1001', message)
        async def download(source, target): target.write_bytes(b'fake-ogg'); return target
        sync.download_media = download
        def convert(source, target, kind):
            assert kind == 'voice'; target.write_bytes(b'fake-aac')
        monkeypatch.setattr('family_photo.telegram.make_thumbnail', convert)
        await sync.downloads()
    asyncio.run(scenario())
    item = archive.history('-1001')['items'][0]['media'][0]
    assert item['kind'] == 'voice' and item['state'] == 'ready' and item['has_audio_preview']
