"""Bounded presentation data for one authorized history page, never a shared cache."""
import json
from collections import defaultdict
from sqlalchemy import select
from .db import people, emoji_assets, media, outbox, site_parts, site_sends, site_uploads, group_management


class HistoryPage:
    def __init__(self, archive, connection, rows):
        self.rows = {(row['group_id'], row['id']):row for row in rows}
        self.contents = {key:archive.objects.read_json(row['content_key']) for key,row in self.rows.items()}
        ids, senders, emojis = defaultdict(set), defaultdict(set), defaultdict(set)
        for (gid, mid), row in self.rows.items():
            ids[gid].add(mid)
            senders[gid].add(str(row['sender_id']))
            content = self.contents[gid, mid]
            emojis[gid].update(str(entry['document_id']) for entry in content.get('custom_emojis', []))
            for reaction in content.get('reactions', []):
                senders[gid].update(str(person['id']) for person in reaction.get('people', []))
                if reaction.get('document_id'):
                    emojis[gid].add(str(reaction['document_id']))

        def scoped(table, column, groups, condition=None):
            for gid, identities in groups.items():
                ordered = sorted(identities)
                for start in range(0, len(ordered), 256):
                    query = select(table).where(table.c.group_id==gid, column.in_(ordered[start:start+256]))
                    if condition is not None:
                        query = query.where(condition)
                    yield from connection.execute(query).mappings()

        self.people = {(row['group_id'], row['sender_id']):row for row in scoped(people, people.c.sender_id, senders)}
        self.emojis = {(row['group_id'], row['document_id']):row for row in scoped(emoji_assets, emoji_assets.c.document_id, emojis)}
        self.media = defaultdict(list)
        for row in scoped(media, media.c.message_id, ids):
            self.media[row['group_id'], row['message_id']].append(row)
        self.authors = {}
        for row in scoped(outbox, outbox.c.message_id, ids, outbox.c.state=='delivered'):
            self.authors.setdefault((row['group_id'], row['message_id']), row['author'])

        self.parts, self.sends, self.uploads, self.reactions = {}, {}, {}, {}
        if not getattr(archive, 'site_messaging', None):
            return
        self.parts = {(row['group_id'], row['message_id']):row for row in scoped(site_parts, site_parts.c.message_id, ids)}
        sends = defaultdict(set)
        for key, part in self.parts.items():
            if str(part['bot_id'])==self.rows[key]['sender_id']:
                sends[key[0]].add(part['send_id'])
        for row in scoped(site_sends, site_sends.c.id, sends):
            self.sends[row['id']] = dict(row) | json.loads(row['value'])
        uploads = defaultdict(set)
        for key, part in self.parts.items():
            if str(part['bot_id'])!=self.rows[key]['sender_id']:
                continue
            job = self.sends.get(part['send_id'])
            if job:
                index = archive.site_messaging.attachment_index(job, part['ordinal'])
                if index is not None:
                    uploads[key[0]].add(job['attachments'][index])
        self.uploads = {row['id']:row for row in scoped(site_uploads, site_uploads.c.id, uploads)}
        from .site_reactions import key as reaction_key
        keys = sorted(reaction_key(gid, mid) for gid, mid in self.rows)
        for start in range(0, len(keys), 256):
            for row in connection.execute(select(group_management).where(group_management.c.id.in_(keys[start:start+256]))).mappings():
                self.reactions[row['id']] = json.loads(row['value'])
