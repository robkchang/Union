"""The unions pages and the browser JSON behind them. Owner only."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from union_protocol.keys import fingerprint

from .db import utcnow
from .relay import build_roster, push_agent_update
from .security import flash, require_user_json, require_user_page, verify_csrf
from .templating import render

router = APIRouter(include_in_schema=False)


def _owned_union(request: Request, union_id: str, user: dict) -> dict:
    u = request.app.state.db.get_union(union_id)
    if not u or u["owner_id"] != user["id"]:
        raise HTTPException(404, {"error": "not_found", "detail": "No such union."})
    return u


def _state(request: Request, union: dict) -> dict:
    st = request.app.state
    nodes = []
    for n in st.db.list_nodes(union["id"]):
        online = st.registry.is_online(n["id"])
        nodes.append({
            "id": n["id"], "name": n["name"], "fingerprint": fingerprint(n["id"]),
            "machine": n["machine"], "harness": n["harness"], "mode": n["mode"], "cwd": n["cwd"],
            "status": (n["status"] or "idle") if online else "offline",
            "last_seen_at": n["last_seen_at"], "joined_at": n["joined_at"],
            "joined_key_gen": n["joined_key_gen"], "key_rotations": n.get("key_rotations") or 0,
            "rotated_at": n.get("rotated_at"),
            "messages_sent": n["messages_sent"] or 0, "messages_recv": n["messages_recv"] or 0,
        })
    return {
        "union": {k: union[k] for k in ("id", "name", "join_key", "join_key_gen", "key_cycled_at", "created_at")},
        "nodes": nodes,
        "activity": st.db.list_activity(union["id"]),
        "now": utcnow(),
    }


# ── pages ────────────────────────────────────────────────────────────────────

@router.get("/unions")
def unions_page(request: Request, user: dict = Depends(require_user_page)):
    return render(request, "unions.html", unions=request.app.state.db.list_unions(user["id"]))


@router.post("/unions", dependencies=[Depends(verify_csrf)])
def create_union(request: Request, name: Annotated[str, Form()], user: dict = Depends(require_user_page)):
    name = name.strip()[:60]
    if not name:
        flash(request, "Give the union a name.", "error")
        return RedirectResponse("/unions", status_code=303)
    u = request.app.state.db.create_union(user["id"], name)
    return RedirectResponse(f"/unions/{u['id']}", status_code=303)


@router.get("/unions/{union_id}")
def union_page(request: Request, union_id: str, user: dict = Depends(require_user_page)):
    union = _owned_union(request, union_id, user)
    return render(request, "union.html", union=union, state=_state(request, union),
                  public_url=request.app.state.settings.server.public_url)


# ── browser JSON ─────────────────────────────────────────────────────────────

class RenameBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)


@router.get("/unions/{union_id}/api/state")
def state(request: Request, union_id: str, user: dict = Depends(require_user_json)):
    return _state(request, _owned_union(request, union_id, user))


@router.post("/unions/{union_id}/api/rename", dependencies=[Depends(verify_csrf)])
async def rename(request: Request, union_id: str, body: RenameBody, user: dict = Depends(require_user_json)):
    union = _owned_union(request, union_id, user)
    request.app.state.db.rename_union(union["id"], body.name.strip(), user["username"])
    union = request.app.state.db.get_union(union["id"])
    request.app.state.registry.broadcast(union["id"], "roster", build_roster(request, union))
    return {"ok": True, "name": union["name"]}


@router.post("/unions/{union_id}/api/cycle-key", dependencies=[Depends(verify_csrf)])
def cycle_key(request: Request, union_id: str, user: dict = Depends(require_user_json)):
    union = _owned_union(request, union_id, user)
    key = request.app.state.db.cycle_join_key(union["id"], user["username"])
    return {"ok": True, "join_key": key}


def _evict(request: Request, node: dict, actor: str, reason: str = "evicted") -> None:
    st = request.app.state
    st.db.remove_node(node["id"], reason, actor)
    st.registry.push(node["id"], "evicted", {"reason": reason})
    st.registry.disconnect(node["id"])


@router.post("/unions/{union_id}/api/nodes/{node_id}/evict", dependencies=[Depends(verify_csrf)])
async def evict(request: Request, union_id: str, node_id: str, user: dict = Depends(require_user_json)):
    st = request.app.state
    union = _owned_union(request, union_id, user)
    node = st.db.get_node(node_id)
    if not node or node["union_id"] != union["id"] or node["removed_at"]:
        raise HTTPException(404, {"error": "not_found", "detail": "No such node."})
    _evict(request, node, user["username"])
    st.registry.broadcast(union["id"], "roster", build_roster(request, union))
    push_agent_update(request, {**node, "status": "offline"}, online=False)
    return {"ok": True}


@router.post("/unions/{union_id}/api/delete", dependencies=[Depends(verify_csrf)])
async def delete(request: Request, union_id: str, user: dict = Depends(require_user_json)):
    st = request.app.state
    union = _owned_union(request, union_id, user)
    for node in st.db.list_nodes(union["id"]):
        _evict(request, node, user["username"], reason="union_deleted")
    st.db.delete_union(union["id"], user["username"])
    return {"ok": True}
