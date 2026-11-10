# Source bindings and archive identity

An offline HTML archive and a live Telegram group are different sources. Never merge them merely because their titles match. Confirm message IDs, dates and text near the beginning, middle and end, and verify all known IDs against source dates before binding. A migrated basic group may share a title with a different supergroup; choose the actual numeric source ID.

Current installations manage sources through DrG Space Management. Imported archives are not automatically connected to a live group. The owner must select an explicit supported transition; messages, media, read cursors and access grants must retain their archive identity.

For upgrading earlier installations only, the legacy environment can describe a verified one-to-one mapping:

```dotenv
FP_TELEGRAM_GROUPS=-1001234567890
FP_TELEGRAM_ARCHIVE_BINDINGS={"-1001234567890":"export:0123456789abcdef"}
```

The Telegram source must be explicitly selected and the destination archive must already exist. These values are imported into the owner-managed registry once. Editing them afterward does not manage current groups. Do not modify source IDs directly in the database.

A verified binding retains the `export:` ID, object keys and portal permissions. Message-date disagreement stops processing rather than silently joining unrelated histories. Unchanged media with the same source identity and compatible metadata reuses its original and preview. Real source replacement is processed as an edit and does not publish an incomplete download as ready.

The reader session supplies history access; it does not authorize sending as the user account. Bot sending remains separately controlled and requires a supported supergroup. Removing source access or encountering a network error is not proof that messages were deleted.

Read and notification cursors are based on actual archive message state. Connecting an old export must not resend its entire history as new notifications. Confirm group status, retained message IDs and portal access after any binding change. See [updates](updates.md) and [storage transfer](storage-transfer.md).
