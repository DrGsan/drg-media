"""Offline Telegram exports. Export IDs never enter the live Telegram namespace."""
import hashlib
import mimetypes
import re
import subprocess
import time
from dataclasses import dataclass, field
from contextlib import ExitStack
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy import and_, select

from .db import media


@dataclass
class Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def find(self, *classes):
        return next((n for n in self.walk() if set(classes) <= set(n.attrs.get('class', '').split())), None)

    def text(self):
        if self.tag == 'br':
            return '\n'
        return ''.join(child.text() if isinstance(child, Node) else child for child in self.children)


class ExportHTML(HTMLParser):
    void = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node('root')
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in self.void:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.void:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


MONTHS = dict(zip(
    'января февраля марта апреля мая июня июля августа сентября октября ноября декабря'.split(), range(1, 13)))
MONTHS.update(dict(zip(
    'january february march april may june july august september october november december'.split(), range(1, 13))))


def exported_date(value, timezone):
    value = value.strip()
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        match = re.fullmatch(r'(\d{1,2})[ .]([\w]+)[ .](\d{4})[, ]+\s*(\d{1,2}):(\d{2})(?::(\d{2}))?', value)
        if not match:
            raise ValueError('Unsupported export date format') from None
        day, month, year, hour, minute, second = match.groups()
        month = int(month) if month.isdigit() else MONTHS.get(month.lower())
        if not month:
            raise ValueError('Unsupported export month')
        stamp = datetime(int(year), month, int(day), int(hour), int(minute), int(second or 0))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone)
    return int(stamp.timestamp())


def local_file(root, reference):
    """Never fetch URLs, follow escaping symlinks, or read files outside the export."""
    url = urlsplit(reference)
    if url.scheme or url.netloc or url.query or url.fragment:
        raise ValueError('An export attachment is not a local file')
    relative = Path(unquote(url.path))
    if relative.is_absolute() or '..' in relative.parts or not relative.parts:
        raise ValueError('An export attachment escapes its directory')
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError('An export attachment is missing or outside its directory')
    return path


@dataclass
class Export:
    group_id: str
    title: str
    records: list
    report: dict
    avatar: Path | None = None


def infer_export_albums(records):
    """HTML has no Telegram grouped_id: conservatively recover short media bursts.

    Preserve every source message; only annotate display metadata. A new caption,
    sender/reply change, text message, ID gap or pause starts a different series.
    """
    run = []
    def finish():
        if len(run) > 1:
            for item in run:
                item['album_id'] = 'export:' + str(run[0]['id'])
    for record in records:
        previous = run[-1] if run else None
        visual=record.get('_kind') in ('photo','video')
        continues = previous and visual and '_file' in record and len(run) < 10 and not record['text'] and (
            record['sender_id'] == previous['sender_id'] and record['id'] == previous['id'] + 1 and
            record.get('reply_to') == previous.get('reply_to') and
            0 <= record['created'] - previous['created'] <= 3 and record['created'] - run[0]['created'] <= 5)
        if not continues:
            finish()
            run = []
        if '_file' in record and visual:
            run.append(record)
    finish()


