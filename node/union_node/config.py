"""Per-project node state under `<project>/.union/`:

    node.json    union url, name, mode, ids, pinned peer keys   (plain JSON)
    node.key     private keys, DPAPI-encrypted on Windows        (never share)
    spool.jsonl  inbound messages for the harness monitor to tail
    inbox/       decrypted attachments, one folder per message
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import sys
from dataclasses import asdict, dataclass, field

from union_protocol.keys import NodeKeys

UNION_DIR = ".union"
CONFIG_FILE = "node.json"
KEY_FILE = "node.key"
SPOOL_FILE = "spool.jsonl"
INBOX_DIR = "inbox"


@dataclass
class NodeConfig:
    url: str
    name: str
    mode: str
    union_id: str
    union_name: str
    node_id: str
    hub_signing_pub: str
    hub_fingerprint: str
    machine: str
    harness: str
    pins: dict[str, dict] = field(default_factory=dict)   # name -> {node_id, signing_pub, kx_pub}
    untrusted: list[str] = field(default_factory=list)    # names whose keys changed without proof
    version: int = 1

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, text: str) -> "NodeConfig":
        data = json.loads(text)
        data.pop("version", None)
        return cls(**data)


def union_dir(project_dir: pathlib.Path) -> pathlib.Path:
    return project_dir / UNION_DIR


def is_plugin_cache_dir(path: pathlib.Path) -> bool:
    """Plugin caches are implementation state, never a Union project."""
    parts = {part.lower() for part in path.resolve().parts}
    return ".codex" in parts and "plugins" in parts and "cache" in parts


def find_project_dir(start: pathlib.Path | None = None) -> pathlib.Path | None:
    """Walk up from `start` (default cwd) to the first directory holding `.union/node.json`."""
    p = (start or pathlib.Path.cwd()).resolve()
    if is_plugin_cache_dir(p):
        return None
    for candidate in (p, *p.parents):
        if (candidate / UNION_DIR / CONFIG_FILE).exists():
            return candidate
    return None


def load_config(project_dir: pathlib.Path) -> NodeConfig | None:
    path = union_dir(project_dir) / CONFIG_FILE
    if not path.exists():
        return None
    return NodeConfig.from_json(path.read_text("utf-8"))


def save_config(project_dir: pathlib.Path, cfg: NodeConfig) -> None:
    d = union_dir(project_dir)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (CONFIG_FILE + ".tmp")
    tmp.write_text(cfg.to_json(), "utf-8")
    os.replace(tmp, d / CONFIG_FILE)


def ensure_gitignore(project_dir: pathlib.Path) -> None:
    gi = project_dir / ".gitignore"
    line = f"{UNION_DIR}/"
    try:
        existing = gi.read_text("utf-8") if gi.exists() else ""
    except OSError:
        return
    if line in existing.splitlines() or f"/{line}" in existing.splitlines():
        return
    with open(gi, "a", encoding="utf-8") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(f"# Union node identity and inbox\n{line}\n")


# ── Key file, protected at rest ──────────────────────────────────────────────

_ENTROPY = b"union-node-key-v1"


def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    import ctypes.wintypes as wt

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob(b: bytes):
        buf = ctypes.create_string_buffer(b, len(b))
        return DATA_BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    inp, keep1 = blob(data)
    ent, keep2 = blob(_ENTROPY)
    out = DATA_BLOB()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(inp), None, ctypes.byref(ent), None, None, 0x01, ctypes.byref(out))  # UI_FORBIDDEN
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def save_keys(project_dir: pathlib.Path, keys: NodeKeys) -> str:
    """Write the private keys. Returns the protection scheme used."""
    d = union_dir(project_dir)
    d.mkdir(parents=True, exist_ok=True)
    raw = keys.to_json().encode("utf-8")
    scheme = "plain"
    if sys.platform == "win32":
        try:
            raw = _dpapi(raw, True)
            scheme = "dpapi"
        except Exception:
            scheme = "plain"
    payload = json.dumps({"scheme": scheme, "data": base64.b64encode(raw).decode("ascii")})
    path = d / KEY_FILE
    tmp = d / (KEY_FILE + ".tmp")
    tmp.write_text(payload, "utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    return scheme


def load_keys(project_dir: pathlib.Path) -> NodeKeys:
    path = union_dir(project_dir) / KEY_FILE
    payload = json.loads(path.read_text("utf-8"))
    raw = base64.b64decode(payload["data"])
    if payload.get("scheme") == "dpapi":
        if sys.platform != "win32":
            raise RuntimeError("node.key is DPAPI-protected and can only be opened on the Windows account that created it.")
        raw = _dpapi(raw, False)
    return NodeKeys.from_json(raw.decode("utf-8"))
