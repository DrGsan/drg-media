#!/usr/bin/env python3
"""Generate configuration without replacing existing credentials."""
import argparse
import os
from pathlib import Path
import re
import secrets
import tempfile
ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / '.env')
    target = parser.parse_args().output
    if target.is_symlink():
        parser.error('Refusing to replace a symbolic link')
    existed = target.exists()
    existing = target.read_text() if existed else ''
    pattern = re.compile(r'^([A-Z][A-Z0-9_]*)=(.*)$')
    values = {}
    for line in existing.splitlines():
        if match := pattern.match(line):
            key, value = match.groups()
            if key in values:
                parser.error('Duplicate environment setting: ' + key)
            values[key] = value
    template = (ROOT / '.env.example').read_text().splitlines()
    known = {m.group(1) for line in template if (m := pattern.match(line))}
    if not existed:
        values['FP_SERVICE_TOKEN'] = secrets.token_urlsafe(48)
        values['FP_POSTGRES_PASSWORD'] = secrets.token_urlsafe(32)
    result = []
    for line in template:
        match = pattern.match(line)
        result.append(match.group(1) + '=' + values.get(match.group(1), match.group(2)) if match else line)
    extras = [line for line in existing.splitlines() if (m := pattern.match(line)) and m.group(1) not in known]
    if extras:
        result.extend(['', '# Existing custom settings', *extras])
    content = '\n'.join(result).rstrip() + '\n'
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.env-', dir=target.parent)
    try:
        with os.fdopen(descriptor, 'w') as output:
            os.chmod(temporary, 0o600)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    print('Configuration written; existing values preserved.' if existed else 'Configuration created with new private credentials.')


if __name__ == '__main__':
    main()
