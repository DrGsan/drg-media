"""Private object storage. No Telegram file links or bucket URLs reach browsers."""
import json
import os
import re
import shutil
import uuid
import fcntl
import hashlib
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


class Objects:
    def __init__(self, config, s3=None):
        self.config = config
        self.root = config.data_dir / "objects"
        if config.storage == "local":
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        else:
            if s3 is None:
                import boto3
                from botocore.config import Config
                s3 = boto3.client("s3", endpoint_url=config.endpoint or None, region_name=config.region,
                    config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 3},
                                  connect_timeout=5, read_timeout=30))
            self.s3 = s3

    def key(self, key):
        if not re.fullmatch(r"[a-z]+/[a-f0-9]{32}", key):
            raise ValueError("Invalid object key")
        return f"{self.config.prefix}/{key}" if self.config.prefix else key

    @contextmanager
    def lease(self, key, blocking=True):
        """Shared-volume leases survive threads/processes, and release on a crash.

        Fixed stripes avoid accumulating a lock file for every uploaded object.
        Writers hold a lease until their database reference is committed.
        """
        self.key(key)
        root = self.config.data_dir / '.object-locks'
        root.mkdir(exist_ok=True, mode=0o700)
        stripe = hashlib.sha256(key.encode()).digest()[0]
        with (root / (key.split('/')[0] + '-' + str(stripe))).open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @contextmanager
    def workspace(self, prefix='fp-work-'):
        with tempfile.TemporaryDirectory(dir=self.config.data_dir, prefix=prefix) as directory:
            with (Path(directory) / '.active').open('w') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                yield directory

    def clean_temporary(self, startup=False):
        """Only our workspaces; never sessions, backups, imports or originals.

        Startup runs under the CLI's exclusive worker/import lock. Legacy
        workspaces without a sentinel can therefore be reclaimed at startup.
        """
        count = total = 0
        for path in self.config.data_dir.iterdir():
            if path.is_symlink() or not path.is_dir() or not re.fullmatch(r'(?:download-|verify-export-|fp-work-)[a-z0-9_]{8}', path.name):
                continue
            if not startup and time.time() - path.stat().st_mtime < 300:
                continue
            sentinel = path / '.active'
            if not sentinel.exists() and not startup:
                continue
            with sentinel.open('a') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                size = sum(p.stat().st_size for p in path.rglob('*') if p.is_file() and not p.is_symlink())
                shutil.rmtree(path)
                count += 1
                total += size
        for path in self.config.data_dir.glob('.*-????????'):
            if path.is_symlink() or not path.is_file() or not re.fullmatch(r'\.(?:sync-progress|telegram-limits)-[a-z0-9_]{8}',path.name) or time.time()-path.stat().st_mtime < 300: continue
            total += path.stat().st_size
            path.unlink()
            count += 1
        if self.config.storage == 'local':
            for path in self.root.glob('*/.*'):
                if path.is_symlink() or not path.is_file() or not re.fullmatch(r'\.[a-f0-9]{32}-[a-f0-9]{32}', path.name): continue
                if time.time() - path.stat().st_mtime < 300: continue
                key = path.parent.name + '/' + path.name[1:33]
                with self.lease(key, blocking=False) as acquired:
                    if acquired:
                        total += path.stat().st_size
                        path.unlink()
                        count += 1
        return {'temporary_removed': count, 'temporary_bytes': total}

    def unfinished_uploads(self):
        if self.config.storage != 's3': return {}
        result = {}
        prefix = self.config.prefix + '/' if self.config.prefix else ''
        for page in self.s3.get_paginator('list_multipart_uploads').paginate(Bucket=self.config.bucket, Prefix=prefix):
            for upload in page.get('Uploads', []):
                key = upload['Key'][len(prefix):]
                if re.fullmatch(r'[a-z]+/[a-f0-9]{32}', key):
                    result.setdefault(key, []).append(upload['UploadId'])
        return result

    def abort_uploads(self, key, uploads):
        """Only GC-confirmed, unreferenced, unleased keys from our prefix."""
        for upload_id in uploads.get(key, []):
            self.s3.abort_multipart_upload(Bucket=self.config.bucket, Key=self.key(key), UploadId=upload_id)

    def put_file(self, key, path, mime="application/octet-stream", progress=None):
        remote = self.key(key)
        if self.config.storage == "s3":
            self.s3.upload_file(str(path), self.config.bucket, remote, ExtraArgs={"ContentType": mime},
                                **({'Callback': progress} if progress else {}))
        else:
            destination = self.root / key
            destination.parent.mkdir(exist_ok=True, mode=0o700)
            temporary = destination.with_name("." + destination.name + "-" + uuid.uuid4().hex)
            try:
                with open(path, "rb") as source, open(temporary, "xb") as target:
                    os.chmod(temporary, 0o600)
                    while block := source.read(1024 * 1024):
                        target.write(block)
                        if progress: progress(len(block))
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)

    def put_json(self, key, data):
        with self.workspace() as temporary:
            path = Path(temporary) / 'content.json'
            path.write_text(json.dumps(data, ensure_ascii=False))
            self.put_file(key, path, "application/json")

    def read_json(self, key):
        return json.loads(b"".join(self.read(key)))

    def check(self):
        if self.config.storage == "s3":
            self.s3.head_bucket(Bucket=self.config.bucket)
        elif not self.root.is_dir() or not os.access(self.root, os.R_OK | os.W_OK):
            raise OSError("Local object store is unavailable")

    def usage(self):
        """One inventory of our prefix, including previews and retained objects."""
        result = dict(objects_bytes=0, media_bytes=0, thumbnail_bytes=0,
                      message_bytes=0, avatar_bytes=0, other_bytes=0, objects=0)
        categories = {'media': 'media_bytes', 'thumbnails': 'thumbnail_bytes',
                      'messages': 'message_bytes', 'outbox': 'message_bytes', 'avatars': 'avatar_bytes'}
        def add(key, size):
            result['objects'] += 1
            result['objects_bytes'] += size
            result[categories.get(key.split('/')[0], 'other_bytes')] += size
        if self.config.storage == 's3':
            prefix = self.config.prefix + '/' if self.config.prefix else ''
            for page in self.s3.get_paginator('list_objects_v2').paginate(Bucket=self.config.bucket, Prefix=prefix):
                for item in page.get('Contents', []):
                    add(item['Key'][len(prefix):], item['Size'])
        else:
            for path in self.root.rglob('*'):
                if path.is_symlink() or not path.is_file():
                    continue
                try:
                    add(path.relative_to(self.root).as_posix(), path.stat().st_size)
                except FileNotFoundError:
                    continue  # An import/garbage collection can finish during the scan.
        return result

    def size(self, key):
        remote = self.key(key)
        if self.config.storage == "local":
            return (self.root / key).stat().st_size
        return self.s3.head_object(Bucket=self.config.bucket, Key=remote)["ContentLength"]

    def read(self, key, start=0, end=None):
        remote = self.key(key)
        if self.config.storage == "local":
            source = open(self.root / key, "rb")
            source.seek(start)
        else:
            args = {"Bucket": self.config.bucket, "Key": remote}
            if start or end is not None:
                args["Range"] = f"bytes={start}-{end if end is not None else ''}"
            source = self.s3.get_object(**args)["Body"]
        remaining = None if end is None else end - start + 1
        try:
            while remaining is None or remaining > 0:
                block = source.read(min(1024 * 1024, remaining) if remaining is not None else 1024 * 1024)
                if not block:
                    break
                yield block
                if remaining is not None:
                    remaining -= len(block)
        finally:
            source.close()

    def delete(self, key):
        remote = self.key(key)
        if self.config.storage == "s3":
            # A delete marker alone retains all bytes in a versioned bucket.
            versioning = self.s3.get_bucket_versioning(Bucket=self.config.bucket).get('Status')
            if versioning in {'Enabled', 'Suspended'}:
                for page in self.s3.get_paginator('list_object_versions').paginate(Bucket=self.config.bucket, Prefix=remote):
                    for item in page.get('Versions', []) + page.get('DeleteMarkers', []):
                        if item['Key'] == remote:
                            self.s3.delete_object(Bucket=self.config.bucket, Key=remote, VersionId=item['VersionId'])
            else:
                self.s3.delete_object(Bucket=self.config.bucket, Key=remote)
        else:
            (self.root / key).unlink(missing_ok=True)
