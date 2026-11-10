"""Correct media metadata without replacing shared originals or frozen history."""
import json

from sqlalchemy import select, or_
from .db import media, export_files, export_jobs


def image_type(header):
    if header.startswith((b'GIF87a', b'GIF89a')): return dict(kind='animation', mime='image/gif')
    if header.startswith(b'\xff\xd8\xff'): return dict(kind='photo', mime='image/jpeg')
    if header.startswith(b'\x89PNG\r\n\x1a\n'): return dict(kind='photo', mime='image/png')
    if header.startswith(b'RIFF') and header[8:12] == b'WEBP': return dict(kind='photo', mime='image/webp')


def verify_gif(archive, item, header):
    """Some Telegram documents advertise image/gif but contain a still JPEG."""
    value = image_type(header)
    if not value: return None
    if value['kind'] == 'animation':
        classify(archive, [item])
    else:
        with archive.transaction() as c:
            registry = getattr(archive, 'registry', None)
            if registry: registry.read(c, lock=True)
            same = media.c.identity == item['identity']
            if item.get('object_key'): same = or_(same, media.c.object_key == item['object_key'])
            c.execute(media.update().where(same, media.c.mime == 'image/gif',
                media.c.kind.in_(['photo', 'animation'])).values(**value))
    return value


def classify(archive, confirmations):
    """Confirmations come from Telegram attributes or a verified GIF signature.

    Telegram document IDs are stable across forwards. Copies can also share an
    object while retaining an older export identity. Neither names nor duration
    prove animation; a normal silent MP4 must remain a video.
    """
    changed = []
    with archive.transaction() as c:
        registry = getattr(archive, 'registry', None)
        if registry: registry.read(c, lock=True)
        for item in confirmations:
            same = media.c.identity == item['identity']
            if item.get('object_key'): same = or_(same, media.c.object_key == item['object_key'])
            rows = list(c.execute(select(media).where(same, media.c.mime == item['mime'],
                media.c.kind.in_(['photo', 'video']))).mappings())
            shared = {row['object_key'] for row in rows if row['object_key']}
            if shared:
                rows = list(c.execute(select(media).where(or_(same, media.c.object_key.in_(shared)),
                    media.c.mime == item['mime'], media.c.kind.in_(['photo', 'video']))).mappings())
            changed.extend(dict(row) for row in rows)
            if rows:
                c.execute(media.update().where(media.c.id.in_([row['id'] for row in rows])).values(kind='animation'))
        if changed:
            # An old prepared ZIP has no source pins left. Retire conservatively
            # for affected groups instead of serving an archive with wrong types.
            from .exports import Exports
            keys = {row['object_key'] for row in changed if row['object_key']}
            for row in c.execute(select(export_jobs).with_for_update()).mappings():
                job = json.loads(row['value']) | dict(id=row['id'], actor_id=row['actor_id'], object_key=row['object_key'])
                if job['state'] not in {'queued', 'running', 'ready'}: continue
                affected = any(item['group_id'] in job['groups'] and
                    (item['kind'] == 'photo' or job['videos']) for item in changed)
                pinned = bool(keys and c.execute(select(export_files.c.job_id).where(
                    export_files.c.job_id == row['id'], export_files.c.object_key.in_(keys)).limit(1)).first())
                if pinned or affected and job['state'] == 'ready':
                    Exports.retire_in(archive, c, job, 'media_type_corrected')
    return changed
