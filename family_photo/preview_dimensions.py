"""Preview geometry stored with media; request-time serializers never open objects."""
import io
from PIL import Image


def dimensions(width, height):
    if type(width) is int and type(height) is int and 0 < width <= 65535 and 0 < height <= 65535:
        return {'width': width, 'height': height}
    return {}


def image_dimensions(source):
    try:
        with Image.open(source) as image:
            return dimensions(*image.size)
    except (OSError, ValueError, Image.DecompressionBombError):
        return {}


def object_dimensions(objects, key):
    """Read a thumbnail header only; never download or decode an original video."""
    stream = objects.read(key, end=512 * 1024 - 1)
    body = bytearray()
    try:
        for block in stream:
            body.extend(block)
            if result := image_dimensions(io.BytesIO(body)):
                return result
            if len(body) >= 512 * 1024:
                break
    finally:
        stream.close()
    return {}


def backfill(engine, objects, *, batch_size=128, workers=4):
    """One-time bounded fill of missing metadata; IDs and bytes remain unchanged."""
    from concurrent.futures import ThreadPoolExecutor
    from botocore.exceptions import BotoCoreError, ClientError
    from sqlalchemy import select, update, bindparam, or_
    from .db import media
    cursor = ''
    result = {'examined': 0, 'updated': 0, 'unavailable': 0}
    missing = or_(media.c.width == 0, media.c.height == 0)
    def inspect(row):
        try:
            return row, object_dimensions(objects, row['thumb_key'])
        except (OSError, ValueError, RuntimeError, BotoCoreError, ClientError):
            return row, {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while True:
            with engine.connect() as c:
                rows = list(c.execute(select(media.c.id, media.c.thumb_key).where(
                    media.c.id > cursor, missing, media.c.state == 'ready',
                    media.c.kind.in_(['photo', 'video', 'animation']), media.c.thumb_key.is_not(None)
                ).order_by(media.c.id).limit(batch_size)).mappings())
            if not rows:
                break
            cursor = rows[-1]['id']; changes = []
            for row, size in pool.map(inspect, rows):
                result['examined'] += 1
                if size:
                    changes.append({'target_id': row['id'], 'expected_thumb': row['thumb_key'], 'w': size['width'], 'h': size['height']})
                else:
                    result['unavailable'] += 1
            if changes:
                with engine.begin() as c:
                    applied = c.execute(update(media).where(media.c.id == bindparam('target_id'),
                        media.c.thumb_key == bindparam('expected_thumb'), missing).values(width=bindparam('w'), height=bindparam('h')), changes)
                    result['updated'] += applied.rowcount
    # Old site uploads are presented from their existing JSON value, not media rows.
    import json
    from .db import site_uploads
    cursor = ''; result.update(uploads_examined=0, uploads_updated=0)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while True:
            with engine.connect() as c:
                rows = list(c.execute(select(site_uploads.c.id, site_uploads.c.thumb_key, site_uploads.c.value).where(
                    site_uploads.c.id > cursor, site_uploads.c.thumb_key.is_not(None)
                ).order_by(site_uploads.c.id).limit(batch_size)).mappings())
            if not rows:break
            cursor = rows[-1]['id']
            missing_rows = [row for row in rows if not dimensions(json.loads(row['value']).get('width'), json.loads(row['value']).get('height'))]
            changes = []
            for row, size in pool.map(inspect, missing_rows):
                result['uploads_examined'] += 1
                if size:
                    changes.append({'target_id':row['id'], 'expected_thumb':row['thumb_key'], 'old_value':row['value'],
                                    'new_value':json.dumps(json.loads(row['value']) | size)})
                else:result['unavailable'] += 1
            if changes:
                with engine.begin() as c:
                    applied = c.execute(update(site_uploads).where(site_uploads.c.id==bindparam('target_id'),
                        site_uploads.c.thumb_key==bindparam('expected_thumb'), site_uploads.c.value==bindparam('old_value'))
                        .values(value=bindparam('new_value')), changes)
                    result['uploads_updated'] += applied.rowcount
    return result


if __name__ == '__main__':
    import argparse, json
    from .config import Config
    from .db import database
    from .objects import Objects
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backfill', action='store_true', required=True)
    args = parser.parse_args()
    cfg = Config.from_env(); engine = database(cfg.database_url)
    try:
        print(json.dumps(backfill(engine, Objects(cfg))))
    finally:
        engine.dispose()
