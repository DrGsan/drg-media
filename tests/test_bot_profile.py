import asyncio
import io
import json
import time
from dataclasses import replace
from email.parser import BytesParser
from email.policy import default

import httpx
from PIL import Image
from sqlalchemy import select
from family_photo.bot_profile import BotProfile, NAME, NAMES, DESCRIPTION, DESCRIPTIONS, KEY, avatar, same_avatar
from family_photo.db import group_management


class TelegramProfile:
    def __init__(self):
        self.calls = []
        self.values = {language: dict(name=NAMES[language], description=DESCRIPTIONS[language], short_description=DESCRIPTIONS[language]) for language in ('', 'ru')}
        self.photo = self.compressed(avatar()[0])
        self.photo_id = 'original-photo'
        self.lost_photo_response = False
        self.hide_uploaded_photo = False
        self.failure = None

    @staticmethod
    def compressed(body):
        with Image.open(io.BytesIO(body)) as image:
            output = io.BytesIO()
            image.resize((640, 640)).save(output, 'JPEG', quality=75)
        return output.getvalue()

    async def handle(self, request):
        method = request.url.path.rsplit('/', 1)[-1]
        self.calls.append(method)
        if self.failure: return httpx.Response(self.failure, json={'ok': False, 'parameters': {'retry_after': 90}})
        if request.method == 'GET': return httpx.Response(200, content=self.photo)
        data = json.loads(request.content) if request.headers.get('content-type', '').startswith('application/json') else {}
        if method == 'getMe': result = dict(id=77, username='Family_DrGbot')
        elif method in {'getMyName', 'getMyDescription', 'getMyShortDescription'}:
            field = {'getMyName': 'name', 'getMyDescription': 'description', 'getMyShortDescription': 'short_description'}[method]
            result = {field: self.values[data['language_code']][field]}
        elif method in {'setMyName', 'setMyDescription', 'setMyShortDescription'}:
            field = {'setMyName': 'name', 'setMyDescription': 'description', 'setMyShortDescription': 'short_description'}[method]
            self.values[data['language_code']][field] = data[field]
            result = True
        elif method == 'getUserProfilePhotos':
            result = {'total_count': int(self.photo is not None), 'photos': [[dict(file_id=self.photo_id, file_unique_id=self.photo_id, width=640, height=640)]] if self.photo is not None else []}
        elif method == 'getFile': result = {'file_path': 'photos/current.jpg'}
        elif method == 'setMyProfilePhoto':
            parsed = BytesParser(policy=default).parsebytes(('Content-Type: '+request.headers['content-type']+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+request.content)
            parts = {part.get_param('name', header='content-disposition'): part for part in parsed.iter_parts()}
            assert json.loads(parts['photo'].get_payload(decode=True)) == {'type': 'static', 'photo': 'attach://avatar'}
            content = parts['avatar'].get_payload(decode=True)
            assert parts['avatar'].get_content_type() == 'image/jpeg' and content == avatar()[0]
            self.photo = self.compressed(content)
            self.photo_id = 'new-photo'
            if self.hide_uploaded_photo: self.photo = None
            if self.lost_photo_response:
                self.lost_photo_response = False
                raise httpx.ReadTimeout('lost response')
            result = True
        else: raise AssertionError('Unexpected Bot API method: '+method)
        return httpx.Response(200, json={'ok': True, 'result': result})

    def writes(self): return [name for name in self.calls if name.startswith('set')]


def tick(profile, remote):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(remote.handle)) as client: await profile.tick(client)
    asyncio.run(run())


def test_matching_profile_causes_no_writes_even_after_restart(app):
    messaging=app.extensions['site_messaging'];profile=messaging.profile;remote=TelegramProfile()
    tick(profile,remote)
    assert remote.writes()==[] and profile.public()['state']=='ready'
    count=len(remote.calls)
    tick(profile,remote);assert len(remote.calls)==count  # No periodic rechecks.
    remote.calls.clear();tick(BotProfile(messaging),remote)
    assert not remote.writes() and 'getUserProfilePhotos' in remote.calls
    assert 'getFile' not in remote.calls  # Stable ID of the verified photo.
    state=profile.public()
    assert 'token' not in state and 'photo_unique_id' not in state


def test_startup_corrects_only_changed_text_leaving_photo_alone(app):
    profile=app.extensions['site_messaging'].profile;remote=TelegramProfile()
    remote.values['ru']['description']='Другое описание'
    tick(profile,remote)
    assert remote.writes()==['setMyDescription']
    # A manual change while running waits for the next project startup.
    remote.values['']['name']='Changed';tick(profile,remote)
    assert remote.values['']['name']=='Changed'
    tick(BotProfile(profile.messaging),remote)
    assert remote.values['']['name']==NAME and remote.writes()==['setMyDescription','setMyName']


