"""The node-facing API: join, roster, presence, the event stream, message
relay, and attachment staging. Everything a client needs is here and in
union_protocol; the web pages are for humans only."""
from __future__ import annotations

import asyncio
import hashlib

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from union_protocol import __version__ as protocol_pkg_version
from union_protocol import constants
from union_protocol.keys import b64d, fingerprint, node_id_from_signing_pub
from union_protocol.models import (
    AckRequest, AgentInfo, AgentList, BlobInfo, ErrorResponse, HubInfo, InboundMessage, JoinRequest,
    JoinResponse, MessageEnvelope, PresenceUpdate, RecipientResult, RotateRequest, SendResponse, SignedRoster,
)
from union_protocol.seal import signature_payload
from union_protocol.signing import SignatureError, verify_json
from union_protocol.ulid import is_ulid

from . import hubkeys
from .db import utcnow
from .security import client_ip
from .signing import NodeContext, signed_node, verify_headers_with_key

router = APIRouter(prefix="/api/v1")

_ERR = {401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 404: {"model": ErrorResponse},
        409: {"model": ErrorResponse}, 429: {"model": ErrorResponse}}


def _err(status: int, code: str, detail: str) -> HTTPException:
    return HTTPException(status, {"error": code, "detail": detail})


# ── helpers ──────────────────────────────────────────────────────────────────

def build_roster(request: Request, union: dict) -> dict:
    db = request.app.state.db
    members = []
    for n in db.list_nodes(union["id"]):
        members.append({
            "node_id": n["id"], "name": n["name"], "fingerprint": fingerprint(n["id"]),
            "signing_pub": n["signing_pub"], "kx_pub": n["kx_pub"], "mode": n["mode"],
            "machine": n["machine"], "harness": n["harness"], "joined_at": n["joined_at"],
            "key_rotations": n.get("key_rotations") or 0,
            "prev_signing_pub": n.get("prev_signing_pub"), "rotation_sig": n.get("rotation_sig"),
        })
    roster = {"union_id": union["id"], "union_name": union["name"], "issued_at": utcnow(), "members": members}
    return hubkeys.signed_roster(request.app.state.hub_keys, roster)


def agent_info(n: dict, online: bool) -> dict:
    return {
        "node_id": n["id"], "name": n["name"], "fingerprint": fingerprint(n["id"]),
        "machine": n["machine"], "harness": n["harness"], "mode": n["mode"],
        "status": (n.get("status") or "idle") if online else "offline",
        "cwd": n.get("cwd"), "last_seen_at": n.get("last_seen_at") or n["joined_at"],
    }


def push_roster(request: Request, union: dict) -> None:
    request.app.state.registry.broadcast(union["id"], "roster", build_roster(request, union))


def push_agent_update(request: Request, n: dict, online: bool) -> None:
    info = agent_info(n, online)
    request.app.state.registry.broadcast(
        n["union_id"], "agent_update",
        {"name": info["name"], "status": info["status"], "mode": info["mode"], "last_seen_at": info["last_seen_at"]},
        exclude=n["id"],
    )


# ── hub and joining ──────────────────────────────────────────────────────────

@router.get("/hub", response_model=HubInfo, tags=["Hub"], summary="Server identity")
def hub(request: Request):
    """Unsigned. Returns the server's signing key so a node can pin it before
    joining. Rosters are signed with this key."""
    st = request.app.state
    return {
        "name": st.settings.server.name,
        "signing_pub": hubkeys.public_b64(st.hub_keys),
        "fingerprint": st.hub_keys.fingerprint,
        "protocol": constants.PROTOCOL_VERSION,
        "version": protocol_pkg_version,
    }


@router.post("/join", response_model=JoinResponse, responses=_ERR, tags=["Hub"], summary="Join a union")
async def join(request: Request, body: JoinRequest):
    """Present a join key and register this node's public keys.

    The request must be signed with the node's own signing key, exactly like
    every other request, so the server knows the caller holds the private
    half. `X-Union-Node` must be the SHA-256 of `signing_pub`.

    Errors: `403 bad_join_key`, `409 name_taken`, `409 already_joined`.
    """
    st = request.app.state
    st.limiter.check("join", client_ip(request), 30, 60)
    try:
        signing_pub = b64d(body.signing_pub)
        kx_pub = b64d(body.kx_pub)
    except Exception:
        raise _err(400, "bad_key", "signing_pub and kx_pub must be base64 of 32 raw bytes.")
    if len(signing_pub) != 32 or len(kx_pub) != 32:
        raise _err(400, "bad_key", "signing_pub and kx_pub must be 32 raw bytes.")
    node_id = node_id_from_signing_pub(signing_pub)
    await verify_headers_with_key(request, body.signing_pub, node_id)

    union = st.db.find_union_by_join_key(body.join_key.strip())
    if not union:
        raise _err(403, "bad_join_key", "No union has this join key. It may have been cycled.")
    existing = st.db.get_node(node_id)
    if existing and not existing["removed_at"]:
        raise _err(409, "already_joined", f"This key pair already belongs to union '{existing['union_id']}' as '{existing['name']}'.")
    if existing and existing["removed_at"]:
        raise _err(403, "evicted", "This key pair was removed from a union. Generate a new identity to join again.")
    if st.db.get_node_by_name(union["id"], body.name):
        raise _err(409, "name_taken", f"'{body.name}' is already a member of this union.")

    node = st.db.add_node(node_id, union["id"], body.name, body.signing_pub, body.kx_pub,
                          body.machine, body.harness, body.mode, body.cwd, union["join_key_gen"])
    push_roster(request, union)
    return {
        "node_id": node_id, "fingerprint": fingerprint(node_id),
        "union_id": union["id"], "union_name": union["name"],
        "roster": build_roster(request, union),
    }


