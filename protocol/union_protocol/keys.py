"""Node identity.

A node has two keys:

* an Ed25519 signing key. Its public half identifies the node. The node id is
  the SHA-256 of the raw 32-byte public key, as 64 hex characters. The
  fingerprint shown to humans is the first 16 hex characters in groups of 4.
* an X25519 key-agreement key, used only to receive encrypted messages.

Raw 32-byte keys travel in JSON as standard base64.
"""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(text: str) -> bytes:
    return base64.b64decode(text, validate=True)


def node_id_from_signing_pub(signing_pub: bytes) -> str:
    return hashlib.sha256(signing_pub).hexdigest()


def fingerprint(node_id: str) -> str:
    head = node_id[:16]
    return "-".join(head[i : i + 4] for i in range(0, 16, 4))


def load_signing_pub(raw: bytes) -> ed25519.Ed25519PublicKey:
    return ed25519.Ed25519PublicKey.from_public_bytes(raw)


def load_kx_pub(raw: bytes) -> x25519.X25519PublicKey:
    return x25519.X25519PublicKey.from_public_bytes(raw)


def _raw_private(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )


def _raw_public(key) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


@dataclass
class NodeKeys:
    """A node's private keys. Persist with `to_json`, restore with `from_json`."""

    signing: ed25519.Ed25519PrivateKey
    kx: x25519.X25519PrivateKey

    @classmethod
    def generate(cls) -> "NodeKeys":
        return cls(ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate())

    @property
    def signing_pub(self) -> bytes:
        return _raw_public(self.signing.public_key())

    @property
    def kx_pub(self) -> bytes:
        return _raw_public(self.kx.public_key())

    @property
    def node_id(self) -> str:
        return node_id_from_signing_pub(self.signing_pub)

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.node_id)

    def sign(self, data: bytes) -> bytes:
        return self.signing.sign(data)

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": 1,
                "signing_priv": b64e(_raw_private(self.signing)),
                "kx_priv": b64e(_raw_private(self.kx)),
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> "NodeKeys":
        data = json.loads(text)
        if data.get("version") != 1:
            raise ValueError("unsupported key file version")
        return cls(
            ed25519.Ed25519PrivateKey.from_private_bytes(b64d(data["signing_priv"])),
            x25519.X25519PrivateKey.from_private_bytes(b64d(data["kx_priv"])),
        )


@dataclass
class HubKeys:
    """The server's signing key. It signs rosters so a node can trust the
    member list it received even if a proxy in between misbehaves."""

    signing: ed25519.Ed25519PrivateKey

    @classmethod
    def generate(cls) -> "HubKeys":
        return cls(ed25519.Ed25519PrivateKey.generate())

    @property
    def signing_pub(self) -> bytes:
        return _raw_public(self.signing.public_key())

    @property
    def fingerprint(self) -> str:
        return fingerprint(node_id_from_signing_pub(self.signing_pub))

    def sign(self, data: bytes) -> bytes:
        return self.signing.sign(data)

    def to_json(self) -> str:
        return json.dumps({"version": 1, "signing_priv": b64e(_raw_private(self.signing))}, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "HubKeys":
        data = json.loads(text)
        return cls(ed25519.Ed25519PrivateKey.from_private_bytes(b64d(data["signing_priv"])))
