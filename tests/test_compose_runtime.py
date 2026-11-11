"""Deployment names and safe storage selection without reading a working .env."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def configuration(tmp_path, settings=None, overlays=()):
    if not shutil.which('docker'):
        pytest.skip('Docker Compose CLI is required')
    env = {k: v for k, v in os.environ.items() if k in ('PATH', 'HOME', 'TMPDIR', 'DOCKER_CONFIG')}
    target = tmp_path / '.env'
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/init-env.py'), '--output', str(target)],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0
    command = ['docker', 'compose', '--project-directory', str(tmp_path), '--env-file', str(target),
               '-f', str(ROOT / 'compose.yaml')]
    for file in overlays:
        command += ['-f', str(ROOT / file)]
    result = subprocess.run(command + ['config', '--format', 'json'], env=env | (settings or {}),
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, 'Compose configuration failed'
    return json.loads(result.stdout)


def test_fresh_names_and_managed_storage(tmp_path):
    config = configuration(tmp_path)
    assert config['name'] == 'drg-media'
    assert config['services']['api']['container_name'] == 'drg-media-api'
    assert config['services']['worker']['container_name'] == 'drg-media-worker'
    assert config['services']['api']['image'] == config['services']['worker']['image'] == 'drg-media:local'
    assert config['services']['worker']['command'] == ['drg-media', 'worker']
    assert config['volumes']['archive-data'] == {'name': 'drg-media_archive-data'}


def test_project_rename_pins_both_existing_volumes_and_preserves_database_identity(tmp_path):
    config = configuration(tmp_path, {'DRG_MEDIA_EXTERNAL_VOLUMES': 'true',
                                      'DRG_MEDIA_DATA_VOLUME': 'previous_archive-data',
                                      'DRG_MEDIA_POSTGRES_VOLUME': 'previous_archive-postgres'},
                           ('compose.postgres.yaml',))
    assert config['volumes']['archive-data'] == {'name': 'previous_archive-data', 'external': True}
    assert config['volumes']['archive-postgres'] == {'name': 'previous_archive-postgres', 'external': True}
    assert config['services']['api']['environment']['FP_DATABASE_URL'].endswith('@drg-media-postgres:5432/family_photo')
    assert config['services']['postgres']['environment']['POSTGRES_DB'] == 'family_photo'
    assert config['services']['postgres']['environment']['POSTGRES_USER'] == 'family_photo'


@pytest.mark.parametrize('settings,expected', [({}, 'drg-space_default'),
    ({'DRG_SITE_NETWORK': 'previous_default'}, 'previous_default'),
    ({'DRG_SITE_NETWORK': 'previous_default', 'DRG_SPACE_NETWORK': 'current_default'}, 'current_default')])
def test_new_and_legacy_network_settings_keep_the_compatible_alias(tmp_path, settings, expected):
    config = configuration(tmp_path, settings, ('compose.site.yaml',))
    assert config['networks']['drg-space']['name'] == expected
    assert config['networks']['drg-space']['external'] is True
    assert set(config['services']['api']['networks']['drg-space']['aliases']) == {'drg-media', 'family-photo'}
