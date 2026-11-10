import os
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Config:
    data_dir: Path
    database_url: str
    service_token: str
    storage: str = "local"
    bucket: str = ""
    endpoint: str = ""
    region: str = "us-east-1"
    prefix: str = "family-photo"
    api_id: int = 0
    api_hash: str = ""
    bot_token: str = ""
    group_ids: tuple[str, ...] = ()
    reconcile_seconds: int = 86400
    page_delay: float = 1.0
    group_bindings: tuple[tuple[str, str], ...] = ()
    redis_url: str = field(default='', repr=False)
    redis_cache_ttl: int = 30

    @property
    def archive_group_ids(self):
        return tuple(self.archive_id(gid) for gid in self.group_ids)

    def archive_id(self, telegram_id):
        return dict(self.group_bindings).get(str(telegram_id), str(telegram_id))

    def telegram_id(self, archive_id):
        return next((source for source, target in self.group_bindings if target == archive_id), archive_id)

    @classmethod
    def from_env(cls):
        bindings = json.loads(os.getenv('FP_TELEGRAM_ARCHIVE_BINDINGS', '{}') or '{}')
        if not isinstance(bindings, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in bindings.items()):
            raise ValueError('FP_TELEGRAM_ARCHIVE_BINDINGS must be a JSON object of Telegram IDs to export IDs')
        root = Path(os.getenv("FP_DATA_DIR", "./data")).resolve()
        url = os.getenv("FP_DATABASE_URL", "") or "sqlite:///" + str(root / "archive.sqlite3")
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        return cls(
            root, url, os.getenv("FP_SERVICE_TOKEN", ""), os.getenv("FP_STORAGE", "local"),
            os.getenv("FP_S3_BUCKET", ""), os.getenv("FP_S3_ENDPOINT_URL", ""),
            os.getenv("FP_S3_REGION", "us-east-1"), os.getenv("FP_S3_PREFIX", "family-photo").strip("/"),
            int(os.getenv("FP_TELEGRAM_API_ID", "0") or 0), os.getenv("FP_TELEGRAM_API_HASH", ""),
            os.getenv("FP_BOT_TOKEN", ""),
            tuple(x.strip() for x in os.getenv("FP_TELEGRAM_GROUPS", "").split(",") if x.strip()),
            max(60, int(os.getenv("FP_RECONCILE_SECONDS", "86400"))),
            max(.2, float(os.getenv("FP_PAGE_DELAY", "1"))),
            tuple(bindings.items()),
            os.getenv('FP_REDIS_URL', '').strip(),
            int(os.getenv('FP_REDIS_CACHE_TTL', '30')),
        )

    def validate(self):
        if type(self.redis_cache_ttl) is not int or not 1 <= self.redis_cache_ttl <= 300:
            raise ValueError('FP_REDIS_CACHE_TTL must be between 1 and 300 seconds')
        if self.redis_url:
            try:
                url = urlsplit(self.redis_url)
                valid = url.scheme in {'redis', 'rediss'} and bool(url.hostname) and not url.query and not url.fragment
                port = url.port
                if port is not None and not 1 <= port <= 65535: valid = False
            except ValueError:
                valid = False
            if not valid:
                raise ValueError('FP_REDIS_URL must be a redis:// or rediss:// URL without query or fragment')
        if len(self.service_token) < 32:
            raise ValueError("FP_SERVICE_TOKEN must contain at least 32 characters")
        if self.storage not in {"local", "s3"}:
            raise ValueError("FP_STORAGE must be local or s3")
        if self.storage == "s3" and not self.bucket:
            raise ValueError("FP_S3_BUCKET is required")
        if not self.database_url.startswith(("sqlite:///", "postgresql+psycopg://")):
            raise ValueError("Only a standalone SQLite or PostgreSQL database is supported")
        for group in self.group_ids:
            if not group.startswith("-") or not group[1:].isdigit():
                raise ValueError("FP_TELEGRAM_GROUPS must contain numeric group IDs")
        if (len(dict(self.group_bindings)) != len(self.group_bindings)
                or len(set(self.archive_group_ids)) != len(self.archive_group_ids)
                or any(source not in self.group_ids or not re.fullmatch(r'export:[a-f0-9]{8,24}', target)
                       for source, target in self.group_bindings)):
            raise ValueError('Archive bindings must uniquely map configured Telegram groups to existing export IDs')
