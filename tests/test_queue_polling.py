import asyncio

from family_photo.queue_polling import IdlePoller
from conftest import photo
from test_site_messaging import messaging


class Event:
    def __init__(self):self.delays=[];self.signalled=False
    def set(self):self.signalled=True
    def wait(self,delay):self.delays.append(delay);return self.signalled
    def clear(self):self.signalled=False


def test_idle_backoff_is_bounded_and_wakeup_or_work_restores_fast_polling():
    for intervals in ((2,5,10),(2,5,15,30)):
        event=Event();poller=IdlePoller(intervals,event)
        for _ in range(6):poller.wait(False)
        assert event.delays==list(intervals)+[intervals[-1]]*(6-len(intervals))
        poller.wake();poller.wait(False);poller.wait(False)
        assert event.delays[-1]==2
        poller.wait(True);assert event.delays[-1]==2


def test_successful_mutations_wake_only_their_queue(client,app):
    messages=app.extensions['queue_pollers']['messages'].event
    exports=app.extensions['queue_pollers']['exports'].event
    assert not messages.is_set() and not exports.is_set()
    client.get('/healthz');assert not messages.is_set()
    response=client.put('/v1/site-permissions',json={'revision':1,'actors':{}})
    assert response.status_code==200 and messages.is_set() and not exports.is_set()
    messages.clear()
    assert client.post('/v1/exports',json={}).status_code==422
    assert not exports.is_set() and not messages.is_set()


def test_export_queue_reports_work_and_wakes_on_enqueue(client,app,archive,config):
    from test_copies_exports import ready
    ready(archive,config)
    poller=app.extensions['queue_pollers']['exports']
    response=client.post('/v1/exports?actor_id='+'a'*32,json={'groups':['-1001'],'videos':False,'request_id':'b'*32})
    assert response.status_code==202 and poller.event.is_set()
    exports=app.extensions['exports']
    assert exports.tick() is True
    assert exports.tick() is False


def test_message_queue_reports_work_then_idle(messaging):
    from test_site_messaging import send
    service,bot=messaging
    send(service)
    assert asyncio.run(service.tick(bot)) is True
    assert asyncio.run(service.tick(bot)) is False
