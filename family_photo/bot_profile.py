"""One startup reconciliation of the configured bot's approved public profile."""
import hashlib
import io
import json
import logging
import re
import time
from functools import lru_cache
from importlib.resources import files

import httpx
from PIL import Image, ImageChops, ImageFilter, ImageOps, ImageStat
from sqlalchemy import select
from .db import group_management

NAME = 'Media'
NAMES = {'': NAME, 'ru': 'Медиа'}
DESCRIPTION = 'Photos, videos, memories and conversations through DrG Space.'
DESCRIPTIONS = {'': DESCRIPTION, 'ru': 'Фотографии, видео, воспоминания и общение через DrG Space.'}
LANGUAGES = ('', 'ru')
KEY = 'site-bot-profile'
log = logging.getLogger('family_photo.bot_profile')


@lru_cache(maxsize=1)
def avatar():
    original = files('family_photo').joinpath('assets/bot-avatar.png').read_bytes()
    with Image.open(io.BytesIO(original)) as image:
        image = image.convert('RGB')
        image.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.save(output, 'JPEG', quality=95, optimize=True)
    target = hashlib.sha256(original + json.dumps([NAMES, DESCRIPTIONS, LANGUAGES]).encode()).hexdigest()
    return output.getvalue(), target


def same_avatar(content):
    # Telegram resizes/recompresses profile photos; byte hashes cannot match.
    # Compare the actual picture, then remember its stable Telegram unique ID.
    if len(content) > 2 * 1024 * 1024: return False
    try:
        def sample(raw):
            with Image.open(io.BytesIO(raw)) as image:
                if image.width * image.height > 4_000_000: return None
                return ImageOps.fit(image.convert('RGB'), (96, 96), method=Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(.6))
        expected, current = sample(avatar()[0]), sample(content)
        if current is None: return False
        difference = ImageStat.Stat(ImageChops.difference(expected, current))
        return max(difference.rms) <= 4
    except (OSError, ValueError, Image.DecompressionBombError):
        return False


class ProfileError(Exception):
    def __init__(self, reason, status=0): self.reason, self.status = reason, status


