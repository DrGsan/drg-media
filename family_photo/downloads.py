"""Private, checked download checkpoints; independent of disposable workspaces."""
import hashlib
import json
import os
import re
import shutil
import time
from contextlib import contextmanager

from .limits import CHUNK_SIZE


class DownloadCache:
    def __init__(self, objects):
        self.objects = objects
        self.root = objects.config.data_dir / '.telegram-downloads'

    @contextmanager
    def open(self, location, expected, fallback):
        identity = getattr(location, 'id', None)
        if not identity or not expected:
            with fallback.open('w+b') as output:
                yield Checkpoint(output, expected)
            return
        variant = getattr(location, 'thumb_size', '')
        if getattr(location, 'sizes', None):
            variant = location.sizes[-1].type
        signature = [type(location).__name__, str(identity), variant, expected]
        key = hashlib.sha256(json.dumps(signature).encode()).hexdigest()
        self.root.mkdir(exist_ok=True, mode=0o700)
        # Same shared-volume lease for download, checkpoint removal and cleanup.
        with self.objects.lease('downloads/' + key[:32]):
            directory = self.root / key
            directory.mkdir(exist_ok=True, mode=0o700)
            path = directory / 'original'
            with path.open('a+b') as output:
                os.chmod(path, 0o600)
                yield Checkpoint(output, expected, directory, str(identity))

    @staticmethod
    def materialize(checkpoint, target):
        # The upload owns this hard link even if maintenance removes the cache
        # after another message has committed the same Telegram file identity.
        if str(target) != checkpoint.output.name:
            target.unlink(missing_ok=True)
            os.link(checkpoint.output.name, target)
        return str(target)

    def remove_identities(self, identities):
        if not self.root.exists(): return
        for directory in self.root.iterdir():
            if directory.is_symlink() or not directory.is_dir() or not re.fullmatch('[a-f0-9]{64}', directory.name): continue
            try: candidate = json.loads((directory / 'checkpoint.json').read_text())
            except (OSError, ValueError): continue
            if candidate.get('identity') not in identities: continue
            with self.objects.lease('downloads/' + directory.name[:32], blocking=False) as acquired:
                if not acquired: raise OSError('checkpoint_in_use')
                try: value = json.loads((directory / 'checkpoint.json').read_text())
                except (OSError, ValueError): continue
                if value.get('identity') in identities: shutil.rmtree(directory)

    def clean(self, ready):
        removed = size = 0
        if not self.root.exists():
            return {}
        for directory in self.root.iterdir():
            if directory.is_symlink() or not directory.is_dir() or not re.fullmatch('[a-f0-9]{64}', directory.name):
                continue
            with self.objects.lease('downloads/' + directory.name[:32], blocking=False) as acquired:
                if not acquired:
                    continue
                state = directory / 'checkpoint.json'
                try:
                    metadata = json.loads(state.read_text())
                except (OSError, ValueError):
                    metadata = {}
                # Ready originals already have a durable object-store reference.
                # Other abandoned partials expire, but never during an active lease.
                if metadata.get('identity') not in ready and time.time() - directory.stat().st_mtime < 7*86400:
                    continue
                size += sum(p.stat().st_size for p in directory.iterdir() if p.is_file() and not p.is_symlink())
                shutil.rmtree(directory)
                removed += 1
        return {'temporary_removed': removed, 'temporary_bytes': size}


class Checkpoint:
    def __init__(self, output, expected, directory=None, identity=None):
        self.output, self.expected, self.directory, self.identity = output, expected, directory, identity
        self.digest = hashlib.sha256()
        offset = 0
        if directory:
            try:
                state = json.loads((directory / 'checkpoint.json').read_text())
                offset = state['offset']
                if type(offset) is not int or not 0 <= offset <= expected or (offset != expected and offset % CHUNK_SIZE):
                    raise ValueError('invalid checkpoint offset')
                output.seek(0)
                left = offset
                while left:
                    block = output.read(min(left, 1024*1024))
                    if not block:
                        raise ValueError('short checkpoint')
                    self.digest.update(block)
                    left -= len(block)
                if self.digest.hexdigest() != state['sha256']:
                    raise ValueError('checkpoint checksum mismatch')
            except (OSError, ValueError, KeyError, TypeError):
                offset = 0
                self.digest = hashlib.sha256()
        output.truncate(offset)
        output.seek(offset)

    @property
    def offset(self):
        return self.output.tell()

    def write(self, chunk):
        if self.expected is not None and self.offset + len(chunk) > self.expected:
            raise ValueError('oversized_media_download')
        self.output.write(chunk)
        self.output.flush()
        os.fsync(self.output.fileno())
        self.digest.update(chunk)
        if self.directory:
            path = self.directory / '.checkpoint.tmp'
            with path.open('w') as state:
                os.chmod(path, 0o600)
                json.dump(dict(identity=self.identity, offset=self.offset, sha256=self.digest.hexdigest()), state)
                state.flush()
                os.fsync(state.fileno())
            os.replace(path, self.directory / 'checkpoint.json')
            os.utime(self.directory, None)