def read_export(source, timezone='UTC', archive_id=None):
    root = Path(source).resolve()
    if not root.is_dir():
        raise ValueError('Provide the extracted export directory')
    zone = ZoneInfo(timezone)
    pages = sorted(root.glob('messages*.html'), key=lambda p: (len(p.name), p.name))
    if not pages:
        raise ValueError('No messages.html export found')
    digest = hashlib.sha256()
    records, seen, title, sender = [], set(), '', ''
    services = unsupported = 0
    avatar = None
    for page in pages:
        raw = page.read_bytes()
        digest.update(page.name.encode())
        digest.update(raw)
        parser = ExportHTML()
        parser.feed(raw.decode('utf-8-sig'))
        if not title:
            header = parser.root.find('page_header')
            name = header.find('name') if header else None
            title = name.text().strip() if name else root.name
            image = next((n for n in header.walk() if n.tag == 'img' and n.attrs.get('src')), None) if header else None
            if image:
                avatar = local_file(root, image.attrs['src'])
        for node in parser.root.walk():
            classes = node.attrs.get('class', '').split()
            if 'message' not in classes:
                continue
            if 'service' in classes:
                services += 1
                continue
            match = re.fullmatch(r'message(\d+)', node.attrs.get('id', ''))
            if not match:
                raise ValueError('An exported message has no usable ID')
            mid = int(match[1])
            if mid in seen or not 0 < mid < 2**53:
                raise ValueError('Duplicate or invalid export message ID')
            seen.add(mid)
            if author := node.find('from_name'):
                sender = author.text().strip()
            if not sender:
                raise ValueError('An exported message has no sender')
            date = node.find('date')
            if date is None:
                raise ValueError('An exported message has no date')
            content = node.find('text')
            record = dict(id=mid, created=exported_date(date.attrs.get('title', ''), zone),
                          edited=0, sender=sender, sender_id='export:' + hashlib.sha256(sender.encode()).hexdigest()[:24],
                          text=content.text().strip() if content else '')
            if reply := node.find('reply_to'):
                for link in reply.walk():
                    reference = link.attrs.get('href', '') + ' ' + link.attrs.get('onclick', '')
                    target = re.search(r'(?:#(?:go_to_)?message|GoToMessage\()(\d+)', reference, re.I)
                    if target:
                        record['reply_to'] = int(target[1])
                        break
            sticker=node.find('sticker_wrap')
            voice=node.find('media_voice_message') or node.find('media_voice')
            if voice:record['content_type']='voice'
            attachment = node.find('animated_wrap') or node.find('photo_wrap') or node.find('video_file_wrap')
            kind = 'animation' if attachment and 'animated_wrap' in attachment.attrs['class'].split() else 'photo' if attachment and 'photo_wrap' in attachment.attrs['class'].split() else 'video'
            if sticker:
                record['content_type']='sticker'
                attachment=next((n for n in sticker.walk() if n.attrs.get('href')),None)
                if attachment is None:
                    image=next((n for n in sticker.walk() if n.tag=='img' and n.attrs.get('src')),None)
                    if image:attachment=Node('a',{'href':image.attrs['src']})
                kind='sticker'
            # Desktop exports use generic media links for uncompressed images,
            # videos sent as files and round video messages. Stickers stay out.
            if attachment is None and node.find('sticker_wrap') is None:
                candidate = node.find('media_video') or node.find('media_file')
                if candidate and candidate.attrs.get('href'):
                    mime = mimetypes.guess_type(unquote(urlsplit(candidate.attrs['href']).path))[0] or ''
                    if mime.startswith(('image/', 'video/')):
                        attachment = candidate
                        attachment_title=candidate.find('title')
                        kind = 'animation' if mime=='image/gif' or attachment_title and attachment_title.text().strip()=='Animation' else 'photo' if mime.startswith('image/') else 'video'
            if attachment:
                path = local_file(root, attachment.attrs.get('href', ''))
                mime = 'application/x-tgsticker' if path.suffix.lower()=='.tgs' else mimetypes.guess_type(path.name)[0] or ('image/webp' if kind=='sticker' else 'image/jpeg' if kind == 'photo' else 'video/mp4')
                if kind!='sticker' and mime=='image/gif':
                    from .animations import image_type
                    with path.open('rb') as stream:verified=image_type(stream.read(16))
                    if verified:kind,mime=verified['kind'],verified['mime']
                    else:kind='animation'
                if kind=='animation':record['animation_verified']=True
                record['_file'] = path
                record['_kind'] = kind
                record['_mime'] = mime
                # Native export already supplies video cover images. Reuse them safely.
                image = next((n for n in attachment.walk() if n.tag == 'img'), None)
                if kind in {'video','animation'} and image and image.attrs.get('src'):
                    try:
                        record['_preview'] = local_file(root, image.attrs['src'])
                    except ValueError:
                        pass  # Original video can still generate a cover via FFmpeg.
            elif node.find('media_wrap') or node.find('sticker_wrap'):
                unsupported += 1
            records.append(record)
    if not records:
        raise ValueError('The export has no ordinary messages')
    gid = archive_id or 'export:' + digest.hexdigest()[:24]
    if not re.fullmatch(r'export:[a-f0-9]{8,24}', gid):
        raise ValueError('Archive ID must be export: followed by 8–24 hexadecimal characters')
    records.sort(key=lambda r: r['id'])
    infer_export_albums(records)
    report = dict(group_id=gid, format='telegram-html', timezone=timezone,
                  messages=len(records), photos=sum(r.get('_kind') == 'photo' for r in records),
                  videos=sum(r.get('_kind') == 'video' for r in records),
                  animations=sum(r.get('_kind') == 'animation' for r in records),
                  stickers=sum(r.get('_kind') == 'sticker' for r in records),
                  bytes=sum(r['_file'].stat().st_size for r in records if '_file' in r),
                  unsupported_attachments=unsupported, service_events_skipped=services,
                  first_date=min(r['created'] for r in records), last_date=max(r['created'] for r in records))
    return Export(gid, title, records, report, avatar)


