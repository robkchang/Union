import json
import pathlib
import time

import pytest

from union_node import cli
from union_node import config as cfgmod
from union_node.mcp_server import Tools
from union_node.node import Node


def _wait(pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def two_nodes(union, tmp_path):
    a_dir, b_dir = tmp_path / "proj-a", tmp_path / "proj-b"
    a_dir.mkdir(); b_dir.mkdir()
    (a_dir / ".gitignore").write_text("node_modules/\n")
    a = Node.join(a_dir, union["url"], "Alpha", union["join_key"], mode="execute", harness="test")
    b = Node.join(b_dir, union["url"], "Beta", union["join_key"], mode="data", harness="test")
    a.start(); b.start()
    assert _wait(lambda: a.online and b.online)
    assert _wait(lambda: "Beta" in a.roster and "Alpha" in b.roster)
    yield a, b
    for n in (a, b):
        try:
            n.client.leave()   # free the name for the next test
        except Exception:
            pass
        n.stop()


def test_join_writes_identity_and_gitignore(union, tmp_path):
    d = tmp_path / "p"; d.mkdir()
    n = Node.join(d, union["url"], "Solo", union["join_key"], mode="data")
    assert (d / ".union" / "node.json").exists() and (d / ".union" / "node.key").exists()
    assert ".union/" in (d / ".gitignore").read_text()
    key_file = json.loads((d / ".union" / "node.key").read_text())
    assert key_file["scheme"] in ("dpapi", "plain")
    assert "signing_priv" not in key_file["data"] or key_file["scheme"] == "plain"
    reloaded = Node.load(d)
    assert reloaded.cfg.node_id == n.cfg.node_id and reloaded.keys.signing_pub == n.keys.signing_pub
    (d / "src").mkdir()
    assert cfgmod.find_project_dir(d / "src") == d
    with pytest.raises(RuntimeError):
        Node.join(d, union["url"], "Solo2", union["join_key"])


def test_rotation_on_start(union, tmp_path):
    d = tmp_path / "r"; d.mkdir()
    n = Node.join(d, union["url"], "Rotator", union["join_key"])
    before = n.keys.signing_pub
    n.start()  # rotates
    try:
        assert n.keys.signing_pub != before
        assert cfgmod.load_keys(d).signing_pub == n.keys.signing_pub  # persisted
        assert _wait(lambda: n.online)
        me = n.client.roster()["roster"]["members"]
        mine = next(m for m in me if m["name"] == "Rotator")
        assert mine["key_rotations"] == 1 and mine["node_id"] == n.cfg.node_id
    finally:
        n.stop()


def test_send_receive_reply_wait_and_spool(two_nodes):
    a, b = two_nodes
    r = a.send("Beta", "Just a friendly hello from another session. Nothing to do.")
    assert r["results"] == [{"to": "Beta", "status": "sent", "detail": None, "last_seen_at": None}]
    assert _wait(lambda: b.unread)
    msg = b.drain_inbox()[0]
    assert msg.from_name == "Alpha" and msg.text.startswith("Just a friendly")
    spool = (b.project_dir / ".union" / "spool.jsonl").read_text().splitlines()
    assert json.loads(spool[-1])["line"].startswith("[union] from Alpha (data, id ")

    # A asks and waits; B replies; the reply lands in A's wait, not A's spool.
    import threading
    result = {}
    def ask():
        result["r"] = a.send("Beta", "What branch are you on?", wait_for_replies=True, timeout=10)
    t = threading.Thread(target=ask); t.start()
    assert _wait(lambda: b.unread)
    q = b.drain_inbox()[0]
    tools_b = Tools(b)
    out = tools_b.reply(q.id, "main, clean tree")
    assert "sent" in out
    t.join(10)
    rep = result["r"]
    assert rep["sent"] == ["Beta"] and len(rep["replies"]) == 1 and rep["replies"][0].text == "main, clean tree"
    assert not (a.project_dir / ".union" / "spool.jsonl").exists() or \
        all("main, clean tree" not in l for l in (a.project_dir / ".union" / "spool.jsonl").read_text().splitlines())


def test_modes_fanout_and_offline(two_nodes, union, tmp_path):
    a, b = two_nodes
    # Beta is data-mode: tasks refused by the server before sending.
    r = a.send("Beta", "do the thing", kind="task")
    assert r["results"][0]["status"] == "refused_mode"
    # A third member that never comes online.
    c_dir = tmp_path / "c"; c_dir.mkdir()
    Node.join(c_dir, union["url"], "Gamma", union["join_key"])
    assert _wait(lambda: "Gamma" in a.roster)
    r = a.send("*", "hello all")
    assert {x["to"]: x["status"] for x in r["results"]} == {"Beta": "sent"}   # '*' = online only
    r = a.send("Gamma, Nobody, Alpha", "x")
    st = {x["to"]: x["status"] for x in r["results"]}
    assert st == {"Gamma": "offline", "Nobody": "not_member", "Alpha": "self"}
    assert r["sent"] == []
    r = a.send("mode:data", "context for data nodes")
    assert [x["to"] for x in r["results"]] == ["Beta"]
    assert _wait(lambda: len(b.unread) >= 2)
    b.drain_inbox()


def test_attachments_file_and_directory(two_nodes):
    a, b = two_nodes
    (a.project_dir / "spec.md").write_bytes(b"# Spec\nhello\n")
    (a.project_dir / "pkg").mkdir(); (a.project_dir / "pkg" / "x.bin").write_bytes(bytes(range(256)) * 10)
    r = a.send("Beta", "see attached", files=["spec.md", "pkg"])
    assert r["results"][0]["status"] == "sent"
    assert _wait(lambda: b.unread)
    m = b.drain_inbox()[0]
    names = {f["name"] for f in m.files}
    assert names == {"spec.md", "pkg.tar.gz"}
    assert m.inline["spec.md"] == "# Spec\nhello\n"
    p = next(f["path"] for f in m.files if f["name"] == "pkg.tar.gz")
    import tarfile
    with tarfile.open(p) as tar:
        assert "pkg/x.bin" in tar.getnames()
    assert "[attachment]" in m.framed() and "[attachment:" in m.one_line()


def test_mode_change_cli_and_status_hook(two_nodes, monkeypatch, capsys):
    a, b = two_nodes
    assert cli.main(["--project", str(b.project_dir), "mode", "execute"]) == 0
    assert "execute mode" in capsys.readouterr().out
    assert _wait(lambda: a.roster.get("Beta", {}).get("mode") == "execute")
    r = a.send("Beta", "now do it", kind="task")
    assert r["results"][0]["status"] == "sent"
    assert _wait(lambda: b.unread); b.drain_inbox()

    # The hook path: JSON on stdin with cwd, prints nothing, exits 0.
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"cwd": str(b.project_dir)})))
    assert cli.main(["status", "busy"]) == 0
    assert capsys.readouterr().out == ""
    assert _wait(lambda: any(x["name"] == "Beta" and x["status"] == "busy" for x in a.agents()))

    # info and agents commands
    assert cli.main(["--project", str(a.project_dir), "info"]) == 0
    out = capsys.readouterr().out
    assert "Alpha" in out and "fingerprint" in out
    assert cli.main(["--project", str(a.project_dir), "agents"]) == 0
    assert "Beta" in capsys.readouterr().out