@router.post("/leave", status_code=204, responses=_ERR, tags=["Hub"], summary="Leave the union")
async def leave(request: Request, ctx: NodeContext = Depends(signed_node)):
    """Remove this node. Its key pair cannot rejoin; generate a new identity."""
    st = request.app.state
    st.db.remove_node(ctx.node["id"], "left")
    st.registry.disconnect(ctx.node["id"])
    push_roster(request, ctx.union)
    push_agent_update(request, {**ctx.node, "status": "offline"}, online=False)
    return Response(status_code=204)


@router.post("/rotate", response_model=SignedRoster, responses=_ERR, tags=["Hub"], summary="Rotate this node's keys")
async def rotate(request: Request, body: RotateRequest, ctx: NodeContext = Depends(signed_node)):
    """Replace this node's signing and key-agreement keys. The request is
    signed with the current (old) key like any other; `proof` shows you hold
    the new key; `rotation_sig` is published in the roster so peers that
    pinned the old key can verify the same holder rotated. Your `node_id`
    does not change. Do this on every session start so a copied key file
    goes stale quickly. Sign all later requests with the new key."""
    st = request.app.state
    me = ctx.node
    try:
        new_signing = b64d(body.signing_pub)
        new_kx = b64d(body.kx_pub)
    except Exception:
        raise _err(400, "bad_key", "signing_pub and kx_pub must be base64 of 32 raw bytes.")
    if len(new_signing) != 32 or len(new_kx) != 32:
        raise _err(400, "bad_key", "signing_pub and kx_pub must be 32 raw bytes.")
    if body.signing_pub == me["signing_pub"]:
        raise _err(400, "bad_key", "The new signing key must differ from the current one.")
    statement = {"node_id": me["id"], "signing_pub": body.signing_pub, "kx_pub": body.kx_pub}
    try:
        verify_json(new_signing, statement, body.proof)
    except SignatureError:
        raise _err(400, "bad_signature", "proof does not verify with the new signing key.")
    try:
        verify_json(b64d(me["signing_pub"]), statement, body.rotation_sig)
    except SignatureError:
        raise _err(400, "bad_signature", "rotation_sig does not verify with the current signing key.")
    st.db.rotate_node_keys(me["id"], body.signing_pub, body.kx_pub, body.rotation_sig)
    push_roster(request, ctx.union)
    return build_roster(request, ctx.union)


@router.get("/roster", response_model=SignedRoster, responses=_ERR, tags=["Union"], summary="Members and keys")
def roster(request: Request, ctx: NodeContext = Depends(signed_node)):
    """All current members with their public keys, signed by the server.
    Pushed as a `roster` event whenever it changes, so polling is unnecessary."""
    return build_roster(request, ctx.union)


# ── presence ─────────────────────────────────────────────────────────────────

@router.get("/agents", response_model=AgentList, responses=_ERR, tags=["Union"], summary="Who is online")
def agents(request: Request, ctx: NodeContext = Depends(signed_node)):
    """Members currently connected to the event stream, with status and mode.
    Resolve `*` or a mode filter against this list before sending."""
    st = request.app.state
    out = []
    for n in st.db.list_nodes(ctx.union["id"]):
        if n["id"] == ctx.node["id"]:
            continue
        if st.registry.is_online(n["id"]):
            out.append(agent_info(n, True))
    return {"self_name": ctx.node["name"], "agents": out}


@router.patch("/presence", response_model=AgentInfo, responses=_ERR, tags=["Union"], summary="Update status, cwd, or mode")
async def presence(request: Request, body: PresenceUpdate, ctx: NodeContext = Depends(signed_node)):
    """Set `status` when the model starts or finishes a turn, `mode` when the
    local user changes it. Other members get an `agent_update` event."""
    st = request.app.state
    st.db.update_node(ctx.node["id"], mode=body.mode, cwd=body.cwd)
    st.db.set_presence(ctx.node["id"], status=body.status if st.registry.is_online(ctx.node["id"]) else None)
    node = st.db.get_node(ctx.node["id"])
    online = st.registry.is_online(node["id"])
    push_agent_update(request, node, online)
    if body.mode is not None:
        push_roster(request, ctx.union)
    return agent_info(node, online)


