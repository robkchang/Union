"""A live Union server with one user and one union, for node tests."""
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
    settings = config.Settings(
        server=config.ServerSettings(host="127.0.0.1", port=port, public_url=f"http://127.0.0.1:{port}"),
        data=config.DataSettings(dir=str(tmp_path_factory.mktemp("data"))),
        session=config.SessionSettings(secret_key="t" * 64),
        security=config.SecuritySettings(register_rate_limit="20 per hour"),
        relay=config.RelaySettings(ping_seconds=0.5, delivery_window_seconds=1.0, blob_window_seconds=30),
    )
    app = create_app(settings)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
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


@pytest.fixture(scope="session")
def union(server) -> dict:
    """Returns {'url', 'join_key', 'id', 'web'} where web is a logged-in browser client."""
    c = httpx.Client(base_url=server, follow_redirects=False, timeout=10)
    csrf = re.search(r'name="csrf-token" content="([^"]+)"', c.get("/register").text).group(1)
    assert c.post("/register", data={"csrf_token": csrf, "username": "rob", "password": "correct horse",
                                     "confirm": "correct horse"}).status_code == 303
    csrf = re.search(r'name="csrf-token" content="([^"]+)"', c.get("/login").text).group(1)
    assert c.post("/login", data={"csrf_token": csrf, "username": "rob", "password": "correct horse"}).status_code == 303
    csrf = re.search(r'name="csrf-token" content="([^"]+)"', c.get("/unions").text).group(1)
    r = c.post("/unions", data={"csrf_token": csrf, "name": "Test"})
    union_id = r.headers["location"].rsplit("/", 1)[1]
    state = c.get(f"/unions/{union_id}/api/state").json()
    return {"url": server, "join_key": state["union"]["join_key"], "id": union_id, "web": c, "csrf": csrf}
