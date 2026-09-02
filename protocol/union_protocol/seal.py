"""End-to-end encryption of a message to one or many recipients.

The server relays ciphertext it cannot read. The construction:

1. The sender generates a random 256-bit content key and a 12-byte nonce and
   encrypts the plaintext payload once with AES-256-GCM. The associated data
   binds the ciphertext to its routing header:

       aad = "<message_id>|<from_name>|<kind>|<created_at>"

2. For each recipient the sender generates an ephemeral X25519 keypair,
   performs X25519 with the recipient's key-agreement public key, and derives
   a wrapping key:

       wrap_key = HKDF-SHA256(shared, salt = message_id (utf-8),
                              info = "union/wrap/v1", length = 32)

   The content key is then encrypted with AES-256-GCM under wrap_key, with a
   fresh 12-byte nonce and the recipient's name as associated data. The
   result is one `Wrap` per recipient: {to, eph_pub, nonce, wrapped_key}.

3. The sender signs the message header (see `signature_payload`) with its
   Ed25519 key so each recipient can verify who sent it, independent of the
   server.

A recipient finds its own Wrap, redoes the X25519 with its private key and
the ephemeral public key, derives the same wrap_key, unwraps the content key,
and decrypts the payload.

Attachments are encrypted separately (`seal_blob`) with their own random key
and nonce, associated data = blob_id. The key travels inside the encrypted
payload, so it needs no per-recipient wrapping of its own.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import constants
from .keys import NodeKeys, b64d, b64e, load_kx_pub


class SealError(Exception):
    """Raised when a message cannot be opened."""


@dataclass
class Wrap:
    to: str
    eph_pub: str
    nonce: str
    wrapped_key: str

    def to_dict(self) -> dict:
        return {"to": self.to, "eph_pub": self.eph_pub, "nonce": self.nonce, "wrapped_key": self.wrapped_key}

    @classmethod
    def from_dict(cls, d: dict) -> "Wrap":
        return cls(d["to"], d["eph_pub"], d["nonce"], d["wrapped_key"])


@dataclass
class Sealed:
    nonce: str
    ciphertext: str
    wraps: list[Wrap]


def message_aad(message_id: str, from_name: str, kind: str, created_at: str) -> bytes:
    return f"{message_id}|{from_name}|{kind}|{created_at}".encode("utf-8")


def _wrap_key(shared: bytes, message_id: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=message_id.encode("utf-8"),
        info=constants.HKDF_WRAP_INFO,
    ).derive(shared)


def seal(plaintext: bytes, recipients: dict[str, bytes], message_id: str, aad: bytes) -> Sealed:
    """Encrypt `plaintext` once and wrap the content key for every recipient.

    `recipients` maps recipient name -> raw 32-byte X25519 public key.
    """
    content_key = os.urandom(32)
    nonce = os.urandom(12)
    ciphertext = AESGCM(content_key).encrypt(nonce, plaintext, aad)
    wraps: list[Wrap] = []
    for name, kx_pub in recipients.items():
        eph = x25519.X25519PrivateKey.generate()
        shared = eph.exchange(load_kx_pub(kx_pub))
        wk = _wrap_key(shared, message_id)
        wrap_nonce = os.urandom(12)
        wrapped = AESGCM(wk).encrypt(wrap_nonce, content_key, name.encode("utf-8"))
        eph_pub = eph.public_key().public_bytes_raw()
        wraps.append(Wrap(name, b64e(eph_pub), b64e(wrap_nonce), b64e(wrapped)))
    return Sealed(b64e(nonce), b64e(ciphertext), wraps)


def open_sealed(keys: NodeKeys, my_name: str, message_id: str, nonce_b64: str, ciphertext_b64: str,
                wrap: Wrap, aad: bytes) -> bytes:
    """Decrypt a message addressed to `my_name` using this node's keys."""
    if wrap.to != my_name:
        raise SealError("wrap is addressed to a different recipient")
    try:
        shared = keys.kx.exchange(load_kx_pub(b64d(wrap.eph_pub)))
        wk = _wrap_key(shared, message_id)
        content_key = AESGCM(wk).decrypt(b64d(wrap.nonce), b64d(wrap.wrapped_key), my_name.encode("utf-8"))
        return AESGCM(content_key).decrypt(b64d(nonce_b64), b64d(ciphertext_b64), aad)
    except Exception as exc:  # cryptography raises InvalidTag; base64 raises ValueError
        raise SealError("cannot open message") from exc


def signature_payload(*, message_id: str, from_name: str, recipients: list[str], kind: str,
                      reply_to: str | None, created_at: str, ciphertext_b64: str,
                      blob_ids: list[str]) -> dict:
    """The header fields a sender signs. Kept as a dict so both sides build it
    identically; sign with signing.sign_json, verify with signing.verify_json."""
    return {
        "id": message_id,
        "from": from_name,
        "recipients": sorted(recipients),
        "kind": kind,
        "reply_to": reply_to,
        "created_at": created_at,
        "ciphertext_sha256": hashlib.sha256(b64d(ciphertext_b64)).hexdigest(),
        "blob_ids": sorted(blob_ids),
    }


@dataclass
class SealedBlob:
    key: str
    nonce: str
    ciphertext: bytes
    sha256: str  # of the plaintext


def seal_blob(data: bytes, blob_id: str) -> SealedBlob:
    key = os.urandom(32)
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, data, blob_id.encode("utf-8"))
    return SealedBlob(b64e(key), b64e(nonce), ct, hashlib.sha256(data).hexdigest())


def open_blob(ciphertext: bytes, blob_id: str, key_b64: str, nonce_b64: str, expected_sha256: str | None = None) -> bytes:
    try:
        data = AESGCM(b64d(key_b64)).decrypt(b64d(nonce_b64), ciphertext, blob_id.encode("utf-8"))
    except Exception as exc:
        raise SealError("cannot open attachment") from exc
    if expected_sha256 and hashlib.sha256(data).hexdigest() != expected_sha256:
        raise SealError("attachment hash mismatch")
    return data
