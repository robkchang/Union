"""The node runtime: identity, roster pins, receiving, sending, and the spool
the harness monitor tails."""
from __future__ import annotations

import io
import json
import logging
import mimetypes
import pathlib
import socket
import tarfile
import threading
import time
from dataclasses import dataclass, field

from union_protocol import seal, signing
from union_protocol.keys import NodeKeys, b64d, b64e, fingerprint
from union_protocol.signing import SignatureError
from union_protocol.ulid import new_ulid

from . import config as cfgmod
from .client import Client, UnionError

log = logging.getLogger("union.node")

INLINE_TEXT_LIMIT = 8 * 1024


@dataclass
class Inbound:
    id: str
    from_name: str
    kind: str
    reply_to: str | None
    text: str
    files: list[dict]            # {name, path, mime, size}
    received_at: str
    inline: dict[str, str] = field(default_factory=dict)  # small text attachments, name -> content

    def framed(self) -> str:
        head = f"[union] message from {self.from_name} (kind: {self.kind}, id: {self.id}"
        if self.reply_to:
            head += f", replying to {self.reply_to}"
        head += ")"
        lines = [head, self.text]
        for f in self.files:
            lines.append(f"[attachment] {f['path']} ({f['mime']}, {_human(f['size'])})")
        for name, content in self.inline.items():
            lines.append(f"[attachment {name} inline]\n{content}")
        return "\n".join(lines)

    def one_line(self) -> str:
        text = " ⏎ ".join(l for l in self.text.splitlines() if l.strip())
        s = f"[union] from {self.from_name} ({self.kind}, id {self.id}"
        if self.reply_to:
            s += f", re {self.reply_to}"
        s += f"): {text}"
        for f in self.files:
            s += f" [attachment: {f['path']}]"
        return s

    def to_json(self) -> dict:
        return {"id": self.id, "from": self.from_name, "kind": self.kind, "reply_to": self.reply_to,
                "text": self.text, "files": self.files, "received_at": self.received_at, "inline": self.inline}


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


_TEXT_EXT = {
    ".md": "text/markdown", ".txt": "text/plain", ".log": "text/plain", ".csv": "text/csv",
    ".json": "application/json", ".yaml": "text/yaml", ".yml": "text/yaml", ".toml": "text/plain",
    ".py": "text/x-python", ".js": "text/javascript", ".ts": "text/typescript", ".sh": "text/x-shellscript",
    ".ps1": "text/plain", ".ini": "text/plain", ".cfg": "text/plain", ".xml": "text/xml", ".html": "text/html",
    ".css": "text/css", ".sql": "text/plain", ".tf": "text/plain", ".env": "text/plain", ".diff": "text/plain",
    ".patch": "text/plain",
}


def _guess_mime(name: str) -> str:
    """mimetypes depends on the OS registry (Windows often lacks .md), so
    prefer a small table for common source and text files."""
    ext = pathlib.Path(name).suffix.lower()
    if ext in _TEXT_EXT:
        return _TEXT_EXT[ext]
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


