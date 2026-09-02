import time

import httpx

from union_protocol import signing
from union_protocol.keys import b64d, b64e

from .refclient import RefNode


def _node(server, name, mode="execute", listen=True) -> RefNode:
    n = RefNode(server, name, mode=mode)
    return n


def test_web_pages_and_docs(server, web):
    assert web.get("/unions").status_code == 200
    assert web.get("/account").status_code == 200
    spec = httpx.get(f"{server}/api/openapi.json").json()
    assert "/api/v1/messages" in spec["paths"]
    assert "EventCatalog" in spec["components"]["schemas"]
    assert "UnionSignature" in spec["components"]["securitySchemes"]
    assert httpx.get(f"{server}/api/guide").status_code == 200
    assert "canonical request string" in httpx.get(f"{server}/api/guide.md").text
    assert httpx.get(f"{server}/api/docs").status_code == 200
    # Anonymous browser is bounced to login.
    r = httpx.get(f"{server}/unions", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_registration_code_and_hammering(server):
    from .conftest import Web
    w = Web(server)
    assert 'name="code"' in w.get("/register").text
    # Wrong code is rejected and re-renders the form.
    r = w.form("/register", username="mallory", password="correct horse", confirm="correct horse", code="nope")
    assert r.status_code == 200 and "Invalid registration code" in r.text
    # Missing code counts as wrong too.
    r = w.form("/register", username="mallory", password="correct horse", confirm="correct horse")
    assert r.status_code == 200 and "Invalid registration code" in r.text
    r = w.form("/register", username="mallory2", password="correct horse", confirm="correct horse", code="x")
    assert r.status_code == 200
    # Fourth bad code in the window: cut off, even with the right code now.
    r = w.form("/register", username="mallory3", password="correct horse", confirm="correct horse", code="OPEN-SESAME")
    assert r.status_code == 429 and r.json()["error"] == "rate_limited"
    # Login hammering: the default limit is 10 per 5 minutes.
    # Earlier fixtures already spent a hit or two from this IP, so look for the cut-off.
    w2 = Web(server)
    w2.get("/login")
    codes = [w2.form("/login", username="rob", password="wrong").status_code for _ in range(11)]
    assert codes[0] == 200 and 429 in codes and all(c == 429 for c in codes[codes.index(429):])


def test_join_roster_and_signature_rules(server, web, union):
    a = _node(server, "Desk-Union")
    try:
        assert a.join("UNJ-WRONGWRONGWRONGWRONG").status_code == 403
        r = a.join(union["join_key"], cwd="C:/code/union")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["union_name"] == "Home" and body["fingerprint"] == a.keys.fingerprint
        assert [m["name"] for m in body["roster"]["roster"]["members"]] == ["Desk-Union"]

        # Same key pair cannot join twice; same name cannot be reused by another key.
        assert a.join(union["join_key"]).status_code == 409
        dup = RefNode(server, "Desk-Union")
        assert dup.join(union["join_key"]).status_code == 409
        dup.close()

        # Web page sees the node, offline.
        state = web.api(f"/unions/{union['id']}/api/state").json()
        assert state["nodes"][0]["name"] == "Desk-Union" and state["nodes"][0]["status"] == "offline"
        assert state["nodes"][0]["fingerprint"] == a.keys.fingerprint

        # Replay of an identical signed request is rejected; unsigned is rejected.
        path = "/api/v1/agents"
        headers = signing.sign_request(a.keys.signing, a.keys.node_id, "GET", path, b"")
        assert a.client.get(path, headers=headers).status_code == 200
        assert a.client.get(path, headers=headers).status_code == 401
        assert a.client.get(path).status_code == 401
        # Signed with the wrong path fails.
        bad = signing.sign_request(a.keys.signing, a.keys.node_id, "GET", "/api/v1/roster", b"")
        assert a.client.get(path, headers=bad).status_code == 401

        # Cycling the key: old key dead, member unaffected.
        r = web.api(f"/unions/{union['id']}/api/cycle-key", {})
        new_key = r.json()["join_key"]
        assert new_key != union["join_key"]
        late = RefNode(server, "Late")
        assert late.join(union["join_key"]).status_code == 403
        assert late.join(new_key).status_code == 200
        late.close()
        assert a.agents() == []
    finally:
        a.close()


def test_send_receive_reply_and_presence(server, web, union):
    a, b = _node(server, "A"), _node(server, "B")
    try:
        assert a.join(union["join_key"]).status_code == 200
        a.listen()
        # B learns about A from its join response; A learns about B via the roster event pushed on join.
        assert b.join(union["join_key"]).status_code == 200
        assert [m["name"] for m in a.wait_event("roster")["roster"]["members"]] == ["A", "B"]
        b.listen()
        assert a.wait_event("agent_update")["name"] == "B"

        agents = a.agents()
        assert [x["name"] for x in agents] == ["B"] and agents[0]["status"] == "idle"
        b.presence(status="busy")
        assert a.wait_event("agent_update")["status"] == "busy"

        mid, results = a.send(["B"], "Just a friendly hello. Nothing to do.")
        assert results == [{"to": "B", "status": "sent", "detail": None, "last_seen_at": None}]
        ev = b.wait_event("message")
        assert ev["id"] == mid and ev["from_name"] == "A" and ev["kind"] == "data"
        b.ack(mid)
        assert b.open(ev)["text"] == "Just a friendly hello. Nothing to do."

        rid, results = b.send(["A"], "Received, hello back. No work to do.", kind="reply", reply_to=mid)
        assert results[0]["status"] == "sent"
        rev = a.wait_event("message")
        assert rev["reply_to"] == mid and a.open(rev)["text"].startswith("Received")
        a.ack(rid)

        # Web page shows both online with counters.
        state = web.api(f"/unions/{union['id']}/api/state").json()
        by = {n["name"]: n for n in state["nodes"]}
        assert by["A"]["status"] == "idle" and by["B"]["status"] == "busy"
        assert by["A"]["messages_sent"] == 1 and by["B"]["messages_recv"] == 1
    finally:
        a.close(); b.close()


def test_offline_mode_refusal_and_undelivered(server, union):
    a, b, c = _node(server, "A"), _node(server, "B", mode="data"), _node(server, "C")
    try:
        for n in (a, b, c):
            assert n.join(union["join_key"]).status_code == 200
        # Sending before opening the stream is refused.
        try:
            a.send(["B"], "x")
            assert False, "expected not_online"
        except RuntimeError as exc:
            assert "not_online" in str(exc)
        a.listen(); b.listen()
        a.wait_event("agent_update")  # B online
        # Refresh rosters so A has everyone's keys.
        a._accept_roster(a.req("GET", "/api/v1/roster").json())

        _, results = a.send(["B", "C", "Nobody", "A"], "hi")
        by = {r["to"]: r for r in results}
        assert by["B"]["status"] == "sent"
        assert by["C"]["status"] == "offline" and by["C"]["last_seen_at"]
        assert by["Nobody"]["status"] == "not_member"
        assert by["A"]["status"] == "self"
        b.ack(b.wait_event("message")["id"])

        _, results = a.send(["B"], "do work", kind="task")
        assert results[0]["status"] == "refused_mode"

        # Unacked message -> undelivered report to the sender after the window (1s in tests).
        mid, results = a.send(["B"], "no ack coming")
        assert results[0]["status"] == "sent"
        b.wait_event("message")
        und = a.wait_event("undelivered", timeout=5)
        assert und == {"id": mid, "recipients": ["B"]}
    finally:
        a.close(); b.close(); c.close()


def test_key_rotation(server, web, union):
    a, b = _node(server, "A"), _node(server, "B")
    try:
        a.join(union["join_key"]); b.join(union["join_key"])
        a.listen(); b.listen()
        old_pub = b64e(a.keys.signing_pub)
        r = a.rotate()
        assert r.status_code == 200, r.text
        me = next(m for m in r.json()["roster"]["members"] if m["name"] == "A")
        assert me["key_rotations"] == 1 and me["prev_signing_pub"] == old_pub
        assert me["signing_pub"] == b64e(a.keys.signing_pub) and me["node_id"] == a.node_id
        # B is told, and can verify continuity with the key it pinned.
        pushed = b.wait_event("roster")
        entry = next(m for m in pushed["roster"]["members"] if m["name"] == "A")
        statement = {"node_id": entry["node_id"], "signing_pub": entry["signing_pub"], "kx_pub": entry["kx_pub"]}
        signing.verify_json(b64d(old_pub), statement, entry["rotation_sig"])
        # New key works, old key is dead, and messaging still flows both ways.
        assert a.agents()[0]["name"] == "B"
        mid, res = b.send(["A"], "after rotation")
        assert res[0]["status"] == "sent"
        ev = a.wait_event("message"); a.ack(mid)
        assert a.open(ev)["text"] == "after rotation"
        mid2, res = a.send(["B"], "and back")
        assert res[0]["status"] == "sent"
        ev2 = b.wait_event("message"); b.ack(mid2)
        assert b.open(ev2)["text"] == "and back"
        state = web.api(f"/unions/{union['id']}/api/state").json()
        assert next(n for n in state["nodes"] if n["name"] == "A")["key_rotations"] == 1
    finally:
        a.close(); b.close()


def test_attachments(server, union):
    a, b = _node(server, "A"), _node(server, "B")
    try:
        a.join(union["join_key"]); b.join(union["join_key"])
        a.listen(); b.listen()
        blob = bytes(range(256)) * 400  # 100 KB binary
        mid, results = a.send(["B"], "see attached", files={"bin.dat": blob, "note.txt": b"hello\n"})
        assert results[0]["status"] == "sent"
        ev = b.wait_event("message")
        assert len(ev["blob_ids"]) == 2
        b.ack(mid)
        payload = b.open(ev)
        assert payload["files"]["bin.dat"] == blob and payload["files"]["note.txt"] == b"hello\n"
        # Fetched by every recipient -> gone.
        assert b.req("GET", f"/api/v1/blobs/{ev['blob_ids'][0]}").status_code == 404
        # A third party cannot fetch a blob it was not sent.
        c = _node(server, "C"); c.join(union["join_key"])
        bid = ev["blob_ids"][1]
        assert c.req("GET", f"/api/v1/blobs/{bid}").status_code == 404
        c.close()
        # Referencing an unstaged blob is rejected.
        r = a.req("POST", "/api/v1/messages", {"id": "0" * 26, "recipients": ["B"], "kind": "data", "reply_to": None,
                                               "created_at": "t", "nonce": "AA==", "ciphertext": "AA==",
                                               "wraps": [], "blob_ids": ["01J00000000000000000000000"], "sig": "AA=="})
        assert r.status_code == 400
    finally:
        a.close(); b.close()


def test_evict_and_delete(server, web, union):
    a, b = _node(server, "A"), _node(server, "B")
    try:
        a.join(union["join_key"]); b.join(union["join_key"])
        a.listen(); b.listen()
        page = web.get(f"/unions/{union['id']}")
        assert page.status_code == 200 and union["join_key"] in page.text and "Evict" in page.text
        state = web.api(f"/unions/{union['id']}/api/state").json()
        b_id = next(n["id"] for n in state["nodes"] if n["name"] == "B")
        assert web.api(f"/unions/{union['id']}/api/nodes/{b_id}/evict", {}).status_code == 200
        assert b.wait_event("evicted")["reason"] == "evicted"
        assert b.req("GET", "/api/v1/agents").status_code == 401
        assert b.req("GET", "/api/v1/agents").json()["error"] == "evicted"
        roster = a.wait_event("roster")
        assert [m["name"] for m in roster["roster"]["members"]] == ["A"]
        assert b.join(union["join_key"]).status_code == 403  # dead key pair
        assert a.agents() == []

        # Rename propagates; delete evicts the rest.
        web.api(f"/unions/{union['id']}/api/rename", {"name": "Home 2"})
        assert a.wait_event("roster")["roster"]["union_name"] == "Home 2"
        assert web.api(f"/unions/{union['id']}/api/delete", {}).status_code == 200
        assert a.wait_event("evicted")["reason"] == "union_deleted"
        assert web.get(f"/unions/{union['id']}").status_code == 404
        time.sleep(0.2)
        assert a.req("GET", "/api/v1/agents").status_code == 401
    finally:
        a.close(); b.close()
