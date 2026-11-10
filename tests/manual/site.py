import os
import secrets
import sys
import tempfile
from pathlib import Path
os.environ.update(DATABASE_URL='',DATABASE_SCHEMA='fp_browser',DATA_ENCRYPTION_ENABLED='true',
 DATA_ENCRYPTION_KEY='dGVzdC1vbmx5LWVuY3J5cHRpb24ta2V5LTMyLWJ5dGU=',APP_SECRET=secrets.token_urlsafe(48),
 SETUP_TOKEN='browser-test-setup-key',PUBLIC_URL='http://127.0.0.1:8017',DATA_DIR=tempfile.mkdtemp(prefix='fp-site-browser-'),
 FAMILY_PHOTO_URL='http://127.0.0.1:8097',FAMILY_PHOTO_TOKEN='browser-service-token-'*3)
os.chdir(os.getenv('FP_SITE_REPO', str(Path(__file__).resolve().parents[3]/'drg-space')))
os.execv(sys.executable,[sys.executable,'-m','gunicorn','--workers','1','--threads','16','--bind','127.0.0.1:8017','app.main:app'])