@router.get("/events", responses={200: {"content": {"text/event-stream": {}},
                                       "description": "Server-Sent Events. See `EventCatalog` in the schemas for each event's payload."},
                                  **_ERR},
            tags=["Union"], summary="Event stream (SSE)")
async def events(request: Request, ctx: NodeContext = Depends(signed_node)):
    """Open this to come online. Holds the connection and pushes events:

    * `message`: an encrypted message for you. Ack it with `POST /messages/{id}/ack`.
    * `roster`: the signed member list changed.
    * `agent_update`: a member's status or mode changed.
    * `undelivered`: recipients of a message you sent did not ack in time.
    * `evicted`: you were removed. Stop.
    * `ping`: keepalive.

    One stream per node; a second connection gets `409 already_online`.
    """
    st = request.app.state
    node = ctx.node
    stream = st.registry.connect(node["id"], node["union_id"], node["name"])
    if stream is None:
        raise _err(409, "already_online", "This node already has an open event stream.")
    st.db.set_presence(node["id"], status="idle")
    push_agent_update(request, {**node, "status": "idle"}, online=True)
    ping = st.settings.relay.ping_seconds

    async def gen():
        try:
            yield st.registry.format_sse(0, "ping", None)
            while True:
                try:
                    item = await asyncio.wait_for(stream.queue.get(), timeout=ping)
                except asyncio.TimeoutError:
                    st.db.set_presence(node["id"])
                    yield st.registry.format_sse(stream.next_id, "ping", None)
                    stream.next_id += 1
                    continue
                if item is None:
                    return
                event, data = item
                yield st.registry.format_sse(stream.next_id, event, data)
                stream.next_id += 1
                if event == "evicted":
                    return
        finally:
            if st.registry.streams.get(node["id"]) is stream:
                st.registry.streams.pop(node["id"], None)
            st.db.set_presence(node["id"], status="offline")
            push_agent_update(request, {**node, "status": "offline"}, online=False)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── messages ─────────────────────────────────────────────────────────────────

@router.post("/messages", response_model=SendResponse, responses=_ERR, tags=["Messages"], summary="Send to one or many")
async def send(request: Request, body: MessageEnvelope, ctx: NodeContext = Depends(signed_node)):
    """Relay an encrypted message to online members of your union.

    The server checks each recipient and reports per recipient. It never
    queues: a recipient that is offline gets `offline` with `last_seen_at`
    and nothing is stored. It verifies `sig` against your signing key and
    that every recipient has exactly one wrap. Recipients in `data` mode
    refuse `task`. The message is held in memory only until each recipient
    acks, then forgotten; unacked recipients are reported to you as an
    `undelivered` event after the delivery window.
    """
    st = request.app.state
    me = ctx.node
    if not st.registry.is_online(me["id"]):
        raise _err(409, "not_online", "Open the event stream before sending, so replies can reach you.")
    if len(body.ciphertext) * 3 // 4 > constants.MAX_MESSAGE_BYTES:
        raise _err(413, "too_large", f"Ciphertext exceeds {constants.MAX_MESSAGE_BYTES} bytes.")
    if body.kind == "reply" and not body.reply_to:
        raise _err(400, "bad_request", "A reply needs reply_to.")
    if body.id in st.registry.inflight:
        raise _err(409, "duplicate_id", "A message with this id is still in flight.")

    # Signature over the header fields.
    payload = signature_payload(message_id=body.id, from_name=me["name"], recipients=body.recipients,
                                kind=body.kind, reply_to=body.reply_to, created_at=body.created_at,
                                ciphertext_b64=body.ciphertext, blob_ids=body.blob_ids)
    try:
        verify_json(b64d(me["signing_pub"]), payload, body.sig)
    except SignatureError:
        raise _err(400, "bad_signature", "sig does not verify over the message header.")

    wraps = {w.to: w for w in body.wraps}
    if len(wraps) != len(body.wraps):
        raise _err(400, "bad_request", "Duplicate wrap for a recipient.")
    extra = set(wraps) - set(body.recipients)
    if extra:
        raise _err(400, "bad_request", f"Wraps for non-recipients: {sorted(extra)}")
    for bid in body.blob_ids:
        b = st.registry.blobs.get(bid)
        if not b or b.owner_id != me["id"]:
            raise _err(400, "unknown_blob", f"Blob {bid} is not staged by you (it may have expired).")

    if not st.registry.allow_send(me["id"]):
        return {"id": body.id, "results": [{"to": r, "status": "throttled",
                                            "detail": f"More than {constants.MAX_SENDS_PER_MINUTE} sends in a minute."}
                                           for r in body.recipients]}

    results: list[dict] = []
    delivered: dict[str, str] = {}
    seen: set[str] = set()
    for name in body.recipients:
        if name in seen:
            continue
        seen.add(name)
        if name == me["name"]:
            results.append({"to": name, "status": "self", "detail": "You cannot message yourself."}); continue
        target = st.db.get_node_by_name(me["union_id"], name)
        if not target:
            results.append({"to": name, "status": "not_member", "detail": "No member with this name."}); continue
        if not st.registry.is_online(target["id"]):
            results.append({"to": name, "status": "offline", "last_seen_at": target.get("last_seen_at"),
                            "detail": "Not connected. Nothing was sent or stored."}); continue
        if body.kind not in constants.MODE_ACCEPTS[target["mode"]]:
            results.append({"to": name, "status": "refused_mode",
                            "detail": f"{name} is in {target['mode']} mode and does not accept {body.kind} messages."}); continue
        if name not in wraps:
            results.append({"to": name, "status": "no_wrap", "detail": "No wrap for this recipient."}); continue
        inbound = InboundMessage(
            id=body.id, from_node=me["id"], from_name=me["name"], from_signing_pub=me["signing_pub"],
            recipients=body.recipients, kind=body.kind, reply_to=body.reply_to, created_at=body.created_at,
            nonce=body.nonce, ciphertext=body.ciphertext, wrap=wraps[name], blob_ids=body.blob_ids, sig=body.sig,
        ).model_dump()
        if not st.registry.push(target["id"], "message", inbound):
            results.append({"to": name, "status": "throttled", "detail": "Recipient's inbox queue is full."}); continue
        delivered[target["id"]] = name
        st.db.bump_counter(target["id"], "messages_recv")
        results.append({"to": name, "status": "sent"})

    if delivered:
        st.registry.track(body.id, me["id"], me["union_id"], delivered)
        for bid in body.blob_ids:
            st.registry.grant_blob(bid, set(delivered))
        st.db.bump_counter(me["id"], "messages_sent")
    return {"id": body.id, "results": results}