def file_digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def import_export(export, archive, progress=None):
    """Repeatable, streaming copy; crashes resume with existing ready attachments."""
    from .telegram import make_thumbnail
    gid = export.group_id
    existing = archive.group(gid)
    if existing and existing['kind'] != 'export':
        raise ValueError('Cannot import into a live Telegram group')
    if getattr(archive,'registry',None): archive.registry.import_source(gid)
    archive.add_group(gid, export.title, kind='export')
    if export.avatar:
        archive.store_avatar(gid, export.avatar, 'export:' + file_digest(export.avatar))
    archive.update_group(gid, state='importing', history_complete=False)
    transferred = reused = thumbnail_errors = 0
    for index, record in enumerate(export.records, 1):
        data = {k: v for k, v in record.items() if not k.startswith('_')}
        if path := record.get('_file'):
            data['media'] = dict(identity='export:' + file_digest(path), kind=record['_kind'],
                                 mime=record['_mime'], name=path.name, size=path.stat().st_size)
        archive.ingest(gid, data)
        if path:
            with archive.engine.connect() as c:
                item = c.execute(select(media).where(and_(media.c.group_id == gid,
                    media.c.message_id == record['id']))).mappings().first()
                item = dict(item) if item else None
            if item and item['state'] == 'ready':
                reused += 1
            elif item:  # A deletion tombstone may have prevented ingestion.
                with ExitStack() as uploads:
                    key = uploads.enter_context(archive.upload('media'))
                    archive.objects.put_file(key, path, data['media']['mime'])
                    thumb = None
                    with archive.objects.workspace() as temporary:
                        preview = Path(temporary) / ('preview.webp' if record['_kind']=='sticker' else 'preview.jpg')
                        try:
                            cover = record.get('_preview', path)
                            make_thumbnail(cover, preview, 'photo' if '_preview' in record else record['_kind'])
                            if preview.exists():
                                thumb = uploads.enter_context(archive.upload('thumbnails'))
                                archive.objects.put_file(thumb, preview, 'image/webp' if record['_kind']=='sticker' else 'image/jpeg')
                        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                            thumbnail_errors += 1
                        archive.attach_download(item, key, thumb, path.stat().st_size)
                transferred += 1
        archive.update_group(gid, cursor=record['id'], last_sync=int(time.time()))
        if progress and (index % 100 == 0 or index == len(export.records)):
            progress(dict(messages_processed=index, total=len(export.records), media_copied=transferred,
                          media_reused=reused))
    archive.update_group(gid, state='imported', history_complete=True)
    archive.collect_garbage()
    return export.report | dict(media_copied=transferred, media_reused=reused, thumbnail_errors=thumbnail_errors)
