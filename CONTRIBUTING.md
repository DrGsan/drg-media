# Contributing

Use Python 3.14 and the pinned runtime requirements. Develop against disposable data and mocked Telegram transports; never run tests with a live session, production database or working bucket.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
.venv/bin/python -m pip install -e '.[test]' -c requirements.lock.txt
.venv/bin/python -m pytest -q
FP_TEST_STORAGE=s3 .venv/bin/python -m pytest -q
python3 scripts/check-public-content.py
.venv/bin/python scripts/dependency-notices.py --check
```

PostgreSQL tests require an explicitly supplied disposable `FP_TEST_DATABASE_URL`; each fixture uses a separate temporary schema. Never supply a production URL. The S3 test mode uses an emulator and synthetic credentials.

Integration browser fixtures under `tests/manual/` use a sibling `../drg-space` checkout or `FP_SITE_REPO`. They require that checkout's Python and Playwright dependencies and built web assets. No working DrG Space instance is used.

Keep protocol compatibility explicit. The application version lives in `family_photo.__version__`; API version 1 is a separate integration contract. Preserve database records, archive IDs, sessions, checkpoints, object keys and rate-limit deadlines across compatible updates.

Maintain English product documentation and the `documentation.json` catalog. Preserve dependency notices, document data-format changes, and include focused regression coverage for fixes. Never commit credentials, sessions, user content or local deployment records.