def test_missing_or_different_avatar_is_uploaded_once(app):
    profile=app.extensions['site_messaging'].profile;remote=TelegramProfile()
    remote.photo=None
    tick(profile,remote)
    assert remote.writes()==['setMyProfilePhoto'] and profile.public()['state']=='ready'
    tick(profile,remote);tick(BotProfile(profile.messaging),remote)
    assert remote.writes()==['setMyProfilePhoto']
    output=io.BytesIO();Image.new('RGB',(640,640),'blue').save(output,'JPEG')
    remote.photo=output.getvalue();remote.photo_id='manually-changed'
    tick(BotProfile(profile.messaging),remote)
    assert remote.writes()==['setMyProfilePhoto','setMyProfilePhoto']


def test_same_artwork_with_new_telegram_id_is_not_uploaded_again(app):
    profile=app.extensions['site_messaging'].profile;remote=TelegramProfile()
    tick(profile,remote)
    remote.photo_id='same-artwork-uploaded-by-owner'
    tick(BotProfile(profile.messaging),remote)
    assert 'getFile' in remote.calls and remote.writes()==[]


def test_ambiguous_upload_response_does_not_duplicate_avatar(app,monkeypatch):
    messaging=app.extensions['site_messaging'];profile=messaging.profile;remote=TelegramProfile()
    remote.photo=None;remote.lost_photo_response=True
    now=time.time();monkeypatch.setattr('family_photo.bot_profile.time.time',lambda:now)
    tick(profile,remote)
    assert profile.public()['state']=='retry' and remote.writes()==['setMyProfilePhoto']
    now+=61
    tick(BotProfile(messaging),remote)
    assert profile.public()['state']=='ready' and remote.writes()==['setMyProfilePhoto']


def test_rate_limits_invalid_token_and_transport_failures_are_safe(app,monkeypatch):
    messaging=app.extensions['site_messaging'];profile=messaging.profile;remote=TelegramProfile()
    remote.failure=429;tick(profile,remote)
    assert messaging.paused() and profile.public()['error']=='telegram_pause'
    before=len(remote.calls);tick(profile,remote);assert len(remote.calls)==before
    with profile.archive.transaction() as c:c.execute(group_management.delete().where(group_management.c.id.in_([KEY,'site-bot-pause'])))
    remote.failure=401;tick(BotProfile(messaging),remote)
    assert profile.public()['error']=='invalid_token' and not remote.writes()
    # Token change bypasses another token's profile cache, without persisting it.
    messaging.config=replace(messaging.config.base,bot_token='replacement-test-token')
    remote.failure=None;tick(BotProfile(messaging),remote)
    assert profile.public()['state']=='ready' and not remote.writes()


def test_unconfigured_bot_makes_no_requests_and_avatar_is_a_bundled_jpeg(app):
    messaging=app.extensions['site_messaging'];messaging.config=replace(messaging.config.base,bot_token='')
    remote=TelegramProfile();tick(BotProfile(messaging),remote)
    assert not remote.calls and messaging.profile.public()['state']=='not_configured'
    assert same_avatar(avatar()[0]) and not same_avatar(b'not an image')


def test_stale_telegram_photo_response_never_causes_an_upload_loop(app,monkeypatch):
    profile=app.extensions['site_messaging'].profile;remote=TelegramProfile()
    remote.photo=None;remote.hide_uploaded_photo=True
    now=time.time();monkeypatch.setattr('family_photo.bot_profile.time.time',lambda:now)
    tick(profile,remote)
    assert remote.writes()==['setMyProfilePhoto']
    for delta in (31,61,301,3601):
        now+=delta;tick(profile,remote)
    assert remote.writes()==['setMyProfilePhoto'] and profile.public()['state']=='verification_failed'
    count=len(remote.calls);tick(profile,remote);assert len(remote.calls)==count


def test_site_worker_runs_profile_check_once_and_keeps_other_work(app,monkeypatch):
    from unittest.mock import AsyncMock
    messaging=app.extensions['site_messaging'];remote=TelegramProfile()
    monkeypatch.setattr(messaging,'check_access',AsyncMock())
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(remote.handle)) as client:
            await messaging.tick(client)
            count=len(remote.calls)
            await messaging.tick(client)
            assert len(remote.calls)==count
    asyncio.run(run())
    assert messaging.check_access.await_count==2*len(messaging.config.archive_group_ids)


def test_legacy_name_and_description_become_localized_without_reuploading_artwork(app):
    profile=app.extensions['site_messaging'].profile;remote=TelegramProfile()
    for values in remote.values.values():
        values.update(name='DrG Family', description='Семейные фотографии, воспоминания и общение через DrG Space.', short_description='Семейные фотографии, воспоминания и общение через DrG Space.')
    tick(profile,remote)
    assert remote.values['']['name']=='Media' and remote.values['ru']['name']=='Медиа'
    assert len(remote.writes())==6 and 'setMyProfilePhoto' not in remote.writes()
    assert profile.public()['username']=='Family_DrGbot' and profile.public()['state']=='ready'
    remote.calls.clear();tick(BotProfile(profile.messaging),remote)
    assert not remote.writes()
