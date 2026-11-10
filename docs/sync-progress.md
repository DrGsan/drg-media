# Progress and cleanup

The private `GET /v1/groups/{gid}/sync` endpoint reports stage, current file, received/expected bytes, speed, queue state, completed files in the current batch, estimated download time and Telegram wait deadlines. It requires the service token; the portal separately authorizes the requesting user and group.

Progress polling is independent of gallery loading. It reads local state and does not issue additional Telegram requests. A batch progress bar counts genuinely ready files in the current queue, not thousands of older originals. New jobs extend the current batch; deleted/replaced jobs leave it. A new batch starts after completion.

Stages distinguish metadata reading, Telegram download, local/S3 storage, existing-original access and preview generation. ETA estimates the current download only; it excludes future Telegram pauses and storage/preview work. Unknown sizes or speeds do not produce invented estimates. Waiting and stale progress reset speed/ETA.

Progress telemetry is written atomically to `/data/.sync-progress.json` with private permissions. It contains no message bodies or S3 keys and is not the authority for file readiness. Download checkpoints are separate durable state: confirmed blocks and checksums permit supported downloads to resume after restart. Temporary work directories can be recreated without treating an incomplete original as ready.

## Object cleanup

Cleanup runs independently of downloading, in bounded batches. Failed object deletion stays queued for another attempt. Superseded objects can be collected after reference checks; unfinished publications receive a grace period.

References include messages, originals, previews, avatars and outgoing jobs. Shared-volume leases protect active writes through database publication. Telemetry never authorizes deletion. S3 multipart cleanup is restricted to registered unreferenced keys in this installation's prefix; it requires the appropriate list/abort permissions and defers on an unverifiable state.

Temporary workspaces use a locked `.active` sentinel. A long-running download or FloodWait remains protected. After a crash, cleanup recognizes only its own workspace formats and preserves live leases, durable download checkpoints, object directories, databases, sessions, backups and manual import sources. Bucket version retention is controlled by the bucket's own lifecycle policy.

Backup must cover the database, objects and persistent operational state consistently. Losing telemetry is harmless to history, but losing objects or checkpoints is not equivalent. See [storage transfer](storage-transfer.md) and [Telegram limits](telegram-limits.md).
