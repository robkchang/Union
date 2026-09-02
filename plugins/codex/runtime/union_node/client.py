"""Signed HTTP and the event stream, on the standard library only. Everything
here maps one-to-one onto the routes in the server's client guide."""
from __future__ import annotations

import http.client
import json
import socket
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from union_protocol import signing
from union_protocol.keys import NodeKeys, b64e


class UnionError(Exception):
    def __init__(self, status: int, code: str, detail: str = ""):
        super().__init__(f"{status} {code}: {detail}")
        self.status = status
        self.code = code
        self.detail = detail


def _error_from(status: int, body: bytes) -> UnionError:
    try:
        data = json.loads(body)
        return UnionError(status, str(data.get("error", "error")), str(data.get("detail", "")))
    except ValueError:
        return UnionError(status, "error", body[:200].decode("utf-8", "replace"))


class Client:
    def __init__(self, url: str, keys: NodeKeys, node_id: str, timeout: float = 15):
        self.url = url.rstrip("/")
        self.keys = keys
        self.node_id = node_id
        self.timeout = timeout

    def close(self) -> None:
        pass

    # ── signed requests ──────────────────────────────────────────────────

    def _headers(self, method: str, path: str, body: bytes) -> dict[str, str]:
        return signing.sign_request(self.keys.signing, self.node_id, method, path, body)

    def _do(self, method: str, path: str, body: bytes, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        req = urllib.request.Request(self.url + path, data=body if body else None, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def req(self, method: str, path: str, json_body: dict | None = None, content: bytes | None = None) -> bytes:
        body = content if content is not None else (
            json.dumps(json_body, separators=(",", ":")).encode() if json_body is not None else b"")
        headers = self._headers(method, path, body)
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        elif content is not None:
            headers["Content-Type"] = "application/octet-stream"
        status, data = self._do(method, path, body, headers, self.timeout)
        if status >= 400:
            raise _error_from(status, data)
        return data

    def req_json(self, method: str, path: str, json_body: dict | None = None) -> dict:
        data = self.req(method, path, json_body)
        return json.loads(data) if data else {}

    # ── routes ───────────────────────────────────────────────────────────

    def hub(self) -> dict:
        status, data = self._do("GET", "/api/v1/hub", b"", {}, self.timeout)
        if status >= 400:
            raise _error_from(status, data)
        return json.loads(data)

    def join(self, join_key: str, name: str, mode: str, machine: str, harness: str, cwd: str | None) -> dict:
        return self.req_json("POST", "/api/v1/join", {
            "join_key": join_key, "name": name, "mode": mode,
            "signing_pub": b64e(self.keys.signing_pub), "kx_pub": b64e(self.keys.kx_pub),
            "machine": machine, "harness": harness, "cwd": cwd,
        })

    def rotate(self, new: NodeKeys) -> dict:
        statement = {"node_id": self.node_id, "signing_pub": b64e(new.signing_pub), "kx_pub": b64e(new.kx_pub)}
        body = {"signing_pub": statement["signing_pub"], "kx_pub": statement["kx_pub"],
                "proof": signing.sign_json(new.signing, statement),
                "rotation_sig": signing.sign_json(self.keys.signing, statement)}
        roster = self.req_json("POST", "/api/v1/rotate", body)
        self.keys = new
        return roster

    def roster(self) -> dict:
        return self.req_json("GET", "/api/v1/roster")

    def agents(self) -> dict:
        return self.req_json("GET", "/api/v1/agents")

    def presence(self, **fields) -> dict:
        return self.req_json("PATCH", "/api/v1/presence", {k: v for k, v in fields.items() if v is not None})

    def leave(self) -> None:
        self.req("POST", "/api/v1/leave")

    def send(self, envelope: dict) -> dict:
        return self.req_json("POST", "/api/v1/messages", envelope)

    def ack(self, message_id: str) -> None:
        self.req("POST", f"/api/v1/messages/{message_id}/ack", {"state": "delivered"})

    def put_blob(self, blob_id: str, ciphertext: bytes) -> dict:
        return json.loads(self.req("PUT", f"/api/v1/blobs/{blob_id}", content=ciphertext))

    def get_blob(self, blob_id: str) -> bytes:
        return self.req("GET", f"/api/v1/blobs/{blob_id}")

    # ── event stream ─────────────────────────────────────────────────────

    def stream(self, on_event: Callable[[str, dict], None], on_connect: Callable[[], None] | None,
               stop: threading.Event, log: Callable[[str], None] = lambda s: None) -> None:
        """Blocking. Reconnects with backoff until `stop` is set or the
        server says we are evicted. The server pings every 20 s; a 90 s read
        timeout therefore means the connection is dead."""
        backoff = 1.0
        path = "/api/v1/events"
        while not stop.is_set():
            try:
                headers = self._headers("GET", path, b"")
                req = urllib.request.Request(self.url + path, method="GET", headers=headers)
                try:
                    resp = urllib.request.urlopen(req, timeout=90)
                except urllib.error.HTTPError as exc:
                    err = _error_from(exc.code, exc.read())
                    if err.code == "evicted":
                        on_event("evicted", {"reason": err.detail or "evicted"})
                        return
                    raise err
                with resp:
                    backoff = 1.0
                    if on_connect:
                        on_connect()
                    event, data = None, ""
                    while not stop.is_set():
                        raw = resp.readline()
                        if not raw:
                            break
                        line = raw.decode("utf-8", "replace").rstrip("\r\n")
                        if line.startswith("event:"):
                            event = line[6:].strip()
                        elif line.startswith("data:"):
                            data += line[5:].strip()
                        elif line == "" and event:
                            on_event(event, json.loads(data or "{}"))
                            if event == "evicted":
                                return
                            event, data = None, ""
                    if event:  # closed right after the last event
                        on_event(event, json.loads(data or "{}"))
                        if event == "evicted":
                            return
            except (UnionError, urllib.error.URLError, http.client.HTTPException, socket.timeout,
                    ssl.SSLError, ConnectionError, TimeoutError, OSError) as exc:
                if stop.is_set():
                    return
                log(f"stream dropped ({exc}); reconnecting in {backoff:.0f}s")
                stop.wait(backoff)
                backoff = min(backoff * 2, 30)
