import os
import sys
import tempfile
from pathlib import Path
from sqlalchemy import select
site_root=Path(os.getenv('FP_SITE_REPO', str(Path(__file__).resolve().parents[3]/'drg-space')))
from family_photo.config import Config
from family_photo.db import database,media,outbox
from family_photo.objects import Objects
from family_photo.archive import Archive
from family_photo.api import create_app

root=Path(tempfile.mkdtemp(prefix='fp-browser-'))
config=Config(root,'sqlite:///'+str(root/'archive.sqlite3'),'browser-service-token-'*3,
              group_ids=('-1001','-1002'),bot_token='fixture-only-never-connect')
archive=Archive(database(config.database_url),Objects(config))
archive.add_group('-1001','Семья Ивановых',kind='supergroup');archive.add_group('-1002','Семья Петровых',kind='supergroup')
archive.add_group('export:abcdef0123456789','Экспорт семейной группы',kind='export')
for mid in range(1,8):
    gid='-1001' if mid<5 else '-1002' if mid<7 else 'export:abcdef0123456789'
    archive.ingest(gid,dict(id=mid,created=1700000000+mid*86400,edited=0,sender_id='42',
        sender='Анна Иванова' if mid<5 else 'Пётр Петров',text=f'Семейное сообщение {mid}',
        album_id='77' if mid<3 else None,
        media=dict(identity=str(mid),kind='photo',mime='image/png',name=f'photo-{mid}.png',size=100)))
    with archive.engine.connect() as c:item=dict(c.execute(select(media).where(media.c.message_id==mid)).mappings().one())
    fixture=site_root/'tests/fixtures'/('sand.png' if mid%2 else 'blue.png')
    key='media/'+item['id'];archive.objects.put_file(key,fixture,'image/png');archive.attach_download(item,key,None,fixture.stat().st_size)
for gid in config.group_ids:archive.update_group(gid,history_complete=True,state='online')
archive.update_group('export:abcdef0123456789',history_complete=True,state='imported')
archive.heartbeat('online')
archive.store_avatar('export:abcdef0123456789',site_root/'tests/fixtures/blue.png','fixture-blue')
app=create_app(config,archive)
@app.post('/_test/identity')
def identity():
    archive.add_group('export:abcdef0123456789','Обновлённая группа')
    archive.store_avatar('export:abcdef0123456789',site_root/'tests/fixtures/sand.png','fixture-sand')
    return {'ok':True}
@app.post('/_test/delete/<int:mid>')
def remove(mid):archive.delete_messages('-1001',[mid]);return {'ok':True}
app.run(host='127.0.0.1',port=8097,threaded=True)
