"""The server's own Ed25519 key, created on first start and kept in the data
directory. It signs rosters so a node can trust the member list."""
from __future__ import annotations

import pathlib

from union_protocol.keys import HubKeys, b64e
from union_protocol.signing import sign_json


def load_or_create(data_dir: pathlib.Path) -> HubKeys:
    path = data_dir / "hub.key"
    if path.exists():
        return HubKeys.from_json(path.read_text("utf-8"))
    data_dir.mkdir(parents=True, exist_ok=True)
    keys = HubKeys.generate()
    path.write_text(keys.to_json(), "utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return keys


def signed_roster(keys: HubKeys, roster: dict) -> dict:
    return {"roster": roster, "sig": sign_json(keys.signing, roster)}


def public_b64(keys: HubKeys) -> str:
    return b64e(keys.signing_pub)
