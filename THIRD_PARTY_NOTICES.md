# Third-party notices

Elastic License 2.0 covers only this project's own code. Dependencies retain their original licenses. Runtime notices are preserved under `licenses/python/` and indexed with hashes in `dependency-licenses.json`.

FFmpeg is a separate Debian package invoked as a separate process. Inspect the actual build with `ffmpeg -buildconf` and `ffmpeg -L`, and retain Debian package copyright/source information when redistributing an image. System packages are not relicensed under ELv2.

The psycopg-binary distribution declares LGPL-3.0-only. Preserve its notices and applicable binary/source obligations. Other wheels can include libraries with their own terms listed in the original notices. Review the actual image's system-package obligations before redistribution.

Telegram content and sessions are user-supplied. Optional Redis/S3 servers are external services, not bundled application code.

| Python package | Pinned version | Declared license |
| --- | --- | --- |
| Flask | 3.1.3 | BSD-3-Clause |
| Jinja2 | 3.1.6 | License :: OSI Approved :: BSD License |
| MarkupSafe | 3.0.4 | BSD-3-Clause |
| SQLAlchemy | 2.1.4 | MIT |
| Telethon | 1.45.0 | MIT |
| Werkzeug | 3.1.9 | BSD-3-Clause |
| anyio | 4.15.1 | MIT |
| blinker | 1.9.0 | License :: OSI Approved :: MIT License |
| boto3 | 1.43.114 | See original license text |
| botocore | 1.43.114 | See original license text |
| certifi | 2026.7.22 | License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0) |
| click | 8.5.0 | BSD-3-Clause |
| gunicorn | 26.2.0 | MIT |
| h11 | 0.16.0 | License :: OSI Approved :: MIT License |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpx | 0.28.1 | License :: OSI Approved :: BSD License |
| idna | 3.20 | BSD-3-Clause |
| itsdangerous | 2.2.0 | License :: OSI Approved :: BSD License |
| jmespath | 1.1.0 | License :: OSI Approved :: MIT License |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| pillow | 12.3.0 | MIT-CMU |
| psycopg-binary | 3.3.6 | LGPL-3.0-only |
| psycopg | 3.3.6 | LGPL-3.0-only |
| pyaes | 1.6.1 | License :: OSI Approved :: MIT License |
| pyasn1 | 0.6.4 | See original license text |
| python-dateutil | 2.9.0.post0 | License :: OSI Approved :: BSD License; License :: OSI Approved :: Apache Software License |
| rsa | 4.9.1 | License :: OSI Approved :: Apache Software License |
| redis | 8.1.0 | MIT |
| s3transfer | 0.19.2 | License :: OSI Approved :: Apache Software License |
| six | 1.17.0 | License :: OSI Approved :: MIT License |
| typing_extensions | 4.16.0 | PSF-2.0 |
| urllib3 | 2.8.0 | MIT |