def test_tools_text(two_nodes, tmp_path):
    a, b = two_nodes
    ta, tb = Tools(a), Tools(b)
    assert "Beta" in ta.list_agents() and "You are Alpha (mode: execute)" in ta.list_agents()
    out = ta.send("Beta", "ping")
    assert "Beta: sent" in out
    assert _wait(lambda: b.unread)
    assert "[union] message from Alpha" in tb.inbox()
    assert tb.inbox() == "No unread messages."
    assert "Nothing sent" in ta.send("Nobody", "x")
    assert "kind must be" in ta.send("Beta", "x", kind="reply")
    assert "mode: execute" in ta.status()
    assert "No message with id" in ta.reply("01000000000000000000000000", "x")
    assert "not joined" in Tools(None, tmp_path).list_agents().lower()


def test_evicted_from_web(two_nodes, union):
    a, b = two_nodes
    web = union["web"]
    state = web.get(f"/unions/{union['id']}/api/state").json()
    b_id = next(n["id"] for n in state["nodes"] if n["name"] == "Beta")
    assert web.post(f"/unions/{union['id']}/api/nodes/{b_id}/evict", json={}, headers={"X-CSRF-Token": union["csrf"]}).status_code == 200
    assert _wait(lambda: b.evicted_reason == "evicted")
    assert "removed from the union" in Tools(b).list_agents()
    assert _wait(lambda: "Beta" not in a.roster)
    r = a.send("Beta", "still there?")
    assert r["results"][0]["status"] == "not_member"
