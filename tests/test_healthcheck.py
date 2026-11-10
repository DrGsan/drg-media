from contextlib import contextmanager
from pathlib import Path
import socket
import subprocess
import threading
import shutil

import pytest


SCRIPT=Path(__file__).resolve().parents[1]/'family_photo/healthcheck.sh'


@contextmanager
def server(response, *, stall=False):
    received=[]
    stop=threading.Event()
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0))
        listener.listen(1)
        listener.settimeout(3)
        def serve():
            try:
                conn,_=listener.accept()
                with conn:
                    conn.settimeout(3)
                    data=b''
                    while b'\r\n\r\n' not in data:
                        block=conn.recv(4096)
                        if not block:return
                        data+=block
                    received.append(data)
                    conn.sendall(response)
                    if stall:stop.wait(3)
            except (OSError,TimeoutError):
                pass
        thread=threading.Thread(target=serve,daemon=True)
        thread.start()
        try:
            yield listener.getsockname()[1],received
        finally:
            stop.set()
            thread.join(timeout=4)
            assert not thread.is_alive()


@pytest.mark.parametrize('response,healthy',[
    (b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{"status":"ok"}',True),
    (b'HTTP/1.0 200 OK\r\n\r\n',True),
    (b'HTTP/1.1 503 Unavailable\r\n\r\n',False),
    (b'HTTP/1.1 401 Unauthorized\r\n\r\n',False),
    (b'HTTP/1.1 302 Found\r\nLocation: /healthz\r\n\r\n',False),
    (b'HTTP/1.1 2000 invalid\r\n\r\n',False),
    (b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n',False),
    (b'HTTP/1.1 200 OK',False),
    (b'',False),
])
def test_healthcheck_requires_successful_complete_http_headers(response,healthy):
    with server(response) as (port,received):
        result=subprocess.run(['/bin/bash',str(SCRIPT),str(port)],capture_output=True,timeout=4)
    assert (result.returncode==0)==healthy
    assert received==[b'GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n']


def test_healthcheck_fails_when_port_is_closed():
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1',0))
        port=reserved.getsockname()[1]
        try:
            result=subprocess.run(['/bin/bash',str(SCRIPT),str(port)],capture_output=True,timeout=.3)
        except subprocess.TimeoutExpired:
            # Compose's outer timeout also reports a stalled connect as failure.
            return
    assert result.returncode!=0


def test_healthcheck_does_not_accept_a_stalled_header():
    with server(b'HTTP/1.1 200 OK\r\nX-Test: unfinished',stall=True) as (port,_):
        with pytest.raises(subprocess.TimeoutExpired):
            subprocess.run(['/bin/bash',str(SCRIPT),str(port)],capture_output=True,timeout=.2)


@pytest.mark.skipif(not shutil.which('timeout'),reason='GNU timeout is verified in the Debian image')
def test_outer_timeout_stops_the_entire_stalled_check():
    with server(b'',stall=True) as (port,_):
        result=subprocess.run(['timeout','--kill-after=1s','.2s','/bin/bash',str(SCRIPT),str(port)],
                              capture_output=True,timeout=2)
    assert result.returncode==124
