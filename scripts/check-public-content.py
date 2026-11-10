#!/usr/bin/env python3
"""Validate the candidate Git contents without printing credential values."""
from pathlib import Path
import re, subprocess, sys

root=Path(__file__).resolve().parents[1]
paths=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=root).decode().split('\0')
errors=[]
for name in sorted(set(filter(None,paths))):
    path=root/name
    if name.startswith(('ios/','private/','.agents/','.codex/','docs/releases/','docs/audits/')) or name=='AGENTS.md' or name.startswith('scripts/build-ios-') or '.session' in path.name:
        errors.append((name,'private or excluded content'));continue
    if (name.startswith('.env') and name!='.env.example') or re.search(r'(?i)(?:token.*\.txt|\.(?:pem|key|p12|pfx|mobileprovision|drgbackup|sqlite3))$',name):
        errors.append((name,'credential, database or signing file'));continue
    if not path.is_file():continue
    data=path.read_bytes()
    if b'\0' in data[:8192]:continue
    checks={
        'local infrastructure reference': rb'(?:/Users/[^/\s]+/(?:PycharmProjects|Library)/|[A-Za-z0-9-]+\.tail[a-f0-9]+\.ts\.net|(?:unraid|tower)[-_](?:vm|local)(?:-local)?)',
        'private key': rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
        'credential token': rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|ntn_[A-Za-z0-9]{30,}|AKIA[A-Z0-9]{16})\b',
    }
    for label,pattern in checks.items():
        if re.search(pattern,data):errors.append((name,label))
for path,reason in errors:print(f'{path}: {reason}',file=sys.stderr)
print(f'Public-content guard: {len(set(filter(None,paths)))} files, {len(errors)} findings')
documentation = subprocess.run([sys.executable, str(root / 'scripts/check-documentation.py')], cwd=root).returncode
sys.exit(bool(errors) or bool(documentation))
