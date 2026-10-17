import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from urllib.error import HTTPError

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
import main as app  # noqa: E402


@pytest.fixture()
def base(monkeypatch):
    monkeypatch.setattr(app.Handler, "counter", 0)
    server = app.make_server(0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read().decode()
    except HTTPError as e:
        return e.code, e.read().decode()


def test_health_and_root(base, monkeypatch):
    monkeypatch.setenv("APP_VERSION", "7")
    monkeypatch.setenv("CONTAINER_APP_REVISION", "orders--v7")
    assert get(base + "/healthz") == (200, "ok")
    status, body = get(base + "/")
    assert status == 200 and json.loads(body) == {"version": "7", "revision": "orders--v7"}
    assert get(base + "/nope")[0] == 404


def test_fail_every_makes_exactly_every_nth_request_fail(base, monkeypatch):
    monkeypatch.setenv("FAIL_EVERY", "3")
    codes = [get(base + "/")[0] for _ in range(9)]
    assert codes == [200, 200, 500, 200, 200, 500, 200, 200, 500]


def test_health_checks_never_count_toward_the_failure_cycle(base, monkeypatch):
    monkeypatch.setenv("FAIL_EVERY", "2")
    for _ in range(5):
        assert get(base + "/healthz")[0] == 200
    assert [get(base + "/")[0] for _ in range(2)] == [200, 500]


def test_logs_are_json_with_a_severity_field(base, capsys):
    get(base + "/")
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines and all("severity" in line and "message" in line for line in lines)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_ready(port, proc, seconds=10):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"the app exited early: {proc.stdout.read()}")
        try:
            if get(f"http://127.0.0.1:{port}/healthz")[0] == 200:
                return
        except OSError:
            time.sleep(0.1)
    raise AssertionError("the app never became ready")


def test_the_app_listens_on_the_port_it_is_given():
    port = free_port()
    proc = subprocess.Popen([sys.executable, str(ROOT / "app" / "main.py")], env={**os.environ, "PORT": str(port)},
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        wait_ready(port, proc)
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no SIGTERM handler semantics; CI covers this on Linux")
def test_sigterm_drains_and_exits_zero_quickly():
    port = free_port()
    proc = subprocess.Popen([sys.executable, str(ROOT / "app" / "main.py")], env={**os.environ, "PORT": str(port)},
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        wait_ready(port, proc)
        started = time.time()
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=8)
        assert proc.returncode == 0 and time.time() - started < 5
        assert "stopped cleanly" in out
    finally:
        if proc.poll() is None:
            proc.kill()
