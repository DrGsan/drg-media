# Updates and compatibility

## DrG Media 1.14.0 baseline

The project name changes from Family Photo to DrG Media. The application remains 1.14.0 and the integration protocol remains `api_version=1`. No new database, schema migration, media conversion or Telegram login is required solely for this repository change. The legacy Python module, command, environment keys and Docker identifiers remain supported.

The supported legacy command is `family-photo`; the new equivalent is `drg-media`. Both use `family_photo.__version__`. Existing service URLs and `FAMILY_PHOTO_*` settings in the portal remain valid. Product history is retained in [CHANGELOG](../CHANGELOG.md).

## Updating an existing installation

Keep the working `.env`, Telegram session, checkpoints, limit deadlines, object keys, database, S3 prefix, Compose project name, networks and volumes. Do not regenerate secrets or initialize a new database. The directory name is not the data identity.

For an existing installation whose project name is `family-photo` and which uses the portal network override, an update looks like:

```sh
git fetch origin
git switch main
git pull --ff-only origin main
docker compose -p family-photo -f compose.yaml -f compose.site.yaml config -q
docker compose -p family-photo -f compose.yaml -f compose.site.yaml up -d --build api worker
docker compose -p family-photo -f compose.yaml -f compose.site.yaml ps
```

Use the installation's actual project name and all overlays, adding `compose.postgres.yaml` only when already used. A source-repository cutover needs a separately prepared checkout: do not merge unrelated Git histories. Confirm every resolved volume before starting a differently named directory.

Update API and worker together. A restart alone does not apply a new image or changed environment. `/healthz` checks HTTP availability; verify authorized status, source state and supported media after updating.

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
