"""Request signing and detached JSON signatures.

Every node request to the server carries four headers:

    X-Union-Node:  <node_id>            64 hex chars, sha256 of the signing public key
    X-Union-Ts:    <unix seconds>       integer, the node's clock
    X-Union-Nonce: <random>             at least 16 bytes, base64
    X-Union-Sig:   <base64 signature>   Ed25519 over the canonical request string

The canonical request string is five lines joined by "\n":

    METHOD            upper-case, e.g. POST
    PATH              the path only, no scheme or host, no query string
    TS                the same value as X-Union-Ts
    NONCE             the same value as X-Union-Nonce
    SHA256(body)      lower-case hex of the raw request body; empty body hashes too

The server rejects a signature whose TS is more than SIG_SKEW_SECONDS from its
own clock, and any (node, nonce) pair it has seen in the last
NONCE_RETENTION_SECONDS.

JSON objects are signed over their canonical form: keys sorted, no
whitespace, UTF-8, non-ASCII kept as-is.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ed25519

from . import constants
from .keys import b64d, b64e, load_signing_pub


class SignatureError(Exception):
    """Raised when a request or JSON signature does not verify."""


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def body_hash(body: bytes | None) -> str:
    return hashlib.sha256(body or b"").hexdigest()


def request_string(method: str, path: str, ts: str, nonce: str, body: bytes | None) -> bytes:
    return "\n".join([method.upper(), path, str(ts), nonce, body_hash(body)]).encode("utf-8")


def new_nonce() -> str:
    return base64.b64encode(os.urandom(18)).decode("ascii")


def sign_request(
    signing_key: ed25519.Ed25519PrivateKey,
    node_id: str,
    method: str,
    path: str,
    body: bytes | None = None,
    *,
    ts: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    """Return the four headers for a signed request."""
    ts_value = str(int(time.time()) if ts is None else ts)
    nonce_value = nonce or new_nonce()
    sig = signing_key.sign(request_string(method, path, ts_value, nonce_value, body))
    return {
        constants.HEADER_NODE: node_id,
        constants.HEADER_TS: ts_value,
        constants.HEADER_NONCE: nonce_value,
        constants.HEADER_SIG: b64e(sig),
    }


def verify_request(
    signing_pub: bytes,
    method: str,
    path: str,
    ts: str,
    nonce: str,
    body: bytes | None,
    sig_b64: str,
    *,
    now: float | None = None,
) -> None:
    """Verify a signed request. Raises SignatureError on any failure."""
    try:
        ts_int = int(ts)
    except (TypeError, ValueError) as exc:
        raise SignatureError("bad timestamp") from exc
    current = time.time() if now is None else now
    if abs(current - ts_int) > constants.SIG_SKEW_SECONDS:
        raise SignatureError("timestamp outside allowed skew")
    if not nonce or len(nonce) < 16:
        raise SignatureError("nonce too short")
    try:
        sig = b64d(sig_b64)
        load_signing_pub(signing_pub).verify(sig, request_string(method, path, ts, nonce, body))
    except (InvalidSignature, ValueError) as exc:
        raise SignatureError("signature does not verify") from exc


def sign_json(signing_key: ed25519.Ed25519PrivateKey, obj: Any) -> str:
    return b64e(signing_key.sign(canonical_json(obj)))


def verify_json(signing_pub: bytes, obj: Any, sig_b64: str) -> None:
    try:
        load_signing_pub(signing_pub).verify(b64d(sig_b64), canonical_json(obj))
    except (InvalidSignature, ValueError) as exc:
        raise SignatureError("signature does not verify") from exc
