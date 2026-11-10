# Telegram limits and download recovery

The reader serializes Telegram RPCs and applies a persisted pacing and pause policy. Stored deadlines survive process restart. Never delete limit state, rotate sessions or restart repeatedly to bypass a Telegram restriction. The service follows the server-provided minimum wait and applies its own cautious backoff.

## Scope and fairness

The policy distinguishes global RPC waits from additional media backoff. A file RPC can impose its server wait globally while its longer local media pause does not unnecessarily stop metadata work. Ordinary `FLOOD_WAIT` keeps the complete server duration plus a safety margin. Other methods retain the appropriate global pause; Premium-related media policy remains separate.

Backoff grows after repeated rejection. A pause or waiting for the scheduler is not a failed download attempt. Fully loaded groups can still show a global wait for update checks; that does not mean their existing archive is downloading again.

History is incremental from a persisted cursor. Known IDs are reconciled periodically in bounded batches; the default `FP_RECONCILE_SECONDS` is 86400. A missing page or request timeout is not evidence of deletion. Confirmed missing messages require successful access checks and repeat verification.

## Checkpoints and failures

Downloads retain confirmed blocks with source identity, offsets and checksums. Restart validates the checkpoint before resuming. Corrupt, mismatched or obsolete state is not accepted as a completed file. An available original is reused for preview generation instead of downloaded again.

Five consecutive file failures without new data automatically pause that attachment. Actual source/file rejections and incomplete transfers count; metadata waits, connection interruptions, temporary server failures, S3 errors and preview failures do not falsely exhaust the file's attempt budget. Confirmed new blocks or a saved original reset the counter.

Paused records retain the source message, identity and known size. Reconciliation of the same attachment or an edited caption does not silently restart it. A genuine replacement is a separate source change. A paused file is not reported ready and does not hold every other download behind it.

## Status and recovery

Status reports the group, source message date/ID, cause and bounded recent list of paused files, with a separate total count. Links to live supergroup messages use their actual Telegram source ID; offline exports do not invent public links. Reading status uses the local database, not a new RPC.

Investigate source access, media identity and the reported failure before an explicit retry. Keep existing wait deadlines even after pausing a file. Preserve the session, checkpoint and limit files when changing containers or paths. See [progress](sync-progress.md) and [updates](updates.md).
