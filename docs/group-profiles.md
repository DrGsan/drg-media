# Group names and avatars

The worker refreshes each connected group's Telegram profile no more frequently than once every five minutes, using its numeric Telegram ID and a fresh entity request. A verified HTML binding retains its existing `export:` archive ID; renaming does not replace message history, media, queues or access grants.

The workspace gallery refreshes titles separately from message history. A profile change can appear on the next worker check without a new chat message. Rate limits, connection failures or a long reconciliation can delay discovery; the profile interval is not a delivery-time guarantee.

Telegram `photo_id` identifies the avatar. An unchanged avatar is not downloaded again. A replacement receives a new protected resource version; removal falls back to the standard icon. A failed replacement preserves the last working avatar while the new title can still update. After successful replacement, cleanup checks references and locks before removing the old object. Profile updates do not delete messages or gallery content.

```sh
.venv/bin/python -m pytest -q tests/test_profiles.py tests/test_bindings.py
FP_TEST_STORAGE=s3 .venv/bin/python -m pytest -q tests/test_profiles.py tests/test_bindings.py
```
