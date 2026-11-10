import subprocess
from pathlib import Path
from sqlalchemy import select
from PIL import Image
from family_photo.api import create_app
from family_photo.db import media
from family_photo.telegram import make_thumbnail

app=create_app()
archive=app.extensions['archive']
assert app.test_client().get('/v1/groups').status_code==401
client=app.test_client()
client.environ_base['HTTP_AUTHORIZATION']='Bearer container-test-only-service-token-1234567890'
assert client.get('/v1/status').json['api_version']==1
registry=archive.registry
registry.request_discovery()
registry.publish_discovery([{'id':'-1001','title':'Container test family','kind':'supergroup'}])
assert client.post('/v1/admin/groups',json={'telegram_id':'-1001'}).status_code==201
original=Path('/data/photo.png');Image.new('RGB',(128,128),'blue').save(original)
thumbnail=Path('/data/thumbnail.jpg');make_thumbnail(original,thumbnail,'photo')
assert thumbnail.stat().st_size>0
archive.ingest('-1001',dict(id=1,created=1700000000,edited=0,sender_id='42',sender='Test sender',text='Container smoke',
    media=dict(identity='42',kind='photo',mime='image/png',name='test.png',size=original.stat().st_size)))
with archive.engine.connect() as connection:item=dict(connection.execute(select(media)).mappings().one())
key,thumb=archive.reserve('media'),archive.reserve('thumbnails')
archive.objects.put_file(key,original,'image/png');archive.objects.put_file(thumb,thumbnail,'image/jpeg')
assert archive.attach_download(item,key,thumb,original.stat().st_size)
assert client.get('/v1/groups/-1001/messages').json['items'][0]['text']=='Container smoke'
url='/v1/groups/-1001/media/'+item['id']
assert client.get(url,headers={'Range':'bytes=2-5'}).status_code==206
assert client.get(url+'?thumbnail=1').status_code==200
archive.delete_messages('-1001',[1]);archive.collect_garbage()
assert client.get(url).status_code==404
video=Path('/data/video.mp4')
subprocess.run(['ffmpeg','-nostdin','-loglevel','error','-f','lavfi','-i','color=c=blue:s=128x128:d=0.2',
                '-c:v','libx264','-y',str(video)],check=True)
cover=Path('/data/video-cover.jpg');make_thumbnail(video,cover,'video')
assert cover.stat().st_size>0
print('Docker smoke passed: non-root startup, SQLite, private API, object storage, thumbnails, HTTP Range, deletion, FFmpeg video cover.')
