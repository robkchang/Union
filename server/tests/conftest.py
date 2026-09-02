"""Starts a real uvicorn server on a free port so SSE streams behave as in
production. Provides a logged-in browser session and a union with a key."""
from __future__ import annotations

import re
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from union_server import config, create_app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def server(tmp_path_factory):
    port = _free_port()
    data_dir = tmp_path_factory.mktemp("data")
    settings = config.Settings(
        server=config.ServerSettings(host="127.0.0.1", port=port, public_url=f"http://127.0.0.1:{port}"),
        data=config.DataSettings(dir=str(data_dir)),
        session=config.SessionSettings(secret_key="t" * 64),
        security=config.SecuritySettings(registration_code="OPEN-SESAME", register_rate_limit="20 per hour",
                                         bad_code_rate_limit="3 per hour"),
        relay=config.RelaySettings(ping_seconds=0.5, delivery_window_seconds=1.0, blob_window_seconds=30),
    )
    app = create_app(settings)
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(cfg)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    deadline = time.time() + 10
    while not srv.started:
        if time.time() > deadline:
            raise RuntimeError("server did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(5)


def _csrf(html: str) -> str:
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert m, "no csrf meta"
    return m.group(1)


class Web:
    """A browser session against the control plane."""

    def __init__(self, base_url: str):
        self.c = httpx.Client(base_url=base_url, follow_redirects=False, timeout=10)
        self.csrf = ""

    def get(self, path: str) -> httpx.Response:
        r = self.c.get(path)
        if "csrf-token" in r.text:
            self.csrf = _csrf(r.text)
        return r

    def form(self, path: str, **data) -> httpx.Response:
        if not self.csrf:
            self.get("/login")
        return self.c.post(path, data={"csrf_token": self.csrf, **data})

    def api(self, path: str, body: dict | None = None) -> httpx.Response:
        if body is None:
            return self.c.get(path)
        return self.c.post(path, json=body, headers={"X-CSRF-Token": self.csrf})

    def register_and_login(self, username: str, password: str = "correct horse", code: str = "OPEN-SESAME") -> None:
        self.get("/register")
        r = self.form("/register", username=username, password=password, confirm=password, code=code)
        assert r.status_code == 303, r.text
        self.get("/login")
        r = self.form("/login", username=username, password=password)
        assert r.status_code == 303 and r.headers["location"] == "/unions", r.text
        self.get("/unions")  # refresh csrf for the logged-in session

    def create_union(self, name: str) -> dict:
        r = self.form("/unions", name=name)
        assert r.status_code == 303, r.text
        union_id = r.headers["location"].rsplit("/", 1)[1]
        state = self.api(f"/unions/{union_id}/api/state").json()
        return state["union"]


@pytest.fixture(scope="session")
def web(server) -> Web:
    w = Web(server)
    w.register_and_login("rob")
    return w


@pytest.fixture
def union(web) -> dict:
    return web.create_union("Home")
