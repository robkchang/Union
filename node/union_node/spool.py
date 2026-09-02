"""Reading the inbound spool on behalf of the harness.

Two readers can report spool lines to the model: the monitor (`union tail`,
started by the harness or armed by the model as a background Monitor) and
the prompt hook (`union unread`, run on every user prompt where no monitor
can exist). A cursor file records how far the spool has been reported so a
message reaches the session once, whichever reader gets to it first. A lock
file makes a second `union tail` for the same project exit immediately, so
arming the monitor twice is harmless.
"""
from __future__ import annotations

import os
import pathlib
import sys

CURSOR_FILE = "spool.cursor"
LOCK_FILE = "tail.lock"


def read_cursor(union_dir: pathlib.Path) -> int | None:
    """Byte offset up to which the spool has been reported, or None if unknown."""
    try:
        return int((union_dir / CURSOR_FILE).read_text("utf-8").strip() or 0)
    except (OSError, ValueError):
        return None


def write_cursor(union_dir: pathlib.Path, pos: int) -> None:
    try:
        union_dir.mkdir(parents=True, exist_ok=True)
        tmp = union_dir / (CURSOR_FILE + ".tmp")
        tmp.write_text(str(pos), "utf-8")
        os.replace(tmp, union_dir / CURSOR_FILE)
    except OSError:
        pass


def start_position(union_dir: pathlib.Path, spool: pathlib.Path) -> int:
    """Where a reader should begin: the cursor if it is valid for the current
    spool, else the end (only messages from now on)."""
    size = spool.stat().st_size if spool.exists() else 0
    cur = read_cursor(union_dir)
    if cur is None or cur > size:
        return size
    return cur


def read_new(spool: pathlib.Path, pos: int) -> tuple[list[str], int]:
    """Spool lines appended after `pos`, as the text the harness should show,
    and the new position. Handles a truncated spool by starting over."""
    import json

    if not spool.exists():
        return [], 0
    size = spool.stat().st_size
    if size < pos:
        pos = 0
    if size == pos:
        return [], pos
    with open(spool, "r", encoding="utf-8") as f:
        f.seek(pos)
        chunk = f.read()
        pos = f.tell()
    lines = []
    for raw in chunk.splitlines():
        try:
            lines.append(json.loads(raw).get("line", ""))
        except ValueError:
            continue
    return [line for line in lines if line], pos


def acquire_tail_lock(union_dir: pathlib.Path):
    """Hold the per-project tail lock for the life of the returned file
    object, or return None if another tail already holds it. The OS drops the
    lock when the holder exits, so there is no stale-lock problem."""
    try:
        union_dir.mkdir(parents=True, exist_ok=True)
        f = open(union_dir / LOCK_FILE, "a+b")
    except OSError:
        return None
    try:
        if sys.platform == "win32":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def release_tail_lock(f) -> None:
    if f is None:
        return
    try:
        if sys.platform == "win32":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    f.close()
