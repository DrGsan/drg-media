# Storage transfer and backup

DrG Media owns its database, objects, configuration, Telegram session, limits and checkpoints. DrG Space backups do not include them. Preserve a consistent archive copy independently. Never substitute a database-only copy for its associated objects.

Changing `FP_STORAGE` does not copy existing data. Database records refer to object keys relative to the configured backend and prefix; retain those keys exactly. The service's objects include message bodies, originals, thumbnails, avatars and supported outgoing/export data.

## Verified local-to-S3 transfer

1. Stop the worker and all API writers. Record the actual Compose project, overrides, database and object locations; retain a consistent recovery copy before changing them.
2. Create a private dedicated destination bucket/prefix with scoped credentials. Do not reuse the portal's object namespace.
3. Enumerate the exact local objects. Record relative keys, sizes and SHA-256 checksums outside the object directory.
4. Copy objects without renaming keys. Verify the destination listing and download every copied object to compare its complete size and checksum; an ETag alone is insufficient.
5. Set `FP_STORAGE=s3` and the destination settings in the existing `.env`. Keep the same database, session, archive IDs and operational state. Recreate API/worker with every previous Compose override and the same project name.
6. Check authorized `/v1/status`, groups, message bodies, avatars, originals, thumbnails, byte-range video and portal integration. HTTP liveness alone does not verify S3.
7. Only after full verification, consider deleting precisely the source objects in the checked transfer journal. Recheck unchanged source checksums and matching destination objects first. Never delete the entire volume, database, session or manual exports as part of object cleanup.

Before source deletion, rollback can restore the previous configuration and local backend. Once new messages or edits exist, reconcile them first: blindly restoring an older database or backend can lose new content. After local copies are removed, rollback requires verified reverse copying from S3.

Store transfer journals and recovery material privately, not in public Git or user object paths. S3 versioning requires a separate lifecycle policy. Preserve existing prefixes during an upgrade; a new prefix creates a different object namespace rather than migrating the old one.

## Database and updates

SQLite and PostgreSQL are metadata alternatives; object storage is independent. A backend transfer does not itself change the SQL schema. Database changes require an explicit supported migration and recovery plan, not a fresh empty database. Do not run a transfer concurrently with writes. See [updates](updates.md).
