# Changelog

Application versions are independent of DrG Space; `api_version=1` is the integration protocol. Preview changes are consolidated into their stable release. Versions before 1.0.0 describe retrospective development milestones rather than separately published releases.

## [1.14.1rc1] — 2026-11-11 — Consistent runtime names

- Docker containers, images and Compose project use DrG Media names.
- The DrG Space network supports the drg-media alias while keeping family-photo compatible.
- Existing archive and optional PostgreSQL volumes can be pinned explicitly during the project rename.
- Compatibility: preserve the database, objects, session and keys; follow docs/updates.md before changing a running project.

## [1.14.0] — 2026-11-06 — Voice and video notes

- Recording metadata retains duration and waveform in the existing attachment JSON. Video notes use sendVideoNote and independently acknowledged author text; retries do not duplicate acknowledged parts.
- DrG Media keeps the existing module, command alias, service API, settings, database and object identifiers compatible.
- Fresh configuration is generated from the example without replacing existing credentials; the new drg-media command retains the legacy family-photo alias.
- Compatibility: no schema migration or replacement of working data is required for this baseline. See docs/updates.md.

## [1.13.0] — 2026-11-01 — Complete gallery calendar index

- The private API supplies all gallery months without loading every media page.

## [1.12.0] — 2026-10-28 — Optional Redis metadata cache

- Shared metadata caching is optional; the supported local fallback remains available.

## [1.11.2] — 2026-10-28 — Lightweight HTTP liveness

- The health endpoint checks HTTP availability without opening the archive or querying Telegram/S3.

## [1.11.1] — 2026-10-28 — Batched metadata queries

- Message states and group metadata avoid per-item SQL queries and repeated permission/status work.

## [1.11.0] — 2026-10-26 — Visible gallery revision checks

- Unchanged gallery pages use revision checks, and idle sending/export queues back off while new work wakes them promptly.

## [1.10.1] — 2026-10-26 — Stable Docker identifiers

- API and worker share the image and stable container names while retaining the family-photo network alias.

## [1.10.0] — 2026-10-24 — Outgoing voice recordings

- Authorized voice uploads use the durable sendVoice queue. Temporary delivery conversion preserves the uploaded original.

## [1.9.0] — 2026-10-24 — Voice originals in the archive

- Voice messages retain their original media through the existing download queue and protected playback routes.

## [1.8.0] — 2026-10-22 — Bot startup profile

- The configured bot receives the supported profile and avatar settings at startup.

## [1.7.2] — 2026-10-22 — GIF content validation

- GIF media is recognized and validated even when its declared MIME is incorrect.

## [1.7.1] — 2026-10-22 — Separate animated GIF media

- Animated GIF messages remain distinct from ordinary photo/video gallery items.

## [1.7.0] — 2026-10-21 — Bot reactions and ZIP dates

- Supported reactions use the shared bot identity, and exported ZIP entries preserve message dates.

## [1.6.0] — 2026-10-21 — Source status and bot attribution

- The API reports source state and attributes supported outgoing bot messages.

## [1.5.0] — 2026-10-21 — Bot sending and editing

- Authorized users send and edit supported messages through the durable bot queue, with explicit uncertain-delivery states.

## [1.4.0] — 2026-10-21 — Export completion and source copies

- Background export completion is reported, and independent copies remain accessible when the original source is disabled.

## [1.3.1] — 2026-10-21 — Copy origin validation

- An archive copy cannot be used as the source of another copy.

## [1.3.0] — 2026-10-21 — Independent group copies and ZIP exports

- Archive groups can produce independent copies and background ZIP downloads.

## [1.2.1] — 2026-10-21 — HTML import after source-registry changes

- Offline HTML archives retain their import behavior and identity after managed-source migration.

## [1.2.0] — 2026-10-21 — Managed sources and delayed deletion

- The portal owner manages connected sources. Group removal follows a seven-day recovery period rather than an immediate destructive reset.

## [1.1.0] — 2026-10-20 — Reaction authors and custom emoji

- The archive retains supported reactions, available authors and custom emoji within messages.

## [1.0.0] — 2026-10-19 — First official stable release

- The standalone archive becomes the first officially numbered stable release with its own database, objects and Telegram session.

## [0.12.0] — 2026-10-16 to 2026-10-17 — Paused failing attachments

- Repeated file failures pause the affected attachment while other archive work can continue.

## [0.11.0] — 2026-10-16 — Restart recovery

- Persisted history state and cursors allow synchronization to continue after restart.

## [0.10.0] — 2026-10-16 — Persisted Telegram rate limits

- Server wait deadlines and cautious request pacing survive process restart.

## [0.9.0] — 2026-10-16 — Progress and download cleanup

- Synchronization exposes file/batch progress and reference-aware cleanup.

## [0.8.0] — 2026-10-16 — Custom emoji

- Single custom emoji and their supported animated resources are retained.

## [0.7.0] — 2026-10-16 — Stickers and voice metadata

- Stickers are separate gallery types; historical voice entries retain type and known duration.

## [0.6.0] — 2026-10-16 — Live group events

- New messages, edits and confirmed deletions follow the selected Telegram sources.

## [0.5.0] — 2026-10-16 — Verified live source bindings

- A verified live Telegram group can continue an existing offline archive without changing its identity.

## [0.4.0] — 2026-10-16 — Archive storage accounting

- Archive usage includes its stored message bodies, media, previews and avatars.

## [0.3.0] — 2026-10-15 — Telegram group profiles

- Selected groups retain their source names and avatars.

## [0.2.0] — 2026-10-15 — Telegram Desktop HTML import

- Extracted HTML histories and referenced local media can be imported without a live session.

## [0.1.0] — 2026-10-15 — Standalone Telegram archive

- The service establishes a separate archive with metadata and private object storage.
