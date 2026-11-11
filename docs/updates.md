# Updates and compatibility

## 1.14.2

The managed Telegram bot uses Media as its default display name and a localized name in Russian, with matching localized descriptions. The existing startup reconciliation updates this text while retaining the approved artwork and bot username. The synchronization client label is Media Server. API version, database schema, keys, object paths, environment and Telegram session are unchanged. No migration or new settings are required. Use the existing Compose update procedure below with the same files and external volumes.

## DrG Media 1.14.0 baseline

The project name changes from Family Photo to DrG Media. The application remains 1.14.0 and the integration protocol remains `api_version=1`. No new database, schema migration, media conversion or Telegram login is required solely for this repository change. The legacy Python module, command, environment keys and Docker identifiers remain supported.

The supported legacy command is `family-photo`; the new equivalent is `drg-media`. Both use `family_photo.__version__`. Existing service URLs and `FAMILY_PHOTO_*` settings in the portal remain valid. Product history is retained in [CHANGELOG](../CHANGELOG.md).

## 1.14.1 runtime-name transition

This release uses project `drg-media`, image `drg-media:local`, containers `drg-media-api` / `drg-media-worker` and optional `drg-media-postgres`. The workspace-network alias is `drg-media`; the old `family-photo` alias remains available. `DRG_SPACE_NETWORK` takes precedence over the supported legacy `DRG_SITE_NETWORK` setting. Module names, CLI alias, `FP_*` application settings, `/v1` API, database/schema, Telegram session, object identifiers, Redis keys and storage prefixes remain compatible. No schema migration or new Telegram login is required.

**Pin existing volumes before changing the Compose project.** Inspect the actual `/data` volume mounted by the old API and worker, and confirm both use the same archive. For a previous project named `family-photo`:

```sh
docker inspect family-photo-api --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}'
```

Add deployment settings to the working `.env`, substituting inspected names:

```dotenv
DRG_MEDIA_EXTERNAL_VOLUMES=true
DRG_MEDIA_DATA_VOLUME=family-photo_archive-data
DRG_SPACE_NETWORK=drg-space_default
```

Set `DRG_MEDIA_POSTGRES_VOLUME` to the existing PostgreSQL volume when that overlay is already used. External mode fails if the named volume is absent; it does not create an empty replacement. Preserve service tokens, Telegram session/checkpoints, database URL, S3 prefix and AWS settings. New installations use managed project-scoped volumes by default.

With the actual existing overlays and the DrG Space network already prepared:

```sh
docker compose -p drg-media -f compose.yaml -f compose.site.yaml config -q
docker compose -p drg-media -f compose.yaml -f compose.site.yaml build api
docker compose -p family-photo -f compose.yaml -f compose.site.yaml stop worker api
docker compose -p family-photo -f compose.yaml -f compose.site.yaml rm -f worker api
docker compose -p drg-media -f compose.yaml -f compose.site.yaml up -d --no-build --wait api worker
```

When bundled PostgreSQL is already used, include its overlay in every command and hand over that database container in the same controlled sequence. Never run two workers against the same archive/session. Keep port bindings and bind mounts unchanged. Do not use `down -v`. Update the workspace's internal service URL to `http://drg-media:8090` when using the new network. A client still using `http://family-photo:8090` on that network remains compatible.

Before removing old networks or images, confirm health, authorized status, archive identities, session availability and worker state. Keep previous images and the recorded old configuration for rollback until acceptance. Rollback stops the new containers and restores the previous images/project with the exact same volume names and environment; do not restore an old database over messages already received merely to rename containers.

## Subsequent updates

Keep all configured external volume names, working `.env`, ports and overlays. Fetch the approved release, then:

```sh
docker compose -p drg-media -f compose.yaml -f compose.site.yaml config -q
docker compose -p drg-media -f compose.yaml -f compose.site.yaml up -d --build --wait api worker
docker compose -p drg-media -f compose.yaml -f compose.site.yaml ps
```

A restart alone does not apply a new image or changed environment. `/healthz` checks HTTP availability; verify authorized status, source state and supported media after updating. A source-repository cutover must not merge unrelated Git histories. Preserve archive storage independently of the source directory.

## Historical compatibility

- 1.14.0 stores recording metadata in the existing attachment JSON and sends video notes with independently acknowledged author text. Existing jobs retain their numbering.
- 1.13.0 adds a complete calendar index so month navigation does not require loading all media pages.
- 1.12.0 makes Redis metadata caching optional; without Redis, local behavior remains available. Upgrade all processes before enabling it.
- 1.11.x reduces metadata queries, adds gallery revision checks and keeps HTTP liveness lightweight.
- 1.10.x adds outgoing voice support and stable Docker identifiers; retain the installation's original project and data volumes.
- 1.9.0 preserves archive voice originals through the ordinary media queue.
- 1.8.0 manages the configured bot's profile; consider that startup behavior when supplying a different bot.
- 1.2.0 introduces the owner-managed source registry and delayed group deletion. Legacy environment source/binding values are imported once, not used as an ongoing editor.

Do not infer arbitrary old-version restore compatibility from a version number. Compare the actual schema and object formats and preserve a matching consistent archive copy. A rollback after successful writes must account for new messages, edits, deletions and acknowledged queue deliveries. Never reset cursors or sessions to make an upgrade appear successful.
