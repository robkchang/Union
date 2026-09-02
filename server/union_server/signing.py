"""Verify signed node requests. See union_protocol.signing for the scheme."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

from fastapi import HTTPException, Request

from union_protocol import constants
from union_protocol.keys import b64d
from union_protocol.signing import SignatureError, verify_request


@dataclass
class NodeContext:
    node: dict    # nodes row joined with presence
    union: dict   # unions row


class NonceCache:
    def __init__(self) -> None:
        self._seen: set[tuple[str, str]] = set()
        self._order: deque = deque()

    def check_and_add(self, node_id: str, nonce: str) -> bool:
        now = time.monotonic()
        while self._order and self._order[0][0] < now - constants.NONCE_RETENTION_SECONDS:
            _, key = self._order.popleft()
            self._seen.discard(key)
        key = (node_id, nonce)
        if key in self._seen:
            return False
        self._seen.add(key)
        self._order.append((now, key))
        return True


def _headers(request: Request) -> tuple[str, str, str, str]:
    h = request.headers
    vals = (h.get(constants.HEADER_NODE), h.get(constants.HEADER_TS),
            h.get(constants.HEADER_NONCE), h.get(constants.HEADER_SIG))
    if not all(vals):
        raise HTTPException(401, {"error": "unsigned", "detail": "Missing X-Union-Node/Ts/Nonce/Sig headers."})
    return vals  # type: ignore[return-value]


async def verify_headers_with_key(request: Request, signing_pub_b64: str, expected_node_id: str) -> None:
    node_id, ts, nonce, sig = _headers(request)
    if node_id != expected_node_id:
        raise HTTPException(401, {"error": "bad_signature", "detail": "X-Union-Node does not match the signing key."})
    body = await request.body()
    try:
        verify_request(b64d(signing_pub_b64), request.method, request.url.path, ts, nonce, body, sig)
    except SignatureError as exc:
        raise HTTPException(401, {"error": "bad_signature", "detail": str(exc)})
    if not request.app.state.nonces.check_and_add(node_id, nonce):
        raise HTTPException(401, {"error": "replay", "detail": "Nonce already used."})


async def signed_node(request: Request) -> NodeContext:
    """Dependency: the calling node, verified. 401 on any failure, with
    `evicted` when the node was removed from its union."""
    node_id, *_ = _headers(request)
    db = request.app.state.db
    node = db.get_node(node_id)
    if not node:
        raise HTTPException(401, {"error": "unknown_node", "detail": "This node has not joined a union on this server."})
    if node["removed_at"]:
        raise HTTPException(401, {"error": "evicted", "detail": f"This node was removed from its union ({node['removed_reason']})."})
    union = db.get_union(node["union_id"])
    if not union:
        raise HTTPException(401, {"error": "evicted", "detail": "The union no longer exists."})
    await verify_headers_with_key(request, node["signing_pub"], node_id)
    return NodeContext(node=node, union=union)
