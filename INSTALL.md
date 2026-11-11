# Installation

## Requirements

Use Docker Engine with Compose v2. Python 3.14 can generate the configuration locally; Python, FFmpeg and runtime dependencies are included in the image. Node.js is not required for the archive service. Commands below assume a Linux/macOS shell or WSL2.

Run commands from the source checkout. A fresh installation uses an independent SQLite database and local objects in `archive-data`. PostgreSQL, S3 and Redis are optional. The archive never shares DrG Space's database tables or object prefix.

```sh
python3 scripts/init-env.py
docker compose build api
```

The generator reads `.env.example`, creates a private `.env`, and generates a service token for a new installation. Existing values are preserved; it does not regenerate working credentials. Review configuration locally and keep it out of Git. `FP_SERVICE_TOKEN` must match the portal's `FAMILY_PHOTO_TOKEN`.

## Offline HTML import

Start only the API:

```sh
docker compose up -d api
```

Export a group with Telegram Desktop in HTML format, including original photos and videos. Keep `messages.html`, additional history pages and media directories together. Extract ZIP archives first; JSON exports are not supported by this importer. Mount the source read-only and allow container UID 10001 to read it:

```sh
docker compose run --rm --no-deps -v /absolute/export:/imports:ro api \
  drg-media import-export /imports --timezone Europe/Moscow --dry-run
docker compose run --rm --no-deps -v /absolute/export:/imports:ro api \
  drg-media import-export /imports --timezone Europe/Moscow
```

Use the export computer's actual timezone when dates lack an offset. Dry-run validates history and required originals without writing. The real import copies data into the archive and may require substantial extra space. Stop the worker while importing; API reads can remain available. Repeating an import resumes without duplicate messages or repeated copies of verified files. Absence from a later export is not evidence of deletion.

Without Docker, install the package and set the same `FP_*` environment values before invoking `drg-media`. The CLI does not implicitly read `.env`.

## Live Telegram synchronization

Obtain your own `api_id` and `api_hash` from https://my.telegram.org and set `FP_TELEGRAM_API_ID` and `FP_TELEGRAM_API_HASH`. Use a dedicated user account with access to the selected groups' history. Stop any existing worker before interactive login:

```sh
docker compose stop worker
docker compose run --rm --no-deps api drg-media login
docker compose run --rm --no-deps api drg-media dialogs
```

Enter the phone number, login code and optional cloud password interactively. Do not put them in documentation or support logs. The session is stored in `/data/telegram.session`. Never run two readers with the same session or reset stored rate limits to bypass a wait.

The workspace owner connects sources through DrG Space Management. The service reads only explicitly managed groups; possessing a Telegram session does not authorize scanning every chat. The legacy `FP_TELEGRAM_GROUPS` and `FP_TELEGRAM_ARCHIVE_BINDINGS` variables are for upgrading earlier installations, not ongoing source management. See [bindings](docs/live-bindings.md).

```sh
docker compose up -d api worker
docker compose ps
docker compose logs --tail=50 api worker
```

The phone need not remain online. Revoking or losing the session requires stopping the worker and repeating login. Initial synchronization may take time; progress and Telegram pauses are reported separately.

## Optional bot sending

Create your own bot through BotFather and add it to explicitly selected supergroups with the needed permissions. Set `FP_BOT_TOKEN`. The user session reads history; the bot sends messages. A bot is not used to read old history, and disabling its privacy mode is not required by this design.

Reading and sending permissions are separate. Basic groups remain readable but cannot use the supergroup bot delivery path. Telegram may restrict voice/video-note sending separately. The service never retries an uncertain send automatically: inspect the delivery state first.

## DrG Space integration

Set `FAMILY_PHOTO_URL` and `FAMILY_PHOTO_TOKEN` in DrG Space. The token is the same secret as `FP_SERVICE_TOKEN`. For Compose network integration, inspect the portal's actual network name, set `DRG_SPACE_NETWORK`, and include the existing override:

```sh
docker compose -f compose.yaml -f compose.site.yaml up -d api worker
```

The current internal API alias is `drg-media`, so use `http://drg-media:8090`. The earlier `family-photo` alias and `DRG_SITE_NETWORK` setting remain supported for existing clients. Enable the module and assign group permissions through DrG Space. The portal supplies the browser interface, accounts, invitations and per-user authorization. The archive token must never be sent to a browser. The service does not need its own public reverse-proxy route.

## Optional backends

For PostgreSQL, generate a URL-safe independent password and use `compose.postgres.yaml` together with every existing override. The generator provides a fresh password for new installs. API and worker must use the same database configuration; preserve the existing database and schema on upgrades. Never point this service at the portal's database.

For S3, set `FP_STORAGE=s3`, a private bucket, endpoint, region and the existing prefix, plus scoped AWS credentials. Keep the bucket private and preserve object keys during transfer. [Storage transfer](docs/storage-transfer.md) describes verification and recovery.

For Redis, set `FP_REDIS_URL` only when a private instance is available. `rediss://` uses verified TLS. Without Redis, metadata uses the supported local fallback. Update all API/worker processes before enabling shared cache behavior.

## Health, security and multiple installations

`/healthz` checks HTTP liveness without creating the archive. Authorized `/v1/status` provides broader service state. Neither a healthy HTTP probe nor a container restart proves Telegram or S3 connectivity. Keep API ports private and use the built-in token.

The default Compose project is `drg-media`, with `drg-media-api`, `drg-media-worker` and image `drg-media:local`. The optional database container is `drg-media-postgres`. Before upgrading an existing `family-photo` deployment, follow the volume-preserving transition in [updates](docs/updates.md). Default names describe one installation per host. A second installation needs distinct container names, a distinct Compose project, independent volumes, credentials and object prefix. Changing the repository directory alone must never silently replace a working volume.

Back up the archive database, objects, configuration, session and persistent operational state consistently. A DrG Space `.drgbackup` does not include this service's data.
