# DrG Media

<p><img src="docs/assets/repository-avatar.png" width="112" height="112" alt="DrG Media logo"></p>

A self-hosted Telegram archive and media service for DrG Space. Archive explicitly selected groups, preserve message history and media, import Telegram Desktop HTML exports, and provide authorized conversations and galleries through the workspace.

[Changelog](CHANGELOG.md) · [Quick start](#quick-start) · [Documentation](#documentation) · [Development](#development-and-checks) · [License](LICENSE)

## Overview

The service has no separate end-user web interface: DrG Space supplies accounts, permissions, chats and galleries.

## Features

- Resumable Telegram history and live updates through a dedicated MTProto account.
- Telegram Desktop HTML import without Telegram credentials, including paginated history and local attachments.
- Photos, videos, albums, captions, senders, replies, edits, confirmed deletions, stickers, GIFs, voice messages and video notes.
- Thumbnails, protected originals, HTTP Range playback and animated sticker previews.
- Optional bot sending, editing and reactions, with separate sending permission and a durable delivery queue.
- Managed source groups, delayed deletion, independent archive copies and background ZIP exports.
- Persisted Telegram rate limits, checked download checkpoints, bounded retries and automatic resource cleanup.
- SQLite and local files by default; optional PostgreSQL, S3-compatible storage and Redis metadata caching.

## Quick start

Start with your own offline export. Clone the repository URL supplied by your maintainer into `drg-media`, then:

```sh
cd drg-media
python3 scripts/init-env.py
docker compose build api
docker compose up -d api
docker compose ps
```

The API binds to `127.0.0.1:8090`; it is a private service, not a public website. Use your own extracted Telegram Desktop HTML export and follow [installation](INSTALL.md). For live Telegram synchronization, complete account login before starting the worker. Generating an environment does not connect to Telegram or enable optional services.

## Documentation

| Guide | Contents |
| --- | --- |
| [Installation and DrG Space integration](INSTALL.md) | Configuration, initial account login and workspace integration |
| [Updates and compatibility](docs/updates.md) | Upgrade steps and storage compatibility |
| [Telegram limits and download recovery](docs/telegram-limits.md) | Rate limits, retries and resumable downloads |
| [Progress and cleanup](docs/sync-progress.md) | Progress reporting and automatic cleanup |
| [Source bindings](docs/live-bindings.md) | Telegram sources and archive identities |
| [Group names and avatars](docs/group-profiles.md) | Managed group profiles |
| [Stickers, voice and video notes](docs/stickers.md) | Supported media and playback |
| [Storage transfer and backup](docs/storage-transfer.md) | Moving and recovering stored data |
| [Private service API](docs/api.md) | Authorized service endpoints |
| [Release history](CHANGELOG.md) | User-facing changes by version |
| [Contributing](CONTRIBUTING.md) | Development setup and checks |

## Compatibility and dependencies

DrG Media is the new project name for Family Photo. The Python module `family_photo`, legacy `family-photo` command, `FP_*` settings, `/v1` API, database identifiers, and object keys remain compatible. Docker names now use `drg-media`; existing installations must follow the documented volume-preserving transition before changing their Compose project. Use Python 3.14 and Docker Compose v2; preserve the working environment, Telegram session, archive identities and storage when updating.

### Optional services

| Service | Purpose | Without it |
| --- | --- | --- |
| PostgreSQL | Alternative metadata database | Dedicated SQLite database |
| S3-compatible storage | Private message bodies and media objects | Local object directory |
| Redis | Metadata caching and coordination | Local cache fallback |
| Telegram user session | Live history and updates | Offline HTML import remains available |
| Telegram bot | Sending, editing and supported reactions | Reading remains available |
| DrG Space | User-facing accounts, authorization and interface | CLI import and private service API remain available |

The reader account and bot have different roles. Do not publish the internal API, Telegram session, bot token or service token. Archive only material you are authorized to access and operate the integration under Telegram's current terms.

## Development and checks

Use Python 3.14 and this project's pinned runtime dependencies. The [contributor guide](CONTRIBUTING.md) describes the PostgreSQL and S3 test modes and the browser integration fixtures.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
.venv/bin/python -m pip install -e '.[test]' -c requirements.lock.txt
.venv/bin/python -m pytest -q
python3 scripts/check-public-content.py
.venv/bin/python scripts/dependency-notices.py --check
```

Tests use disposable data, emulated storage and mocked Telegram transports. Keep live sessions, production databases and working buckets outside test configurations.

## Release history

Read the [changelog](CHANGELOG.md) for release notes and the [update guide](docs/updates.md) before upgrading. The application version is maintained in `family_photo.__version__`; `api_version` is the separate integration contract.

## License

The project's own code uses [Elastic License 2.0](LICENSE), the same approach as DrG Space. Self-hosting and modifications are permitted under that license; providing substantial functionality as a hosted or managed service requires separate permission. This is source-available software, not OSI open source.

Dependencies retain their original licenses. See [third-party notices](THIRD_PARTY_NOTICES.md).
