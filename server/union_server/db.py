"""All SQL. Users and passkeys are DittoRoo's tables unchanged; the rest is
Union's control plane. There is no message table: bodies never touch disk."""
from __future__ import annotations

import pathlib
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from union_protocol.ulid import new_ulid

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    UNIQUE NOT NULL,
    password_hash TEXT    NOT NULL,
    created_at    TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS webauthn_credentials (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    credential_id BLOB    NOT NULL UNIQUE,
    public_key    BLOB    NOT NULL,
    sign_count    INTEGER NOT NULL DEFAULT 0,
    transports    TEXT    DEFAULT NULL,
    name          TEXT    NOT NULL DEFAULT 'Passkey',
    created_at    TEXT    DEFAULT (datetime('now')),
    last_used_at  TEXT    DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS unions (
    id            TEXT PRIMARY KEY,
    owner_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    join_key      TEXT NOT NULL,
    join_key_gen  INTEGER NOT NULL DEFAULT 1,
    key_cycled_at TEXT,
    created_at    TEXT DEFAULT (datetime('now')),
    deleted_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_unions_owner ON unions(owner_id);

CREATE TABLE IF NOT EXISTS nodes (
    id             TEXT PRIMARY KEY,
    union_id       TEXT NOT NULL REFERENCES unions(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    signing_pub    TEXT NOT NULL,
    kx_pub         TEXT NOT NULL,
    machine        TEXT NOT NULL,
    harness        TEXT NOT NULL,
    mode           TEXT NOT NULL,
    cwd            TEXT,
    joined_at      TEXT DEFAULT (datetime('now')),
    joined_key_gen INTEGER NOT NULL,
    removed_at     TEXT,
    removed_reason TEXT,
    key_rotations    INTEGER NOT NULL DEFAULT 0,
    prev_signing_pub TEXT,
    rotation_sig     TEXT,
    rotated_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_nodes_union ON nodes(union_id);

CREATE TABLE IF NOT EXISTS presence (
    node_id       TEXT PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
    status        TEXT NOT NULL DEFAULT 'offline',
    last_seen_at  TEXT,
    messages_sent INTEGER NOT NULL DEFAULT 0,
    messages_recv INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS activity (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    union_id  TEXT NOT NULL,
    kind      TEXT NOT NULL,
    node_id   TEXT,
    node_name TEXT,
    actor     TEXT,
    at        TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_activity_union ON activity(union_id, id DESC);
"""

_KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no I, O, 0, 1


def new_join_key() -> str:
    return "UNJ-" + "".join(secrets.choice(_KEY_ALPHABET) for _ in range(20))


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Database:
    def __init__(self, path: pathlib.Path):
        self.path = path

    @contextmanager
    def conn(self):
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA synchronous = NORMAL")
        try:
            yield con
            con.commit()
        finally:
            con.close()

    _MIGRATIONS = [
        "ALTER TABLE nodes ADD COLUMN key_rotations INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE nodes ADD COLUMN prev_signing_pub TEXT",
        "ALTER TABLE nodes ADD COLUMN rotation_sig TEXT",
        "ALTER TABLE nodes ADD COLUMN rotated_at TEXT",
    ]

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as con:
            con.executescript(SCHEMA)
            cols = {r["name"] for r in con.execute("PRAGMA table_info(nodes)").fetchall()}
            for stmt in self._MIGRATIONS:
                col = stmt.split("ADD COLUMN ")[1].split()[0]
                if col not in cols:
                    con.execute(stmt)

    # ── Users (DittoRoo) ─────────────────────────────────────────────────

    def create_user(self, username: str, password_hash: str) -> int:
        with self.conn() as con:
            return con.execute("INSERT INTO users(username, password_hash) VALUES (?,?)",
                               (username, password_hash)).lastrowid

    def get_user_by_username(self, username: str) -> dict | None:
        with self.conn() as con:
            row = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        return dict(row) if row else None

    def get_user_by_id(self, user_id: int) -> dict | None:
        with self.conn() as con:
            row = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def user_count(self) -> int:
        with self.conn() as con:
            return con.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    # ── Passkeys (DittoRoo) ──────────────────────────────────────────────

    def get_passkeys(self, user_id: int) -> list[dict]:
        with self.conn() as con:
            rows = con.execute("SELECT * FROM webauthn_credentials WHERE user_id=? ORDER BY created_at",
                               (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def get_passkey_by_credential_id(self, credential_id: bytes) -> dict | None:
        with self.conn() as con:
            row = con.execute("SELECT * FROM webauthn_credentials WHERE credential_id=?",
                              (credential_id,)).fetchone()
        return dict(row) if row else None

    def save_passkey(self, user_id: int, credential_id: bytes, public_key: bytes,
                     sign_count: int, transports: str, name: str) -> int:
        with self.conn() as con:
            return con.execute(
                """INSERT INTO webauthn_credentials
                   (user_id, credential_id, public_key, sign_count, transports, name)
                   VALUES (?,?,?,?,?,?)""",
                (user_id, credential_id, public_key, sign_count, transports, name)).lastrowid

    def update_passkey_sign_count(self, credential_id: bytes, sign_count: int) -> None:
        with self.conn() as con:
            con.execute("""UPDATE webauthn_credentials SET sign_count=?, last_used_at=datetime('now')
                           WHERE credential_id=?""", (sign_count, credential_id))

    def delete_passkey(self, pk_id: int, user_id: int) -> None:
        with self.conn() as con:
            con.execute("DELETE FROM webauthn_credentials WHERE id=? AND user_id=?", (pk_id, user_id))

    # ── Unions ───────────────────────────────────────────────────────────

    def create_union(self, owner_id: int, name: str) -> dict:
        uid = new_ulid()
        with self.conn() as con:
            con.execute("INSERT INTO unions(id, owner_id, name, join_key) VALUES (?,?,?,?)",
                        (uid, owner_id, name, new_join_key()))
            con.execute("INSERT INTO activity(union_id, kind, actor) VALUES (?,?,?)",
                        (uid, "created", str(owner_id)))
        return self.get_union(uid)

    def get_union(self, union_id: str) -> dict | None:
        with self.conn() as con:
            row = con.execute("SELECT * FROM unions WHERE id=? AND deleted_at IS NULL", (union_id,)).fetchone()
        return dict(row) if row else None

    def list_unions(self, owner_id: int) -> list[dict]:
        with self.conn() as con:
            rows = con.execute(
                """SELECT u.*,
                          (SELECT COUNT(*) FROM nodes n WHERE n.union_id=u.id AND n.removed_at IS NULL) AS node_count,
                          (SELECT COUNT(*) FROM nodes n JOIN presence p ON p.node_id=n.id
                             WHERE n.union_id=u.id AND n.removed_at IS NULL AND p.status!='offline') AS online_count
                   FROM unions u WHERE owner_id=? AND deleted_at IS NULL ORDER BY created_at""",
                (owner_id,)).fetchall()
        return [dict(r) for r in rows]

    def find_union_by_join_key(self, key: str) -> dict | None:
        with self.conn() as con:
            rows = con.execute("SELECT * FROM unions WHERE deleted_at IS NULL").fetchall()
        for r in rows:
            if secrets.compare_digest(r["join_key"], key):
                return dict(r)
        return None

    def rename_union(self, union_id: str, name: str, actor: str) -> None:
        with self.conn() as con:
            con.execute("UPDATE unions SET name=? WHERE id=?", (name, union_id))
            con.execute("INSERT INTO activity(union_id, kind, actor) VALUES (?,?,?)", (union_id, "renamed", actor))

    def cycle_join_key(self, union_id: str, actor: str) -> str:
        key = new_join_key()
        with self.conn() as con:
            con.execute("""UPDATE unions SET join_key=?, join_key_gen=join_key_gen+1, key_cycled_at=datetime('now')
                           WHERE id=?""", (key, union_id))
            con.execute("INSERT INTO activity(union_id, kind, actor) VALUES (?,?,?)", (union_id, "key_cycled", actor))
        return key

    def delete_union(self, union_id: str, actor: str) -> None:
        with self.conn() as con:
            con.execute("UPDATE unions SET deleted_at=datetime('now') WHERE id=?", (union_id,))
            con.execute("""UPDATE nodes SET removed_at=datetime('now'), removed_reason='union_deleted'
                           WHERE union_id=? AND removed_at IS NULL""", (union_id,))
            con.execute("INSERT INTO activity(union_id, kind, actor) VALUES (?,?,?)", (union_id, "deleted", actor))

    # ── Nodes ────────────────────────────────────────────────────────────

    def add_node(self, node_id: str, union_id: str, name: str, signing_pub: str, kx_pub: str,
                 machine: str, harness: str, mode: str, cwd: str | None, key_gen: int) -> dict:
        with self.conn() as con:
            con.execute(
                """INSERT INTO nodes(id, union_id, name, signing_pub, kx_pub, machine, harness, mode, cwd, joined_key_gen)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (node_id, union_id, name, signing_pub, kx_pub, machine, harness, mode, cwd, key_gen))
            con.execute("INSERT OR REPLACE INTO presence(node_id, status, last_seen_at) VALUES (?,?,?)",
                        (node_id, "offline", utcnow()))
            con.execute("INSERT INTO activity(union_id, kind, node_id, node_name) VALUES (?,?,?,?)",
                        (union_id, "joined", node_id, name))
        return self.get_node(node_id)

    def get_node(self, node_id: str) -> dict | None:
        """Returns the node row joined with presence, removed or not."""
        with self.conn() as con:
            row = con.execute(
                """SELECT n.*, p.status, p.last_seen_at, p.messages_sent, p.messages_recv
                   FROM nodes n LEFT JOIN presence p ON p.node_id=n.id WHERE n.id=?""", (node_id,)).fetchone()
        return dict(row) if row else None

    def get_node_by_name(self, union_id: str, name: str) -> dict | None:
        with self.conn() as con:
            row = con.execute(
                """SELECT n.*, p.status, p.last_seen_at FROM nodes n LEFT JOIN presence p ON p.node_id=n.id
                   WHERE n.union_id=? AND n.name=? AND n.removed_at IS NULL""", (union_id, name)).fetchone()
        return dict(row) if row else None

    def list_nodes(self, union_id: str, include_removed: bool = False) -> list[dict]:
        cond = "" if include_removed else "AND n.removed_at IS NULL"
        with self.conn() as con:
            rows = con.execute(
                f"""SELECT n.*, p.status, p.last_seen_at, p.messages_sent, p.messages_recv
                    FROM nodes n LEFT JOIN presence p ON p.node_id=n.id
                    WHERE n.union_id=? {cond} ORDER BY n.joined_at""", (union_id,)).fetchall()
        return [dict(r) for r in rows]

    def remove_node(self, node_id: str, reason: str, actor: str | None = None) -> None:
        node = self.get_node(node_id)
        if not node or node["removed_at"]:
            return
        with self.conn() as con:
            con.execute("UPDATE nodes SET removed_at=datetime('now'), removed_reason=? WHERE id=?", (reason, node_id))
            con.execute("UPDATE presence SET status='offline' WHERE node_id=?", (node_id,))
            con.execute("INSERT INTO activity(union_id, kind, node_id, node_name, actor) VALUES (?,?,?,?,?)",
                        (node["union_id"], "evicted" if reason == "evicted" else "left", node_id, node["name"], actor))

    def rotate_node_keys(self, node_id: str, signing_pub: str, kx_pub: str, rotation_sig: str) -> None:
        with self.conn() as con:
            con.execute(
                """UPDATE nodes SET prev_signing_pub=signing_pub, signing_pub=?, kx_pub=?, rotation_sig=?,
                   key_rotations=key_rotations+1, rotated_at=datetime('now') WHERE id=?""",
                (signing_pub, kx_pub, rotation_sig, node_id))

    def update_node(self, node_id: str, *, mode: str | None = None, cwd: str | None = None) -> None:
        sets, args = [], []
        if mode is not None:
            sets.append("mode=?"); args.append(mode)
        if cwd is not None:
            sets.append("cwd=?"); args.append(cwd)
        if not sets:
            return
        args.append(node_id)
        with self.conn() as con:
            con.execute(f"UPDATE nodes SET {', '.join(sets)} WHERE id=?", args)

    # ── Presence ─────────────────────────────────────────────────────────

    def set_presence(self, node_id: str, status: str | None = None, seen: bool = True) -> None:
        sets, args = [], []
        if status is not None:
            sets.append("status=?"); args.append(status)
        if seen:
            sets.append("last_seen_at=?"); args.append(utcnow())
        if not sets:
            return
        args.append(node_id)
        with self.conn() as con:
            con.execute(f"UPDATE presence SET {', '.join(sets)} WHERE node_id=?", args)

    def bump_counter(self, node_id: str, column: str, by: int = 1) -> None:
        assert column in ("messages_sent", "messages_recv")
        with self.conn() as con:
            con.execute(f"UPDATE presence SET {column}={column}+? WHERE node_id=?", (by, node_id))

    def mark_all_offline(self) -> None:
        """On startup: no stream can be open yet."""
        with self.conn() as con:
            con.execute("UPDATE presence SET status='offline'")

    # ── Activity ─────────────────────────────────────────────────────────

    def list_activity(self, union_id: str, limit: int = 50) -> list[dict]:
        with self.conn() as con:
            rows = con.execute("SELECT * FROM activity WHERE union_id=? ORDER BY id DESC LIMIT ?",
                               (union_id, limit)).fetchall()
        return [dict(r) for r in rows]
