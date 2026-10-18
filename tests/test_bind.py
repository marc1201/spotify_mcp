"""spotify_mcp.server.bind_kwargs — where the server listens: a socket-activation fd
(spotify-mcp.socket), a unix socket, or 127.0.0.1:port — and /healthz answering on each for real."""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from spotify_mcp.server import BindError, bind_kwargs, build_app, describe_bind
from spotify_mcp.settings import Settings

SETTINGS = Settings(
    spotify_client_id="client-id",
    spotify_client_secret="not-a-real-secret",
    base_url="https://mcp.test",
    port=8080,
    allowed_user_ids=frozenset({"alice"}),
    jwt_signing_key="0" * 64,
)


# ---- the pure function ----------------------------------------------------------------------------
def test_default_is_loopback_port_from_settings():
    assert bind_kwargs({}, port=8080) == {"host": "127.0.0.1", "port": 8080}


def test_default_port_from_env_or_8080():
    assert bind_kwargs({}) == {"host": "127.0.0.1", "port": 8080}
    assert bind_kwargs({"PORT": " 9000 "}) == {"host": "127.0.0.1", "port": 9000}
    with pytest.raises(BindError):
        bind_kwargs({"PORT": "x"})


def test_spotify_mcp_socket_is_a_unix_path():
    assert bind_kwargs({"SPOTIFY_MCP_SOCKET": "/run/spotify-mcp/server.sock"}) == {
        "uds": "/run/spotify-mcp/server.sock"
    }
    assert bind_kwargs({"SPOTIFY_MCP_SOCKET": "  "}) == {"host": "127.0.0.1", "port": 8080}
    with pytest.raises(BindError):
        bind_kwargs({"SPOTIFY_MCP_SOCKET": "relative.sock"})


def test_socket_activation_wins_and_checks_the_pid():
    env = {"LISTEN_FDS": "1", "LISTEN_PID": "4242", "SPOTIFY_MCP_SOCKET": "/x.sock"}
    assert bind_kwargs(env, pid=4242) == {"fd": 3}
    assert bind_kwargs({"LISTEN_FDS": "1", "LISTEN_PID": str(os.getpid())}) == {"fd": 3}
    with pytest.raises(BindError, match="LISTEN_PID"):
        bind_kwargs(env, pid=1)
    with pytest.raises(BindError, match="LISTEN_PID"):
        bind_kwargs({"LISTEN_FDS": "1"}, pid=1)
    with pytest.raises(BindError, match="exactly one"):
        bind_kwargs({"LISTEN_FDS": "2", "LISTEN_PID": "7"}, pid=7)


def test_describe_bind_names_no_secret():
    assert describe_bind({"fd": 3}) == "socket-activation fd 3"
    assert describe_bind({"uds": "/a.sock"}) == "unix:/a.sock"
    assert describe_bind({"host": "127.0.0.1", "port": 1}) == "127.0.0.1:1"


# ---- /healthz on all three binds -------------------------------------------------------------------
class _Serving:
    def __init__(self, kw: dict):
        self.server = uvicorn.Server(
            uvicorn.Config(build_app(SETTINGS)[0], log_level="error", **kw)
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("uvicorn did not start")

    def __exit__(self, *_exc):
        self.server.should_exit = True
        self.thread.join(10)


def _healthz(conn: http.client.HTTPConnection) -> dict:
    conn.request("GET", "/healthz")
    resp = conn.getresponse()
    assert resp.status == 200
    return json.loads(resp.read())


class _UnixConn(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("localhost", timeout=10)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_healthz_over_loopback_port():
    port = _free_port()
    with _Serving(bind_kwargs({}, port=port)):
        got = _healthz(http.client.HTTPConnection("127.0.0.1", port, timeout=10))
    assert got == {"status": "ok", "service": "spotify-mcp"}


def test_healthz_over_unix_socket(tmp_path: Path):
    sock = tmp_path / "server.sock"
    with _Serving(bind_kwargs({"SPOTIFY_MCP_SOCKET": str(sock)})):
        got = _healthz(_UnixConn(str(sock)))
    assert got["status"] == "ok"


FD_CHILD = r"""
import os, socket, sys
from spotify_mcp.server import build_app, serve
from spotify_mcp.settings import Settings
path = sys.argv[1]
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.bind(path); s.listen(16)
os.dup2(s.fileno(), 3); os.set_inheritable(3, True)   # what systemd does for a .socket unit
os.environ["LISTEN_FDS"] = "1"; os.environ["LISTEN_PID"] = str(os.getpid())
os.environ["SPOTIFY_MCP_SOCKET"] = "/must/not/be/used.sock"
app, st = build_app(Settings(spotify_client_id="client-id", spotify_client_secret="not-a-real-secret",
    base_url="https://mcp.test", port=1, allowed_user_ids=frozenset({"alice"}),
    jwt_signing_key="0" * 64))
serve(app, st)
"""


def test_healthz_over_socket_activation_fd(tmp_path: Path):
    """serve() on fd 3 (LISTEN_FDS/LISTEN_PID set the way systemd sets them), in a child process so
    fd 3 of this interpreter is left alone."""
    sock = tmp_path / "activated.sock"
    child = subprocess.Popen(
        [sys.executable, "-c", FD_CHILD, str(sock)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        got = None
        for _ in range(200):
            if child.poll() is not None:
                break
            try:
                got = _healthz(_UnixConn(str(sock)))
                break
            except (OSError, http.client.HTTPException):
                time.sleep(0.05)
        assert got == {"status": "ok", "service": "spotify-mcp"}, child.stderr.read()
    finally:
        child.terminate()
        child.wait(10)
