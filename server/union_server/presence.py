"""The live part of the relay, all in memory: open event streams, messages
waiting for their delivery ack, staged attachments, and send rate limits.

Single-process by design. Run one uvicorn worker. Every method that touches
a stream queue or creates a task must be called from the event loop thread,
so routes that push events are `async def`."""
from __future__ import annotations

import asyncio
import json
import pathlib
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

from union_protocol import constants


@dataclass
class Stream:
    node_id: str
    union_id: str
    name: str
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=constants.MAX_INBOX_QUEUE))
    connected_at: float = field(default_factory=time.time)
    next_id: int = 1


@dataclass
class InFlight:
    message_id: str
    sender_id: str
    union_id: str
    pending: set[str]              # recipient node ids not yet acked
    pending_names: dict[str, str]  # node id -> name, for the undelivered report
    task: asyncio.Task | None = None


@dataclass
class Blob:
    id: str
    owner_id: str
    union_id: str
    path: pathlib.Path
    size: int
    expires_at: float
    allowed: set[str] = field(default_factory=set)
    fetched: set[str] = field(default_factory=set)


class Registry:
    def __init__(self, blob_dir: pathlib.Path, delivery_window: float, blob_window: float):
        self.streams: dict[str, Stream] = {}
        self.inflight: dict[str, InFlight] = {}
        self.blobs: dict[str, Blob] = {}
        self.sends: dict[str, deque] = defaultdict(deque)
        self.blob_dir = blob_dir
        self.delivery_window = delivery_window
        self.blob_window = blob_window
        self._sweeper: asyncio.Task | None = None

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        for p in self.blob_dir.glob("*"):
            try:
                p.unlink()
            except OSError:
                pass
        self._sweeper = asyncio.create_task(self._sweep())

    async def stop(self) -> None:
        if self._sweeper:
            self._sweeper.cancel()
        for s in list(self.streams.values()):
            self._close_stream(s)

    async def _sweep(self) -> None:
        while True:
            await asyncio.sleep(15)
            now = time.time()
            for bid, b in list(self.blobs.items()):
                if b.expires_at < now:
                    self._drop_blob(bid)

    # ── streams ──────────────────────────────────────────────────────────

    def connect(self, node_id: str, union_id: str, name: str) -> Stream | None:
        if node_id in self.streams:
            return None
        s = Stream(node_id, union_id, name)
        self.streams[node_id] = s
        return s

    def disconnect(self, node_id: str) -> None:
        s = self.streams.pop(node_id, None)
        if s:
            self._close_stream(s)

    def _close_stream(self, s: Stream) -> None:
        try:
            s.queue.put_nowait(None)  # sentinel: end the generator
        except asyncio.QueueFull:
            pass

    def is_online(self, node_id: str) -> bool:
        return node_id in self.streams

    def push(self, node_id: str, event: str, data: dict | None) -> bool:
        s = self.streams.get(node_id)
        if not s:
            return False
        try:
            s.queue.put_nowait((event, data))
            return True
        except asyncio.QueueFull:
            return False

    def broadcast(self, union_id: str, event: str, data: dict | None, exclude: str | None = None) -> None:
        for s in list(self.streams.values()):
            if s.union_id == union_id and s.node_id != exclude:
                self.push(s.node_id, event, data)

    @staticmethod
    def format_sse(event_id: int, event: str, data: dict | None) -> str:
        body = json.dumps(data, separators=(",", ":")) if data is not None else "{}"
        return f"id: {event_id}\nevent: {event}\ndata: {body}\n\n"

    # ── in-flight messages ───────────────────────────────────────────────

    def track(self, message_id: str, sender_id: str, union_id: str, recipients: dict[str, str]) -> None:
        """recipients: node id -> name."""
        inf = InFlight(message_id, sender_id, union_id, set(recipients), dict(recipients))
        inf.task = asyncio.create_task(self._expire(inf))
        self.inflight[message_id] = inf

    async def _expire(self, inf: InFlight) -> None:
        await asyncio.sleep(self.delivery_window)
        cur = self.inflight.pop(inf.message_id, None)
        if cur and cur.pending:
            names = sorted(cur.pending_names[n] for n in cur.pending)
            self.push(cur.sender_id, "undelivered", {"id": cur.message_id, "recipients": names})

    def ack(self, message_id: str, recipient_id: str) -> bool:
        inf = self.inflight.get(message_id)
        if not inf or recipient_id not in inf.pending:
            return False
        inf.pending.discard(recipient_id)
        if not inf.pending:
            self.inflight.pop(message_id, None)
            if inf.task:
                inf.task.cancel()
        return True

    # ── send throttle ────────────────────────────────────────────────────

    def allow_send(self, node_id: str) -> bool:
        now = time.monotonic()
        q = self.sends[node_id]
        while q and q[0] < now - 60:
            q.popleft()
        if len(q) >= constants.MAX_SENDS_PER_MINUTE:
            return False
        q.append(now)
        return True

    # ── blobs ────────────────────────────────────────────────────────────

    def stage_blob(self, blob_id: str, owner_id: str, union_id: str, data: bytes) -> Blob:
        path = self.blob_dir / blob_id
        path.write_bytes(data)
        b = Blob(blob_id, owner_id, union_id, path, len(data), time.time() + self.blob_window)
        self.blobs[blob_id] = b
        return b

    def grant_blob(self, blob_id: str, recipient_ids: set[str]) -> None:
        b = self.blobs.get(blob_id)
        if b:
            b.allowed |= recipient_ids
            b.expires_at = time.time() + self.blob_window

    def take_blob(self, blob_id: str, node_id: str) -> bytes | None:
        b = self.blobs.get(blob_id)
        if not b or (node_id != b.owner_id and node_id not in b.allowed):
            return None
        try:
            data = b.path.read_bytes()
        except OSError:
            self.blobs.pop(blob_id, None)
            return None
        b.fetched.add(node_id)
        if b.allowed and b.allowed <= b.fetched:
            self._drop_blob(blob_id)
        return data

    def _drop_blob(self, blob_id: str) -> None:
        b = self.blobs.pop(blob_id, None)
        if b:
            try:
                b.path.unlink()
            except OSError:
                pass
