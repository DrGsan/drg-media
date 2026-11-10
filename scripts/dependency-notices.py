#!/usr/bin/env python3
"""Generate or check original license notices from the exact runtime lock."""
import argparse
import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path
import re
import sys
ROOT = Path(__file__).resolve().parent.parent
HEADER = """# Third-party notices

Elastic License 2.0 covers only this project's own code. Dependencies retain their original licenses. Runtime notices are preserved under `licenses/python/` and indexed with hashes in `dependency-licenses.json`.

FFmpeg is a separate Debian package invoked as a separate process. Inspect the actual build with `ffmpeg -buildconf` and `ffmpeg -L`, and retain Debian package copyright/source information when redistributing an image. System packages are not relicensed under ELv2.

The psycopg-binary distribution declares LGPL-3.0-only. Preserve its notices and applicable binary/source obligations. Other wheels can include libraries with their own terms listed in the original notices. Review the actual image's system-package obligations before redistribution.

Telegram content and sessions are user-supplied. Optional Redis/S3 servers are external services, not bundled application code.

| Python package | Pinned version | Declared license |
| --- | --- | --- |
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    packages, expected = [], {}
    for line in (ROOT / 'requirements.lock.txt').read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        name, version = line.strip().split('==')
        distribution = metadata.distribution(name)
        if distribution.version != version:
            raise SystemExit(name + ': installed version differs from runtime lock')
        normalized = re.sub(r'[-_.]+', '-', name).lower()
        notices = []
        for file in distribution.files or []:
            text = str(file)
            if not ('.dist-info/licenses/' in text.lower() or '.dist-info/' in text.lower() and Path(text).name.lower().startswith(('license', 'copying', 'notice', 'authors'))):
                continue
            source = Path(distribution.locate_file(file))
            if not source.is_file():
                continue
            target = 'licenses/python/' + normalized + '-' + version + '/' + text.split('.dist-info/', 1)[1]
            data = source.read_bytes()
            expected[target] = data
            notices.append({'path': target, 'sha256': hashlib.sha256(data).hexdigest()})
        if not notices:
            raise SystemExit(name + ': no original license file; manual review required')
        declared = distribution.metadata.get('License-Expression') or '; '.join(v for v in distribution.metadata.get_all('Classifier', []) if v.startswith('License ::')) or 'See original license text'
        packages.append({'name': name, 'version': version, 'license': declared, 'notices': notices})
    expected['dependency-licenses.json'] = (json.dumps({'packages': packages}, indent=2) + '\n').encode()
    table = ''.join('| ' + p['name'] + ' | ' + p['version'] + ' | ' + p['license'].replace('|', '/') + ' |\n' for p in packages)
    expected['THIRD_PARTY_NOTICES.md'] = (HEADER + table).encode()
    problems = []
    for path, data in expected.items():
        target = ROOT / path
        if args.check:
            if not target.is_file() or target.read_bytes() != data:
                problems.append(path)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    old = {str(p.relative_to(ROOT)) for p in (ROOT / 'licenses/python').rglob('*') if p.is_file()} if (ROOT / 'licenses/python').exists() else set()
    stale = old - set(expected)
    if args.check:
        problems.extend(sorted(stale))
    else:
        for path in stale:
            (ROOT / path).unlink()
    if problems:
        print('Dependency notices need review: ' + ', '.join(problems), file=sys.stderr)
        raise SystemExit(1)
    print('Dependency notices: ' + str(len(packages)) + ' exact runtime packages; original licenses preserved.')


if __name__ == '__main__':
    main()
