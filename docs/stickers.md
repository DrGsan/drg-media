# Stickers, voice messages and video notes

Static WebP stickers, TGS animations, video stickers and single custom emoji are stored separately from photo/video gallery items. Custom emoji documents are fetched in batches and cached within a bounded catalog; ordinary text containing emoji remains text.

TGS remains a private gzip original. The protected `media/{id}?animation=1` route returns bounded, validated animation JSON without executable expressions, remote images or fonts. The workspace plays it locally. FFmpeg and Pillow prepare supported animated WebP previews for video stickers; originals remain available through authorized routes. Stickers do not enter the photo gallery.

Current versions preserve voice originals and support authorized playback. Voice uploads are checked against the actual media container and duration. Supported outgoing formats include AAC/M4A, Opus/Ogg and Opus/WebM; WebM can be converted to a temporary delivery format while the uploaded original remains unchanged.

Voice recordings are limited to ten minutes. Video notes are square recordings with audio, limited to one minute; the recording upload limit is 50 MiB. Telegram bot permissions are checked during upload, queueing and delivery. Video notes use `sendVideoNote`; author text is a separately acknowledged queue part because Telegram does not accept a normal caption there.

Durable delivery parts prevent retrying already acknowledged components. An uncertain result stops automatic retries. Original message IDs, ownership, queue semantics, access control and Telegram deadlines remain in effect.

A startup refresh and normal bounded reconciliation update existing media metadata. Downloading runs independently of metadata and sending work. One failing file does not block the whole archive, and available originals are not re-downloaded merely to generate a preview. See [limits](telegram-limits.md) and [progress](sync-progress.md).
