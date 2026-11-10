#!/usr/bin/env python3
"""Validate the maintained English documentation catalog and local links."""
import json,re,subprocess,sys
from pathlib import Path
from urllib.parse import unquote,urlsplit
root=Path(__file__).resolve().parent.parent
catalog=json.loads((root/'documentation.json').read_text());expected={x['path'] for x in catalog['documents']};third=tuple(catalog.get('third_party_roots',[]));paths=set(filter(None,subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=root).decode().split('\0')))
actual={p for p in paths if p.lower().endswith(('.md','.markdown')) and (root/p).is_file() and not p.startswith(third)};errors=[]
for path in sorted(actual-expected):errors.append(path+': unregistered Markdown document')
for path in sorted(expected-actual):errors.append(path+': documented file is missing')
markers=re.compile(r'(?im)^#{1,6}\s*(?:system prompt|user prompt|agent instructions|acceptance report|internal audit)\b|(?:SITE|DRG)-\d+|codex://|/Users/[^/\s]+/(?:PycharmProjects|Library)/|(?:unraid|tower)[-_](?:vm|local)(?:-local)?')
for path in sorted(actual&expected):
 source=root/path;text=source.read_text()
 if re.search('[А-Яа-яЁё]',text):errors.append(path+': non-English prose requires review')
 if markers.search(text):errors.append(path+': internal workflow or local-environment material')
 if text.count('```')%2:errors.append(path+': unbalanced code fence')
 for target in re.findall(r'\]\(([^\s)]+)\)',text):
  url=urlsplit(target)
  if url.scheme or url.netloc or not url.path:continue
  dest=(source.parent/unquote(url.path)).resolve()
  if not dest.is_relative_to(root) or not dest.exists():errors.append(path+': invalid local link '+target)
for error in errors:print(error,file=sys.stderr)
print(f'Documentation: {len(actual)} maintained English guides, {len(errors)} findings')
sys.exit(bool(errors))