class BotProfile:
    def __init__(self, messaging):
        self.messaging = messaging
        self.archive = messaging.archive
        self.completed = None
        self.photo_attempted = None

    def read(self):
        with self.archive.engine.connect() as c:
            raw = c.execute(select(group_management.c.value).where(group_management.c.id == KEY)).scalar()
        return json.loads(raw) if raw else {}

    def save(self, state):
        with self.archive.transaction() as c:
            if c.execute(select(group_management.c.id).where(group_management.c.id == KEY)).first():
                c.execute(group_management.update().where(group_management.c.id == KEY).values(value=json.dumps(state)))
            else:
                c.execute(group_management.insert().values(id=KEY, value=json.dumps(state)))

    def public(self):
        token = self.messaging.config.bot_token
        state = self.read() if token else {}
        if not token: return {'state': 'not_configured'}
        if state.get('token') != hashlib.sha256(token.encode()).hexdigest(): return {'state': 'checking'}
        return {key: state[key] for key in ('state', 'username', 'name', 'checked_at', 'retry_at', 'error') if key in state}

    async def tick(self, http=None):
        token = self.messaging.config.bot_token
        if not token: return
        fingerprint = hashlib.sha256(token.encode()).hexdigest()
        if self.completed == fingerprint or self.messaging.paused(): return
        # Also safe for several API processes starting together.
        with self.archive.objects.lease('botprofile/' + '0'*32, blocking=False) as acquired:
            if not acquired: return
            state = self.read()
            if state.get('token') != fingerprint: state = {'token': fingerprint}
            if state.get('retry_at', 0) > time.time(): return
            try:
                photo, target = avatar()
                if state.get('target') != target:
                    state = {'token': fingerprint, 'target': target}
                async with httpx.AsyncClient(timeout=20, follow_redirects=False) as default:
                    client = http or default
                    base = 'https://api.telegram.org/bot' + token + '/'

                    async def call(method, body=None, **kwargs):
                        response = await client.post(base + method, **({'json': body or {}} if not kwargs else kwargs))
                        data = response.json()
                        if response.status_code == 429:
                            self.messaging.pause(data.get('parameters', {}).get('retry_after', 60))
                            raise ProfileError('telegram_pause', 429)
                        if response.status_code != 200 or not data.get('ok'):
                            raise ProfileError('invalid_token' if response.status_code == 401 else 'telegram_rejected', response.status_code)
                        if method.startswith('set') and data.get('result') is not True: raise ProfileError('invalid_response')
                        return data['result']

                    me = await call('getMe')
                    state.update(bot_id=me['id'], username=me['username'])
                    for language in LANGUAGES:
                        for getter, setter, field, expected in (
                            ('getMyName', 'setMyName', 'name', NAMES[language]),
                            ('getMyDescription', 'setMyDescription', 'description', DESCRIPTIONS[language]),
                            ('getMyShortDescription', 'setMyShortDescription', 'short_description', DESCRIPTIONS[language])):
                            current = await call(getter, {'language_code': language})
                            if current[field] != expected:
                                await call(setter, {field: expected, 'language_code': language})

                    async def current_photo():
                        value = await call('getUserProfilePhotos', {'user_id': me['id'], 'limit': 1})
                        sizes = value['photos'][0] if value['photos'] else []
                        return max(sizes, key=lambda p: p['width']*p['height']) if sizes else None

                    async def matches(current):
                        if not current: return False
                        if state.get('photo_unique_id') == current['file_unique_id']: return True
                        value = await call('getFile', {'file_id': current['file_id']})
                        path = value['file_path']
                        if not re.fullmatch(r'[A-Za-z0-9_./-]+', path) or '..' in path.split('/'): raise ProfileError('invalid_photo_path')
                        async with client.stream('GET', 'https://api.telegram.org/file/bot' + token + '/' + path) as response:
                            if response.status_code != 200: raise ProfileError('photo_unavailable')
                            content = bytearray()
                            async for chunk in response.aiter_bytes():
                                content.extend(chunk)
                                if len(content) > 2*1024*1024: return False
                        return same_avatar(bytes(content))

                    current = await current_photo()
                    if not await matches(current):
                        # Persist intent first. A lost response or process crash
                        # must not turn a retry into repeated avatar uploads.
                        if state.get('photo_set_at', 0) > time.time() - 300:
                            state.update(state='checking', retry_at=int(time.time()) + 30, error=None)
                            self.save(state)
                            return
                        if self.photo_attempted == fingerprint:
                            # A successful/ambiguous upload is never repeated in
                            # a loop if Telegram keeps returning an old picture.
                            state.update(state='verification_failed', retry_at=0, error='photo_not_confirmed')
                            self.save(state)
                            self.completed = fingerprint
                            log.warning('Bot profile photo was not confirmed; next check is on startup')
                            return
                        state.update(photo_set_at=int(time.time()), photo_unique_id=None)
                        self.save(state)
                        self.photo_attempted = fingerprint
                        try:
                            await call('setMyProfilePhoto', data={'photo': json.dumps({'type': 'static', 'photo': 'attach://avatar'})},
                                files={'avatar': ('media.jpg', photo, 'image/jpeg')})
                        except ProfileError as error:
                            if 400 <= error.status < 500 and error.status != 429:
                                state.pop('photo_set_at', None)
                                self.photo_attempted = None
                            raise
                        current = await current_photo()
                        if not await matches(current):
                            state.update(state='checking', retry_at=int(time.time()) + 30, error=None)
                            self.save(state)
                            return
                    state.update(state='ready', name=NAME, photo_unique_id=current['file_unique_id'],
                        checked_at=int(time.time()), retry_at=0, error=None, failures=0)
                    state.pop('photo_set_at', None)
                    self.save(state)
                    self.completed = fingerprint
                    log.info('Bot profile checked (@%s)', state['username'])
            except Exception as error:
                reason = error.reason if isinstance(error, ProfileError) else 'telegram_unavailable'
                failures = state.get('failures', 0) + 1
                state.update(state='retry', error=reason, failures=failures,
                    retry_at=int(time.time()) + (900 if reason == 'invalid_token' else min(3600, 30*2**min(failures, 7))))
                self.save(state)
                log.warning('Bot profile check postponed (%s)', reason)
