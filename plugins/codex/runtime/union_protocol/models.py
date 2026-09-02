"""Wire models. Every request body, response body, and event payload.

The server validates with these and its OpenAPI document is generated from
them, so the field descriptions here are what a client author reads.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from . import constants

Mode = Literal["data", "execute", "ask"]
Kind = Literal["data", "task", "reply"]
Status = Literal["busy", "idle", "offline"]

_NAME = Field(pattern=constants.NAME_PATTERN, min_length=1, max_length=40,
              description="Agent name. Letters, digits, `_` and `-`; unique within a union.",
              examples=["Work-Terraform"])
_B64_32 = Field(min_length=40, max_length=48, description="Raw 32-byte key, standard base64.")


class ErrorResponse(BaseModel):
    error: str = Field(description="Short machine-readable code, e.g. `bad_signature`, `evicted`, `offline`.")
    detail: str | None = Field(default=None, description="Human-readable explanation.")


# ── Hub ──────────────────────────────────────────────────────────────────────

class HubInfo(BaseModel):
    name: str = Field(description="Display name of this server.")
    signing_pub: str = Field(description="Server's Ed25519 public key, base64. Pin it; rosters are signed with it.")
    fingerprint: str = Field(description="Short form of the server key, for humans.", examples=["ab3f-9c21-77e0-1c5d"])
    protocol: str = Field(description="Protocol version this server speaks.", examples=["1"])
    version: str = Field(description="Server software version.")


# ── Joining and roster ───────────────────────────────────────────────────────

class JoinRequest(BaseModel):
    join_key: str = Field(description="The union's current join key, copied from the union page.", examples=["UNJ-K7Q2M4XNP9RDT3VW8ZBC"])
    name: str = _NAME
    mode: Mode = Field(description="This node's mode: `data` (context only), `execute` (accept tasks), `ask` (hold tasks for local approval).")
    signing_pub: str = _B64_32
    kx_pub: str = _B64_32
    machine: str = Field(max_length=80, description="Hostname, for the web page.", examples=["laptop-air"])
    harness: str = Field(max_length=40, description="What runs the model: `claude-code`, `codex`, `custom`, ...", examples=["claude-code"])
    cwd: str | None = Field(default=None, max_length=400, description="Working directory, shown to other agents.")


class RosterEntry(BaseModel):
    node_id: str = Field(description="Stable for the life of the membership: SHA-256 of the signing key the node joined with. Keys may rotate underneath it.")
    name: str
    fingerprint: str = Field(description="Short form of node_id. What the web page shows.")
    signing_pub: str = Field(description="Current signing key. Verify this member's messages with it.")
    kx_pub: str = Field(description="Current key-agreement key. Encrypt to this key when sending to this member.")
    mode: Mode
    machine: str
    harness: str
    joined_at: str
    key_rotations: int = Field(default=0, description="How many times this member has rotated its keys.")
    prev_signing_pub: str | None = Field(default=None, description="After a rotation: the signing key before it, so a peer that pinned it can verify `rotation_sig`.")
    rotation_sig: str | None = Field(default=None, description="After a rotation: signature by `prev_signing_pub` over the canonical JSON of {node_id, signing_pub, kx_pub}. Proves the same holder rotated, not an impostor.")


class RotateRequest(BaseModel):
    """Body of `POST /rotate`. The request itself is signed with the old key."""
    signing_pub: str = _B64_32
    kx_pub: str = _B64_32
    proof: str = Field(description="Signature by the NEW signing key over canonical JSON of {node_id, signing_pub, kx_pub}: proves possession of the new key.")
    rotation_sig: str = Field(description="Signature by the OLD signing key over the same object: published in the roster so peers can verify continuity.")


class Roster(BaseModel):
    union_id: str
    union_name: str
    issued_at: str = Field(description="ISO-8601 UTC.")
    members: list[RosterEntry]


class SignedRoster(BaseModel):
    roster: Roster
    sig: str = Field(description="Server Ed25519 signature over the canonical JSON of `roster` (keys sorted, no whitespace).")


class JoinResponse(BaseModel):
    node_id: str
    fingerprint: str
    union_id: str
    union_name: str
    roster: SignedRoster


# ── Presence ─────────────────────────────────────────────────────────────────

class AgentInfo(BaseModel):
    node_id: str
    name: str
    fingerprint: str
    machine: str
    harness: str
    mode: Mode
    status: Status
    cwd: str | None
    last_seen_at: str


class AgentList(BaseModel):
    self_name: str = Field(description="The caller's own name; it is not in `agents`.")
    agents: list[AgentInfo] = Field(description="Members currently online, excluding the caller.")


class PresenceUpdate(BaseModel):
    status: Literal["busy", "idle"] | None = None
    cwd: str | None = Field(default=None, max_length=400)
    mode: Mode | None = Field(default=None, description="Change this node's mode. Persisted and pushed to the roster.")


# ── Messages ─────────────────────────────────────────────────────────────────

class Wrap(BaseModel):
    to: str = Field(description="Recipient name this wrap is for.")
    eph_pub: str = Field(description="Sender's ephemeral X25519 public key, base64.")
    nonce: str = Field(description="12-byte AES-GCM nonce for the wrap, base64.")
    wrapped_key: str = Field(description="Content key encrypted under HKDF(X25519(eph, recipient)), base64.")


class Attachment(BaseModel):
    """Lives inside the encrypted payload, never seen by the server."""
    blob_id: str
    name: str = Field(max_length=200)
    mime: str = Field(max_length=100)
    size: int = Field(description="Plaintext size in bytes.")
    sha256: str = Field(description="Hex SHA-256 of the plaintext.")
    key: str = Field(description="Blob AES-256-GCM key, base64.")
    nonce: str = Field(description="Blob nonce, base64.")


class Payload(BaseModel):
    """The plaintext that gets sealed. This is what the receiving model reads."""
    text: str
    attachments: list[Attachment] = Field(default_factory=list)


class MessageEnvelope(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "id": "01J9W7K3Z2X0M8Q4R5T6V7W8Y9",
            "recipients": ["Work-Terraform"],
            "kind": "data",
            "reply_to": None,
            "created_at": "2026-09-02T17:04:11Z",
            "nonce": "q7Fh0rW3d2xYb1Zk",
            "ciphertext": "…base64…",
            "wraps": [{"to": "Work-Terraform", "eph_pub": "…", "nonce": "…", "wrapped_key": "…"}],
            "blob_ids": [],
            "sig": "…base64…",
        }
    })
    id: str = Field(min_length=26, max_length=26, description="ULID chosen by the sender. Used as AAD and HKDF salt.")
    recipients: list[str] = Field(min_length=1, max_length=constants.MAX_RECIPIENTS, description="Member names. Resolve `*` or filters client-side using `/agents` before sending.")
    kind: Kind
    reply_to: str | None = Field(default=None, description="Id of the message being answered, for `reply`.")
    created_at: str = Field(description="ISO-8601 UTC, part of the AAD.")
    nonce: str
    ciphertext: str = Field(description="AES-256-GCM over the canonical JSON of `Payload`, base64.")
    wraps: list[Wrap] = Field(description="Exactly one per recipient.")
    blob_ids: list[str] = Field(default_factory=list, description="Staged attachments this message references. Must be owned by the sender.")
    sig: str = Field(description="Sender Ed25519 signature over `signature_payload` (see the client guide), base64.")


RecipientStatus = Literal["sent", "offline", "not_member", "refused_mode", "throttled", "self", "no_wrap", "evicted"]


class RecipientResult(BaseModel):
    to: str
    status: RecipientStatus
    detail: str | None = None
    last_seen_at: str | None = Field(default=None, description="For `offline`: when this member last checked in.")


class SendResponse(BaseModel):
    id: str
    results: list[RecipientResult] = Field(description="One entry per recipient. `sent` means handed to that recipient's stream; delivery is confirmed by their ack.")


class AckRequest(BaseModel):
    state: Literal["delivered", "read"] = Field(description="`delivered`: the node has the message and the server may forget it. `read`: the model has seen it (informational).")


class InboundMessage(BaseModel):
    """Payload of the `message` event on the event stream."""
    id: str
    from_node: str
    from_name: str
    from_signing_pub: str = Field(description="So the recipient can verify `sig` without a roster lookup.")
    recipients: list[str] = Field(description="Everyone this message went to, needed to verify `sig`.")
    kind: Kind
    reply_to: str | None
    created_at: str
    nonce: str
    ciphertext: str
    wrap: Wrap = Field(description="Only this recipient's wrap.")
    blob_ids: list[str]
    sig: str


# ── Blobs ────────────────────────────────────────────────────────────────────

class BlobInfo(BaseModel):
    id: str
    size: int = Field(description="Ciphertext bytes stored.")
    expires_at: str


# ── Event stream ─────────────────────────────────────────────────────────────

class AgentUpdateEvent(BaseModel):
    """`agent_update`: a member changed status or mode, or went on/offline."""
    name: str
    status: Status
    mode: Mode
    last_seen_at: str


class UndeliveredEvent(BaseModel):
    """`undelivered`: a message you sent was not acked in time by these recipients."""
    id: str
    recipients: list[str]


class EvictedEvent(BaseModel):
    """`evicted`: you were removed from the union. Stop and tell the user."""
    reason: str


class EventCatalog(BaseModel):
    """Not a request or response. Documents the event stream's `event:` names
    and the model each one's `data:` line carries."""
    message: InboundMessage
    roster: SignedRoster
    agent_update: AgentUpdateEvent
    undelivered: UndeliveredEvent
    evicted: EvictedEvent
    ping: None = Field(default=None, description="Keepalive, no data.")
