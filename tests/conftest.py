from dataclasses import replace
import os
import uuid
import pytest
from family_photo.config import Config
from family_photo.db import database
from family_photo.objects import Objects
from family_photo.archive import Archive
from family_photo.api import create_app


@pytest.fixture
def config(tmp_path,monkeypatch):
    url='sqlite:///'+str(tmp_path/'archive.sqlite3')
    admin=None
    if remote:=os.getenv('FP_TEST_DATABASE_URL'):
        from sqlalchemy import create_engine
        from urllib.parse import quote
        admin=create_engine(remote)
        schema='fp_test_'+uuid.uuid4().hex
        with admin.begin() as c:c.exec_driver_sql('CREATE SCHEMA '+schema)
        url=remote+('&' if '?' in remote else '?')+'options='+quote('-csearch_path='+schema)
    storage=os.getenv('FP_TEST_STORAGE','local')
    mock=None
    if storage=='s3':
        import boto3
        from moto import mock_aws
        mock=mock_aws();mock.start()
        monkeypatch.setenv('AWS_ACCESS_KEY_ID','test-only-key')
        monkeypatch.setenv('AWS_SECRET_ACCESS_KEY','test-only-secret')
        boto3.client('s3',region_name='us-east-1').create_bucket(Bucket='family-photo-test-archive')
    yield Config(tmp_path, url, 'test-service-token-'*3,storage=storage,bucket='family-photo-test-archive' if mock else '',
                 group_ids=('-1001','-1002'), bot_token='test-bot-token')
    if mock:mock.stop()
    if admin:
        with admin.begin() as c:c.exec_driver_sql('DROP SCHEMA '+schema+' CASCADE')
        admin.dispose()


@pytest.fixture
def archive(config):
    value=Archive(database(config.database_url), Objects(config))
    value.add_group('-1001','Семья 1',kind='supergroup')
    value.add_group('-1002','Семья 2',kind='supergroup')
    yield value
    value.engine.dispose()


@pytest.fixture
def app(config,archive):
    return create_app(config,archive)


@pytest.fixture
def client(app,config):
    client=app.test_client()
    client.environ_base['HTTP_AUTHORIZATION']='Bearer '+config.service_token
    return client


def message(mid=1,**changes):
    return dict(id=mid,created=1700000000+mid,edited=0,sender_id='42',sender='Анна',text='Семейное фото',**{})|changes


def photo(mid=1,**changes):
    return message(mid,media=dict(identity=str(mid),kind='photo',mime='image/jpeg',name='photo.jpg',size=10),**changes)
