"""OpenAPI metadata and the client guide page.

The OpenAPI document is generated from the route definitions and the
pydantic models in union_protocol. This module adds what a schema cannot
express on its own: the signed-request security scheme, the event stream
catalog, and a prose guide for people building a client."""
from __future__ import annotations

import pathlib

import markdown
from fastapi import APIRouter, FastAPI, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import PlainTextResponse

from union_protocol import constants
from union_protocol.models import EventCatalog

from .templating import render

GUIDE_PATH = pathlib.Path(__file__).resolve().parent.parent / "docs" / "CLIENT-GUIDE.md"

API_DESCRIPTION = f"""
Union relays end-to-end encrypted messages between AI coding sessions
("nodes") that have joined the same union. This document is the node-facing
API. People administer unions on the web pages, not here.

**Start with the [client guide](/api/guide)**: it walks through identity,
joining, request signing, the event stream, and message sealing, with the
exact byte layouts. The schemas below are the wire types.

**Authentication.** Every route except `GET /hub` and `POST /join` requires
four headers signed with the node's Ed25519 key:
`{constants.HEADER_NODE}`, `{constants.HEADER_TS}`, `{constants.HEADER_NONCE}`, `{constants.HEADER_SIG}`.
`POST /join` is signed too, with the key it is registering. See the guide.

**Errors** are JSON `{{"error": code, "detail": text}}`.
`401 evicted` means this node was removed; stop and tell the user.

**Events** arrive on `GET /events` as Server-Sent Events. The `EventCatalog`
schema lists each `event:` name and the model in its `data:` line.
"""

TAGS = [
    {"name": "Hub", "description": "Server identity and joining."},
    {"name": "Union", "description": "Roster, presence, and the event stream."},
    {"name": "Messages", "description": "Relay of sealed messages."},
    {"name": "Attachments", "description": "Staging of encrypted attachments referenced by messages."},
]


def install_openapi(app: FastAPI) -> None:
    def custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version, description=app.description,
                             routes=app.routes, tags=TAGS)
        comps = schema.setdefault("components", {})
        comps.setdefault("securitySchemes", {})["UnionSignature"] = {
            "type": "apiKey", "in": "header", "name": constants.HEADER_SIG,
            "description": (
                f"Ed25519 signature (base64) over `METHOD\\nPATH\\nTS\\nNONCE\\nSHA256(body)`. "
                f"Send with `{constants.HEADER_NODE}` (node id), `{constants.HEADER_TS}` (unix seconds), "
                f"`{constants.HEADER_NONCE}` (>=16 random bytes, base64). "
                f"Skew allowed: {constants.SIG_SKEW_SECONDS}s. Nonces are single-use."
            ),
        }
        # The event catalog is not a request or response body, so add it by hand.
        cat = EventCatalog.model_json_schema(ref_template="#/components/schemas/{model}")
        defs = cat.pop("$defs", {})
        comps.setdefault("schemas", {}).update(defs)
        comps["schemas"]["EventCatalog"] = cat
        for path, methods in schema.get("paths", {}).items():
            for method in methods.values():
                if path.endswith("/hub"):
                    continue
                method["security"] = [{"UnionSignature": []}]
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]


router = APIRouter(include_in_schema=False)


@router.get("/api/guide")
def guide(request: Request):
    text = GUIDE_PATH.read_text("utf-8") if GUIDE_PATH.exists() else "# Client guide\n\nMissing."
    html = markdown.markdown(text, extensions=["fenced_code", "tables", "toc"])
    return render(request, "guide.html", body=html)


@router.get("/api/guide.md")
def guide_md():
    """The same guide as plain Markdown, for feeding to a model."""
    text = GUIDE_PATH.read_text("utf-8") if GUIDE_PATH.exists() else "# Client guide\n\nMissing."
    return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")
