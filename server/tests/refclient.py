"""A small reference node built only from union_protocol and httpx. Used by
the server tests; it is also the seed of the real node package."""
from __future__ import annotations

import hashlib
import json
import queue
import threading
import time

import httpx

from union_protocol import seal, signing
from union_protocol.keys import NodeKeys, b64d, b64e
from union_protocol.ulid import new_ulid


class RefNode:
    def __init__(self, base_url: str, name: str, mode: str = "execute", harness: str = "test", machine: str = "testbox"):
        self.base_url = base_url.rstrip("/")
        self.name = name
        self.mode = mode
        self.harness = harness
        self.machine = machine
        self.keys = NodeKeys.generate()
        self.node_id = self.keys.node_id  # fixed at join; survives key rotation
        self.client = httpx.Client(base_url=self.base_url, timeout=10)
        self.roster: dict[str, dict] = {}
        self.hub_pub: bytes | None = None
        self.events: "queue.Queue[tuple[str, dict]]" = queue.Queue()
        self._stream_thread: threading.Thread | None = None
        self._stream_resp = None
        self._stream_client: httpx.Client | None = None

    # ── transport ────────────────────────────────────────────────────────

    def _headers(self, method: str, path: str, body: bytes) -> dict[str, str]:
        return signing.sign_request(self.keys.signing, self.node_id, method, path, body)

    def req(self, method: str, path: str, json_body: dict | None = None, content: bytes | None = None,
            **kw) -> httpx.Response:
        body = content if content is not None else (
            json.dumps(json_body, separators=(",", ":")).encode() if json_body is not None else b"")
        headers = self._headers(method, path, body)
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        elif content is not None:
            headers["Content-Type"] = "application/octet-stream"
        return self.client.request(method, path, content=body, headers=headers, **kw)

    # ── union ────────────────────────────────────────────────────────────

    def hub(self) -> dict:
        info = self.client.get("/api/v1/hub").json()
        self.hub_pub = b64d(info["signing_pub"])
        return info

    def _accept_roster(self, signed: dict) -> None:
        assert self.hub_pub is not None
        signing.verify_json(self.hub_pub, signed["roster"], signed["sig"])
        self.roster = {m["name"]: m for m in signed["roster"]["members"]}

    def join(self, join_key: str, cwd: str | None = None) -> httpx.Response:
        if self.hub_pub is None:
            self.hub()
        r = self.req("POST", "/api/v1/join", {
            "join_key": join_key, "name": self.name, "mode": self.mode,
            "signing_pub": b64e(self.keys.signing_pub), "kx_pub": b64e(self.keys.kx_pub),
            "machine": self.machine, "harness": self.harness, "cwd": cwd,
        })
        if r.status_code == 200:
            self._accept_roster(r.json()["roster"])
        return r

    def rotate(self) -> httpx.Response:
        new = NodeKeys.generate()
        statement = {"node_id": self.keys.node_id, "signing_pub": b64e(new.signing_pub), "kx_pub": b64e(new.kx_pub)}
        body = {"signing_pub": statement["signing_pub"], "kx_pub": statement["kx_pub"],
                "proof": signing.sign_json(new.signing, statement),
                "rotation_sig": signing.sign_json(self.keys.signing, statement)}
        r = self.req("POST", "/api/v1/rotate", body)
        if r.status_code == 200:
            self.keys = new  # node_id is stable: it stays what it was at join
            self._accept_roster(r.json())
        return r

    def agents(self) -> list[dict]:
        r = self.req("GET", "/api/v1/agents")
        r.raise_for_status()
        return r.json()["agents"]

    def presence(self, **kw) -> dict:
        r = self.req("PATCH", "/api/v1/presence", kw)
        r.raise_for_status()
        return r.json()

    # ── event stream ─────────────────────────────────────────────────────

    def listen(self) -> None:
        ready = threading.Event()
        err: list[Exception] = []

        def run():
            self._stream_client = httpx.Client(base_url=self.base_url, timeout=None)
            headers = self._headers("GET", "/api/v1/events", b"")
            try:
                with self._stream_client.stream("GET", "/api/v1/events", headers=headers) as resp:
                    self._stream_resp = resp
                    if resp.status_code != 200:
                        err.append(RuntimeError(f"{resp.status_code} {resp.read()!r}"))
                        ready.set()
                        return
                    event, data = None, ""
                    for line in resp.iter_lines():
                        if line.startswith("event:"):
                            event = line[6:].strip()
                        elif line.startswith("data:"):
                            data += line[5:].strip()
                        elif line == "":
                            if event:
                                payload = json.loads(data or "{}")
                                if event == "ping":
                                    ready.set()
                                if event == "roster":
                                    self._accept_roster(payload)
                                self.events.put((event, payload))
                            event, data = None, ""
            except Exception as exc:  # closed from another thread
                err.append(exc)
                ready.set()

        self._stream_thread = threading.Thread(target=run, daemon=True)
        self._stream_thread.start()
        ready.wait(5)
        if err:
            raise err[0]
        # A client refreshes the roster whenever it (re)connects: joins that
        # happened while it was offline were never pushed to it.
        self._accept_roster(self.req("GET", "/api/v1/roster").json())

    def wait_event(self, name: str, timeout: float = 5) -> dict:
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(f"no {name} event within {timeout}s")
            ev, data = self.events.get(timeout=remaining)
            if ev == name:
                return data

    def close(self) -> None:
        if self._stream_resp is not None:
            try:
                self._stream_resp.close()
            except Exception:
                pass
        if self._stream_client is not None:
            try:
                self._stream_client.close()
            except Exception:
                pass
        if self._stream_thread:
            self._stream_thread.join(3)
        self.client.close()

    # ── messages ─────────────────────────────────────────────────────────

    def send(self, recipients: list[str], text: str, kind: str = "data", reply_to: str | None = None,
             files: dict[str, bytes] | None = None) -> tuple[str, list[dict]]:
        mid = new_ulid()
        created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        attachments, blob_ids = [], []
        for fname, data in (files or {}).items():
            bid = new_ulid()
            sb = seal.seal_blob(data, bid)
            r = self.req("PUT", f"/api/v1/blobs/{bid}", content=sb.ciphertext)
            r.raise_for_status()
            attachments.append({"blob_id": bid, "name": fname, "mime": "application/octet-stream",
                                "size": len(data), "sha256": sb.sha256, "key": sb.key, "nonce": sb.nonce})
            blob_ids.append(bid)
        payload = signing.canonical_json({"text": text, "attachments": attachments})
        aad = seal.message_aad(mid, self.name, kind, created)
        keys = {r: b64d(self.roster[r]["kx_pub"]) for r in recipients if r in self.roster}
        sealed = seal.seal(payload, keys, mid, aad)
        sig_payload = seal.signature_payload(message_id=mid, from_name=self.name, recipients=recipients, kind=kind,
                                             reply_to=reply_to, created_at=created, ciphertext_b64=sealed.ciphertext,
                                             blob_ids=blob_ids)
        env = {
            "id": mid, "recipients": recipients, "kind": kind, "reply_to": reply_to, "created_at": created,
            "nonce": sealed.nonce, "ciphertext": sealed.ciphertext, "wraps": [w.to_dict() for w in sealed.wraps],
            "blob_ids": blob_ids, "sig": signing.sign_json(self.keys.signing, sig_payload),
        }
        r = self.req("POST", "/api/v1/messages", env)
        if r.status_code != 200:
            raise RuntimeError(f"send failed: {r.status_code} {r.text}")
        return mid, r.json()["results"]

    def ack(self, message_id: str) -> None:
        self.req("POST", f"/api/v1/messages/{message_id}/ack", {"state": "delivered"}).raise_for_status()

    def open(self, ev: dict) -> dict:
        """Verify and decrypt an inbound message event. Returns the payload
        with attachments fetched and decrypted into `files`."""
        sig_payload = seal.signature_payload(message_id=ev["id"], from_name=ev["from_name"], recipients=ev["recipients"],
                                             kind=ev["kind"], reply_to=ev["reply_to"], created_at=ev["created_at"],
                                             ciphertext_b64=ev["ciphertext"], blob_ids=ev["blob_ids"])
        signing.verify_json(b64d(ev["from_signing_pub"]), sig_payload, ev["sig"])
        pinned = self.roster.get(ev["from_name"])
        assert pinned and pinned["signing_pub"] == ev["from_signing_pub"], "sender key not pinned"
        aad = seal.message_aad(ev["id"], ev["from_name"], ev["kind"], ev["created_at"])
        plaintext = seal.open_sealed(self.keys, self.name, ev["id"], ev["nonce"], ev["ciphertext"],
                                     seal.Wrap.from_dict(ev["wrap"]), aad)
        payload = json.loads(plaintext)
        payload["files"] = {}
        for att in payload.get("attachments", []):
            r = self.req("GET", f"/api/v1/blobs/{att['blob_id']}")
            r.raise_for_status()
            payload["files"][att["name"]] = seal.open_blob(r.content, att["blob_id"], att["key"], att["nonce"], att["sha256"])
        return payload