@router.post("/messages/{message_id}/ack", status_code=204, responses=_ERR, tags=["Messages"], summary="Acknowledge a message")
async def ack(request: Request, message_id: str, body: AckRequest, ctx: NodeContext = Depends(signed_node)):
    """`delivered`: your node holds the message; the server forgets its copy.
    Send it as soon as the event arrives, before the model reads it.
    `read` is accepted and ignored for now."""
    if body.state == "delivered":
        request.app.state.registry.ack(message_id, ctx.node["id"])
    return Response(status_code=204)


# ── blobs ────────────────────────────────────────────────────────────────────

@router.put("/blobs/{blob_id}", response_model=BlobInfo, responses=_ERR, tags=["Attachments"], summary="Stage an encrypted attachment")
async def put_blob(request: Request, blob_id: str, ctx: NodeContext = Depends(signed_node)):
    """Upload the ciphertext of one attachment as the raw request body, then
    reference `blob_id` in a message. The blob expires unreferenced after the
    blob window, and is deleted once every recipient has fetched it."""
    st = request.app.state
    if not is_ulid(blob_id):
        raise _err(400, "bad_request", "blob_id must be a ULID.")
    if blob_id in st.registry.blobs:
        raise _err(409, "duplicate_id", "Blob id already staged.")
    data = await request.body()
    if len(data) > constants.MAX_BLOB_BYTES:
        raise _err(413, "too_large", f"Blob exceeds {constants.MAX_BLOB_BYTES} bytes.")
    if not data:
        raise _err(400, "bad_request", "Empty body.")
    b = st.registry.stage_blob(blob_id, ctx.node["id"], ctx.node["union_id"], data)
    from datetime import datetime, timezone
    return {"id": b.id, "size": b.size,
            "expires_at": datetime.fromtimestamp(b.expires_at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


@router.get("/blobs/{blob_id}", responses={200: {"content": {"application/octet-stream": {}}}, **_ERR},
            tags=["Attachments"], summary="Fetch an attachment")
def get_blob(request: Request, blob_id: str, ctx: NodeContext = Depends(signed_node)):
    """Allowed for the uploader and for recipients of a message that
    referenced the blob. Returns the ciphertext; the key is in the message."""
    data = request.app.state.registry.take_blob(blob_id, ctx.node["id"])
    if data is None:
        raise _err(404, "not_found", "No such blob, or you are not a recipient of a message that carries it.")
    return Response(content=data, media_type="application/octet-stream",
                    headers={"X-Union-Sha256": hashlib.sha256(data).hexdigest()})
