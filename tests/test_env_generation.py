from pathlib import Path
import os
import subprocess
import sys

SCRIPT=Path(__file__).resolve().parents[1]/'scripts/init-env.py'


def generate(path):
    return subprocess.run([sys.executable,str(SCRIPT),'--output',str(path)],capture_output=True,text=True)


def values(path):
    return dict(line.split('=',1) for line in path.read_text().splitlines() if line and not line.startswith('#') and '=' in line)


def test_fresh_private_env_is_complete_and_idempotent(tmp_path):
    path=tmp_path/'private.env'
    assert generate(path).returncode==0
    first=path.read_bytes();data=values(path)
    assert len(data['FP_SERVICE_TOKEN'])>=32
    assert len(data['FP_POSTGRES_PASSWORD'])>=24
    assert data['FP_STORAGE']=='local' and data['FP_REDIS_URL']==''
    assert os.stat(path).st_mode&0o777==0o600
    assert generate(path).returncode==0 and path.read_bytes()==first


def test_working_secrets_and_custom_keys_are_preserved(tmp_path):
    path=tmp_path/'private.env'
    path.write_text('FP_SERVICE_TOKEN=existing-secret-for-fixture-only\nFP_POSTGRES_PASSWORD=unchanged\nCUSTOM_SETTING="with spaces"\nFP_STORAGE=s3\n')
    assert generate(path).returncode==0
    data=values(path)
    assert data['FP_SERVICE_TOKEN']=='existing-secret-for-fixture-only'
    assert data['FP_POSTGRES_PASSWORD']=='unchanged'
    assert data['CUSTOM_SETTING']=='"with spaces"' and data['FP_STORAGE']=='s3'
    first=path.read_bytes();assert generate(path).returncode==0 and path.read_bytes()==first


def test_duplicate_key_refuses_without_replacement(tmp_path):
    path=tmp_path/'private.env';path.write_text('FP_SERVICE_TOKEN=first\nFP_SERVICE_TOKEN=second\n');first=path.read_bytes()
    assert generate(path).returncode!=0 and path.read_bytes()==first


def test_symlink_refuses_without_replacing_destination(tmp_path):
    destination=tmp_path/'destination';destination.write_text('original')
    path=tmp_path/'private.env';path.symlink_to(destination)
    assert generate(path).returncode!=0 and path.is_symlink() and destination.read_text()=='original'
