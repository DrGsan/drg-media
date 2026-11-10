"""Reusable sender photos and custom emoji; no Telegram URLs reach consumers."""
import time
from pathlib import Path
from sqlalchemy import select, and_
from .db import people, emoji_assets,garbage

class Details:
    def person(self,c,gid,sid):
        value=c.execute(select(people).where(people.c.group_id==gid,people.c.sender_id==str(sid))).mappings().first()
        return {'sender_avatar_version':value['avatar_identity'],'sender_has_avatar':bool(value['avatar_key'])} if value else {}

    def reactor(self,c,gid,person):
        value=c.execute(select(people).where(people.c.group_id==gid,people.c.sender_id==str(person['id']))).mappings().first()
        return {**person,**({'name':value['name'],'avatar_version':value['avatar_identity'],'has_avatar':bool(value['avatar_key'])} if value else {})}

    def emoji(self,c,gid,eid):
        value=c.execute(select(emoji_assets).where(emoji_assets.c.group_id==gid,emoji_assets.c.document_id==str(eid))).mappings().first()
        return {'state':value['state'],'mime':value['mime'],'has_thumbnail':bool(value['thumb_key'])} if value else {'state':'pending'}

    def save_person(self,gid,sid,**values):
        where=and_(people.c.group_id==gid,people.c.sender_id==str(sid))
        with self.transaction() as c:
            self.require_active(c,gid)
            old=c.execute(select(people).where(where)).mappings().first()
            if old:
                if 'avatar_key' in values and old['avatar_key']!=values['avatar_key']:self.gc(c,old['avatar_key'])
                c.execute(people.update().where(where).values(**values))
            else:c.execute(people.insert().values(group_id=gid,sender_id=str(sid),**values))
            if values.get('avatar_key'):c.execute(garbage.delete().where(garbage.c.key==values['avatar_key']))

    def store_person_avatar(self,gid,sid,name,path,identity):
        from .telegram import make_thumbnail
        with self.objects.workspace() as temporary:
            target=Path(temporary)/'avatar.jpg';make_thumbnail(Path(path),target,'photo')
            with self.upload('avatars') as key:
                self.objects.put_file(key,target,'image/jpeg')
                self.save_person(gid,sid,name=name,avatar_key=key,avatar_identity=identity,checked_at=int(time.time()))

    def queue_emoji(self,gid,ids):
        with self.transaction() as c:
            self.require_active(c,gid)
            for eid in set(str(value) for value in ids):
                where=and_(emoji_assets.c.group_id==gid,emoji_assets.c.document_id==eid)
                if not c.execute(select(emoji_assets.c.document_id).where(where)).first():c.execute(emoji_assets.insert().values(group_id=gid,document_id=eid))

    def save_emoji(self,gid,eid,**values):
        where=and_(emoji_assets.c.group_id==gid,emoji_assets.c.document_id==str(eid))
        with self.transaction() as c:
            self.require_active(c,gid)
            old=c.execute(select(emoji_assets).where(where)).mappings().first()
            if not old:return
            for field in ('object_key','thumb_key'):
                if field in values and old[field]!=values[field]:self.gc(c,old[field])
            c.execute(emoji_assets.update().where(where).values(**values))
            for field in ('object_key','thumb_key'):
                if values.get(field):c.execute(garbage.delete().where(garbage.c.key==values[field]))
