"""Geometry is persisted once, returned in batch and never read from objects per file."""
import io,json
from PIL import Image
from sqlalchemy import select,event
from conftest import message
from family_photo.db import media,site_uploads
from family_photo.preview_dimensions import backfill,dimensions,image_dimensions


def jpeg(width=360,height=640):
    body=io.BytesIO();Image.new('RGB',(width,height),'#5984a8').save(body,'JPEG');return body.getvalue()


def ready(archive,tmp_path,mid=1,kind='photo'):
    archive.ingest('-1001',message(mid,media={'identity':str(mid),'kind':kind,'mime':'video/mp4' if kind=='video' else 'image/jpeg','name':'synthetic','size':10}))
    with archive.engine.connect() as c:item=dict(c.execute(select(media).where(media.c.message_id==mid)).mappings().one())
    image=tmp_path/('preview'+str(mid)+'.jpg');image.write_bytes(jpeg())
    with archive.upload('media') as original,archive.upload('thumbnails') as thumb:
        archive.objects.put_file(original,image,'image/jpeg');archive.objects.put_file(thumb,image,'image/jpeg')
        assert archive.attach_download(item,original,thumb,image.stat().st_size,dimensions=image_dimensions(image))
    return item['id']


def test_preview_dimensions_are_validated():
    for pair in [(0,1),(-1,1),(1,65536),(True,1),('360',640),(None,None)]:assert dimensions(*pair)=={}
    assert dimensions(360,640)=={'width':360,'height':640}
    assert image_dimensions(io.BytesIO(b'not an image'))=={}


def test_ready_photo_and_video_history_has_geometry_without_object_lookups(client,archive,tmp_path,monkeypatch):
    ready(archive,tmp_path,kind='photo');ready(archive,tmp_path,mid=2,kind='video')
    read=archive.objects.read;calls=[]
    def json_only(key,*args,**kwargs):calls.append(key);return read(key,*args,**kwargs)
    monkeypatch.setattr(archive.objects,'read',json_only)
    response=client.get('/v1/groups/-1001/messages?limit=100');assert response.status_code==200
    assert all((f['width'],f['height'])==(360,640) for m in response.json['items'] for f in m['media'])
    assert len(calls)==2 and all(key.startswith('messages/') for key in calls),'only the existing message JSON reads are needed'


def test_backfill_is_idempotent_and_preserves_ids_bytes_and_failed_sources(archive,tmp_path):
    fid=ready(archive,tmp_path);missing=ready(archive,tmp_path,mid=2)
    with archive.engine.begin() as c:
        original=[dict(r) for r in c.execute(select(media)).mappings()]
        c.execute(media.update().values(width=0,height=0))
        c.execute(media.update().where(media.c.id==missing).values(thumb_key='thumbnails/'+'f'*32))
        c.execute(site_uploads.insert().values(id='a'*32,group_id='-1001',actor_id='actor',object_key=original[0]['object_key'],thumb_key=original[0]['thumb_key'],value=json.dumps({'kind':'photo','name':'synthetic'}),created=1))
    result=backfill(archive.engine,archive.objects,batch_size=1,workers=2)
    assert result['updated']==1 and result['unavailable']==1 and result['uploads_updated']==1
    with archive.engine.connect() as c:
        row=dict(c.execute(select(media).where(media.c.id==fid)).mappings().one());upload=json.loads(c.execute(select(site_uploads.c.value)).scalar_one())
    before=next(r for r in original if r['id']==fid)
    assert {k:v for k,v in row.items() if k not in ['width','height']}=={k:v for k,v in before.items() if k not in ['width','height']}
    assert (row['width'],row['height'])==(360,640) and upload['width']==360 and upload['height']==640
    second=backfill(archive.engine,archive.objects,batch_size=1,workers=2)
    assert second['updated']==0 and second['uploads_updated']==0 and second['examined']==1


def test_additive_upgrade_and_backfill_preserve_legacy_rows_and_old_reader(archive,tmp_path):
    from sqlalchemy import text
    fid=ready(archive,tmp_path)
    legacy=[column for column in media.c if column.name not in ['width','height']]
    with archive.engine.begin() as c:
        before=[tuple(r) for r in c.execute(select(*legacy).order_by(media.c.id))]
        # Reconstruct the preceding schema in this disposable fixture only.
        c.execute(text('ALTER TABLE telegram_media DROP COLUMN width'))
        c.execute(text('ALTER TABLE telegram_media DROP COLUMN height'))
        c.execute(text('ALTER TABLE telegram_media ADD COLUMN width INTEGER NOT NULL DEFAULT 0'))
        c.execute(text('ALTER TABLE telegram_media ADD COLUMN height INTEGER NOT NULL DEFAULT 0'))
        assert [tuple(r) for r in c.execute(select(*legacy).order_by(media.c.id))]==before
        assert c.execute(select(media.c.width,media.c.height).where(media.c.id==fid)).one()==(0,0)
    assert backfill(archive.engine,archive.objects)['updated']==1
    with archive.engine.connect() as c:
        assert [tuple(r) for r in c.execute(select(*legacy).order_by(media.c.id))]==before
        assert c.execute(select(media.c.width,media.c.height).where(media.c.id==fid)).one()==(360,640)