class Node:
    """One configured project. Create with `Node.join(...)` once, then
    `Node.load(project_dir)` on every start."""

    def __init__(self, project_dir: pathlib.Path, cfg: cfgmod.NodeConfig, keys: NodeKeys):
        self.project_dir = project_dir
        self.cfg = cfg
        self.keys = keys
        self.client = Client(cfg.url, keys, cfg.node_id)
        self.roster: dict[str, dict] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.unread: list[Inbound] = []
        self.seen_senders: dict[str, str] = {}   # message id -> sender name, for replies after reading
        self._waiters: dict[str, tuple[threading.Condition, list[Inbound]]] = {}
        self.online = False
        self.evicted_reason: str | None = None
        self.last_undelivered: list[dict] = []

    # ── lifecycle ────────────────────────────────────────────────────────

    @classmethod
    def load(cls, project_dir: pathlib.Path) -> "Node":
        cfg = cfgmod.load_config(project_dir)
        if cfg is None:
            raise FileNotFoundError(f"No Union node in {project_dir}. Run `union join` there first.")
        return cls(project_dir, cfg, cfgmod.load_keys(project_dir))

    @classmethod
    def join(cls, project_dir: pathlib.Path, url: str, name: str, join_key: str, mode: str = "data",
             harness: str = "claude-code", machine: str | None = None) -> "Node":
        if cfgmod.load_config(project_dir):
            raise RuntimeError(f"{project_dir} already has a Union node. Run `union leave` first to replace it.")
        keys = NodeKeys.generate()
        client = Client(url, keys, keys.node_id)
        hub = client.hub()
        resp = client.join(join_key.strip(), name, mode, machine or socket.gethostname(), harness, str(project_dir))
        cfg = cfgmod.NodeConfig(
            url=url.rstrip("/"), name=name, mode=mode, union_id=resp["union_id"], union_name=resp["union_name"],
            node_id=resp["node_id"], hub_signing_pub=hub["signing_pub"], hub_fingerprint=hub["fingerprint"],
            machine=machine or socket.gethostname(), harness=harness,
        )
        node = cls(project_dir, cfg, keys)
        node._accept_roster(resp["roster"])
        cfgmod.save_keys(project_dir, keys)
        cfgmod.save_config(project_dir, cfg)
        cfgmod.ensure_gitignore(project_dir)
        client.close()
        return node

    def start(self, rotate: bool = True) -> None:
        """Rotate keys (best effort), then open the event stream in a thread."""
        if rotate:
            self.rotate_keys()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_stream, name="union-stream", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(3)
        self.client.close()

    def rotate_keys(self) -> bool:
        new = NodeKeys.generate()
        try:
            roster = self.client.rotate(new)
        except Exception as exc:
            log.warning("key rotation skipped: %s", exc)
            return False
        self.keys = new
        cfgmod.save_keys(self.project_dir, new)
        self._accept_roster(roster)
        return True

    def leave(self) -> None:
        try:
            self.client.leave()
        finally:
            self.stop()

    # ── roster and pins ──────────────────────────────────────────────────

    def _accept_roster(self, signed: dict) -> None:
        try:
            signing.verify_json(b64d(self.cfg.hub_signing_pub), signed["roster"], signed["sig"])
        except SignatureError:
            log.error("roster signature does not verify; ignoring it")
            return
        members = {m["name"]: m for m in signed["roster"]["members"]}
        changed = False
        for name, m in members.items():
            if name == self.cfg.name:
                continue
            pin = self.cfg.pins.get(name)
            if pin is None:
                self.cfg.pins[name] = {"node_id": m["node_id"], "signing_pub": m["signing_pub"], "kx_pub": m["kx_pub"]}
                changed = True
            elif pin["node_id"] != m["node_id"]:
                # Same name, different membership: evicted and rejoined by someone.
                if name not in self.cfg.untrusted:
                    self.cfg.untrusted.append(name); changed = True
                    log.warning("%s rejoined with a new identity; not trusted until `union trust %s`", name, name)
            elif pin["signing_pub"] != m["signing_pub"] or pin["kx_pub"] != m["kx_pub"]:
                statement = {"node_id": m["node_id"], "signing_pub": m["signing_pub"], "kx_pub": m["kx_pub"]}
                try:
                    signing.verify_json(b64d(pin["signing_pub"]), statement, m.get("rotation_sig") or "")
                    pin.update(signing_pub=m["signing_pub"], kx_pub=m["kx_pub"]); changed = True
                except SignatureError:
                    if name not in self.cfg.untrusted:
                        self.cfg.untrusted.append(name); changed = True
                        log.warning("%s changed keys without a valid rotation proof; not trusted until `union trust %s`", name, name)
        with self._lock:
            self.roster = members
            self.cfg.union_name = signed["roster"]["union_name"]
        if changed:
            cfgmod.save_config(self.project_dir, self.cfg)

    def trust(self, name: str) -> None:
        m = self.roster.get(name)
        if not m:
            raise ValueError(f"{name} is not a member")
        self.cfg.pins[name] = {"node_id": m["node_id"], "signing_pub": m["signing_pub"], "kx_pub": m["kx_pub"]}
        if name in self.cfg.untrusted:
            self.cfg.untrusted.remove(name)
        cfgmod.save_config(self.project_dir, self.cfg)

    def _trusted_keys(self, name: str) -> dict | None:
        if name in self.cfg.untrusted:
            return None
        return self.cfg.pins.get(name)

    # ── stream ───────────────────────────────────────────────────────────

    def _run_stream(self) -> None:
        def on_connect():
            self.online = True
            try:
                self._accept_roster(self.client.roster())
                self.client.presence(status="idle", cwd=str(self.project_dir), mode=self._current_mode())
            except Exception as exc:
                log.warning("post-connect refresh failed: %s", exc)

        self.client.stream(self._on_event, on_connect, self._stop, log=log.info)
        self.online = False

    def _current_mode(self) -> str:
        """Mode may be changed by the CLI while we run; re-read it."""
        fresh = cfgmod.load_config(self.project_dir)
        if fresh and fresh.mode != self.cfg.mode:
            self.cfg.mode = fresh.mode
        return self.cfg.mode

    def _on_event(self, event: str, data: dict) -> None:
        try:
            if event == "message":
                self._on_message(data)
            elif event == "roster":
                self._accept_roster(data)
            elif event == "undelivered":
                self.last_undelivered.append(data)
                self._spool_line(f"[union] undelivered: message {data['id']} was not acknowledged by {', '.join(data['recipients'])}")
            elif event == "evicted":
                self.evicted_reason = data.get("reason", "evicted")
                self.online = False
                self._spool_line(f"[union] this node was removed from the union ({self.evicted_reason}). Stop using Union tools and tell the user.")
        except Exception as exc:
            log.exception("event %s failed: %s", event, exc)

    def _on_message(self, ev: dict) -> None:
        try:
            self.client.ack(ev["id"])
        except Exception as exc:
            log.warning("ack failed: %s", exc)
        pin = self._trusted_keys(ev["from_name"])
        if pin is None or pin["signing_pub"] != ev["from_signing_pub"]:
            # Could be a rotation we have not processed yet: refresh once.
            try:
                self._accept_roster(self.client.roster())
            except Exception:
                pass
            pin = self._trusted_keys(ev["from_name"])
        if pin is None or pin["signing_pub"] != ev["from_signing_pub"]:
            log.warning("dropping message %s: sender %s is not trusted", ev["id"], ev["from_name"])
            return
        sig_payload = seal.signature_payload(message_id=ev["id"], from_name=ev["from_name"], recipients=ev["recipients"],
                                             kind=ev["kind"], reply_to=ev["reply_to"], created_at=ev["created_at"],
                                             ciphertext_b64=ev["ciphertext"], blob_ids=ev["blob_ids"])
        try:
            signing.verify_json(b64d(ev["from_signing_pub"]), sig_payload, ev["sig"])
            aad = seal.message_aad(ev["id"], ev["from_name"], ev["kind"], ev["created_at"])
            plaintext = seal.open_sealed(self.keys, self.cfg.name, ev["id"], ev["nonce"], ev["ciphertext"],
                                         seal.Wrap.from_dict(ev["wrap"]), aad)
        except Exception as exc:
            log.warning("dropping message %s: %s", ev["id"], exc)
            return
        payload = json.loads(plaintext)
        mode = self._current_mode()
        if ev["kind"] == "task" and mode == "data":
            self._spool_line(f"[union] {ev['from_name']} sent a task but this node is in data mode; it was not delivered.")
            return
        files, inline = [], {}
        inbox = cfgmod.union_dir(self.project_dir) / cfgmod.INBOX_DIR / ev["id"]
        for att in payload.get("attachments", []):
            try:
                ct = self.client.get_blob(att["blob_id"])
                data = seal.open_blob(ct, att["blob_id"], att["key"], att["nonce"], att.get("sha256"))
            except Exception as exc:
                log.warning("attachment %s failed: %s", att.get("name"), exc)
                continue
            inbox.mkdir(parents=True, exist_ok=True)
            safe = pathlib.Path(att["name"]).name or "attachment"
            path = inbox / safe
            path.write_bytes(data)
            files.append({"name": safe, "path": str(path), "mime": att.get("mime", "application/octet-stream"), "size": len(data)})
            mime = att.get("mime", "")
            if (mime.startswith("text/") or mime == "application/json") and len(data) <= INLINE_TEXT_LIMIT:
                try:
                    inline[safe] = data.decode("utf-8")
                except UnicodeDecodeError:
                    pass
        msg = Inbound(ev["id"], ev["from_name"], ev["kind"], ev["reply_to"], payload.get("text", ""), files, _now(), inline)
        with self._lock:
            self.seen_senders[msg.id] = msg.from_name
            if len(self.seen_senders) > 500:
                for k in list(self.seen_senders)[:100]:
                    self.seen_senders.pop(k, None)
        if ev["kind"] == "task" and mode == "ask":
            msg.text = "[held for your approval: this node is in ask mode]\n" + msg.text
        # A reply someone is waiting on goes to the waiter, not the spool.
        if msg.reply_to:
            with self._lock:
                waiter = self._waiters.get(msg.reply_to)
            if waiter:
                cond, replies = waiter
                with cond:
                    replies.append(msg)
                    cond.notify_all()
                return
        with self._lock:
            self.unread.append(msg)
        self._spool(msg)

    # ── spool for the harness monitor ────────────────────────────────────

    def _spool(self, msg: Inbound) -> None:
        self._spool_write({"line": msg.one_line(), "message": msg.to_json()})

    def _spool_line(self, line: str) -> None:
        self._spool_write({"line": line})

    def _spool_write(self, rec: dict) -> None:
        path = cfgmod.union_dir(self.project_dir) / cfgmod.SPOOL_FILE
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ── inbox ────────────────────────────────────────────────────────────

    def drain_inbox(self) -> list[Inbound]:
        with self._lock:
            msgs, self.unread = self.unread, []
        return msgs

    # ── sending ──────────────────────────────────────────────────────────

    def agents(self) -> list[dict]:
        return self.client.agents()["agents"]

    def resolve(self, to: str | list[str]) -> tuple[list[str], list[dict]]:
        """Turn '*', 'mode:execute', or names into a recipient list plus
        pre-check results for names that cannot be sent to."""
        if isinstance(to, str):
            parts = [p.strip() for p in to.split(",") if p.strip()]
        else:
            parts = [p.strip() for p in to if p.strip()]
        agents = {a["name"]: a for a in self.agents()}
        names: list[str] = []
        pre: list[dict] = []
        for p in parts:
            if p == "*":
                names += [n for n in agents if n not in names]
            elif p.startswith("mode:"):
                want = p[5:]
                names += [n for n, a in agents.items() if a["mode"] == want and n not in names]
            elif p not in names:
                names.append(p)
        ok = []
        for n in names:
            if n == self.cfg.name:
                pre.append({"to": n, "status": "self", "detail": "You cannot message yourself."})
            elif n not in self.roster:
                pre.append({"to": n, "status": "not_member", "detail": "No member with this name."})
            elif self._trusted_keys(n) is None:
                pre.append({"to": n, "status": "untrusted", "detail": f"{n}'s keys changed without proof. Run `union trust {n}` if you expected that."})
            else:
                ok.append(n)
        return ok, pre

    def send(self, to: str | list[str], text: str, kind: str = "data", reply_to: str | None = None,
             files: list[str] | None = None, wait_for_replies: bool = False, timeout: float = 600) -> dict:
        if self.evicted_reason:
            raise UnionError(401, "evicted", self.evicted_reason)
        recipients, pre = self.resolve(to)
        if not recipients:
            return {"id": None, "results": pre, "replies": []}
        mid = new_ulid()
        created = _now()
        attachments, blob_ids = [], []
        for f in files or []:
            att, bid = self._stage_file(f)
            attachments.append(att); blob_ids.append(bid)
        payload = signing.canonical_json({"text": text, "attachments": attachments})
        aad = seal.message_aad(mid, self.cfg.name, kind, created)
        keys = {r: b64d(self._trusted_keys(r)["kx_pub"]) for r in recipients}
        sealed = seal.seal(payload, keys, mid, aad)
        sig_payload = seal.signature_payload(message_id=mid, from_name=self.cfg.name, recipients=recipients, kind=kind,
                                             reply_to=reply_to, created_at=created, ciphertext_b64=sealed.ciphertext,
                                             blob_ids=blob_ids)
        envelope = {
            "id": mid, "recipients": recipients, "kind": kind, "reply_to": reply_to, "created_at": created,
            "nonce": sealed.nonce, "ciphertext": sealed.ciphertext, "wraps": [w.to_dict() for w in sealed.wraps],
            "blob_ids": blob_ids, "sig": signing.sign_json(self.keys.signing, sig_payload),
        }
        cond, replies = threading.Condition(), []
        if wait_for_replies:
            with self._lock:
                self._waiters[mid] = (cond, replies)
        try:
            resp = self.client.send(envelope)
            results = pre + resp["results"]
            sent = [r["to"] for r in resp["results"] if r["status"] == "sent"]
            if wait_for_replies and sent:
                deadline = time.time() + timeout
                with cond:
                    while len(replies) < len(sent) and time.time() < deadline:
                        cond.wait(min(1.0, max(0.0, deadline - time.time())))
            return {"id": mid, "results": results, "replies": list(replies), "sent": sent}
        finally:
            if wait_for_replies:
                with self._lock:
                    self._waiters.pop(mid, None)

    def _stage_file(self, spec: str) -> tuple[dict, str]:
        p = pathlib.Path(spec)
        if not p.is_absolute():
            p = self.project_dir / p
        if not p.exists():
            raise FileNotFoundError(f"attachment not found: {spec}")
        if p.is_dir():
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                tar.add(p, arcname=p.name)
            data, name, mime = buf.getvalue(), f"{p.name}.tar.gz", "application/gzip"
        else:
            data, name = p.read_bytes(), p.name
            mime = _guess_mime(name)
        bid = new_ulid()
        sb = seal.seal_blob(data, bid)
        self.client.put_blob(bid, sb.ciphertext)
        return ({"blob_id": bid, "name": name, "mime": mime, "size": len(data), "sha256": sb.sha256,
                 "key": sb.key, "nonce": sb.nonce}, bid)

    # ── presence ─────────────────────────────────────────────────────────

    def set_status(self, status: str) -> None:
        self.client.presence(status=status)

    def set_mode(self, mode: str) -> None:
        self.cfg.mode = mode
        cfgmod.save_config(self.project_dir, self.cfg)
        try:
            self.client.presence(mode=mode)
        except UnionError as exc:
            log.warning("mode saved locally; server not updated: %s", exc)

    def info(self) -> dict:
        return {"project": str(self.project_dir), "url": self.cfg.url, "union": self.cfg.union_name,
                "name": self.cfg.name, "mode": self.cfg.mode, "node_id": self.cfg.node_id,
                "fingerprint": fingerprint(self.cfg.node_id), "hub_fingerprint": self.cfg.hub_fingerprint,
                "peers": sorted(n for n in self.cfg.pins), "untrusted": list(self.cfg.untrusted)}
