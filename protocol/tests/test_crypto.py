import json

import pytest

from union_protocol import keys, seal, signing, ulid


def test_keys_roundtrip_and_ids():
    k = keys.NodeKeys.generate()
    k2 = keys.NodeKeys.from_json(k.to_json())
    assert k2.signing_pub == k.signing_pub
    assert k2.kx_pub == k.kx_pub
    assert len(k.node_id) == 64
    assert k.fingerprint.count("-") == 3


def test_request_signature_verifies_and_rejects_tamper():
    k = keys.NodeKeys.generate()
    body = b'{"a":1}'
    headers = signing.sign_request(k.signing, k.node_id, "post", "/api/v1/x", body)
    signing.verify_request(k.signing_pub, "POST", "/api/v1/x",
                           headers["X-Union-Ts"], headers["X-Union-Nonce"], body, headers["X-Union-Sig"])
    with pytest.raises(signing.SignatureError):
        signing.verify_request(k.signing_pub, "POST", "/api/v1/x",
                               headers["X-Union-Ts"], headers["X-Union-Nonce"], b'{"a":2}', headers["X-Union-Sig"])
    with pytest.raises(signing.SignatureError):
        signing.verify_request(k.signing_pub, "POST", "/api/v1/y",
                               headers["X-Union-Ts"], headers["X-Union-Nonce"], body, headers["X-Union-Sig"])


def test_request_signature_rejects_skew():
    k = keys.NodeKeys.generate()
    headers = signing.sign_request(k.signing, k.node_id, "GET", "/p", b"", ts=1000)
    with pytest.raises(signing.SignatureError):
        signing.verify_request(k.signing_pub, "GET", "/p", headers["X-Union-Ts"],
                               headers["X-Union-Nonce"], b"", headers["X-Union-Sig"], now=2000)


def test_seal_to_two_recipients_and_open():
    alice, bob, carol = (keys.NodeKeys.generate() for _ in range(3))
    mid = ulid.new_ulid()
    aad = seal.message_aad(mid, "alice", "data", "2026-09-02T00:00:00Z")
    payload = json.dumps({"text": "hello"}).encode()
    sealed = seal.seal(payload, {"bob": bob.kx_pub, "carol": carol.kx_pub}, mid, aad)
    assert len(sealed.wraps) == 2

    for name, k in (("bob", bob), ("carol", carol)):
        wrap = next(w for w in sealed.wraps if w.to == name)
        assert seal.open_sealed(k, name, mid, sealed.nonce, sealed.ciphertext, wrap, aad) == payload

    # Wrong key, wrong name, wrong aad all fail.
    wrap_bob = next(w for w in sealed.wraps if w.to == "bob")
    with pytest.raises(seal.SealError):
        seal.open_sealed(carol, "bob", mid, sealed.nonce, sealed.ciphertext, wrap_bob, aad)
    with pytest.raises(seal.SealError):
        seal.open_sealed(bob, "carol", mid, sealed.nonce, sealed.ciphertext, wrap_bob, aad)
    with pytest.raises(seal.SealError):
        seal.open_sealed(bob, "bob", mid, sealed.nonce, sealed.ciphertext, wrap_bob, b"other")


def test_signature_payload_and_json_sig():
    alice = keys.NodeKeys.generate()
    mid = ulid.new_ulid()
    sealed = seal.seal(b"x", {"bob": keys.NodeKeys.generate().kx_pub}, mid, b"aad")
    payload = seal.signature_payload(message_id=mid, from_name="alice", recipients=["bob"], kind="data",
                                     reply_to=None, created_at="t", ciphertext_b64=sealed.ciphertext, blob_ids=[])
    sig = signing.sign_json(alice.signing, payload)
    signing.verify_json(alice.signing_pub, payload, sig)
    payload["kind"] = "task"
    with pytest.raises(signing.SignatureError):
        signing.verify_json(alice.signing_pub, payload, sig)


def test_blob_roundtrip():
    data = bytes(range(256)) * 10
    bid = ulid.new_ulid()
    sb = seal.seal_blob(data, bid)
    assert seal.open_blob(sb.ciphertext, bid, sb.key, sb.nonce, sb.sha256) == data
    with pytest.raises(seal.SealError):
        seal.open_blob(sb.ciphertext, "other", sb.key, sb.nonce)


def test_ulid_shape():
    u = ulid.new_ulid()
    assert ulid.is_ulid(u)
    assert ulid.new_ulid() != u
