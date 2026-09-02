"""Login, register, logout, account page, WebAuthn passkeys. The passkey
flow is DittoRoo's, on FastAPI."""
from __future__ import annotations

import base64
import json
import secrets
from typing import Annotated

import webauthn
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from webauthn.helpers.structs import (
    AuthenticationCredential,
    AuthenticatorAssertionResponse,
    AuthenticatorAttestationResponse,
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    PublicKeyCredentialType,
    RegistrationCredential,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from . import security
from .security import client_ip, current_user, flash, require_user_json, require_user_page, verify_csrf
from .templating import render

router = APIRouter(include_in_schema=False)


def _safe_next(url: str | None) -> str:
    return url if url and url.startswith("/") and not url.startswith("//") else "/unions"


@router.get("/")
def index(request: Request):
    return RedirectResponse("/unions" if current_user(request) else "/login", status_code=303)


# ── Password login / register ────────────────────────────────────────────────

@router.get("/login")
def login_page(request: Request):
    if current_user(request):
        return RedirectResponse("/unions", status_code=303)
    return render(request, "landing.html", mode="login", errors={})


@router.post("/login", dependencies=[Depends(verify_csrf)])
def login_submit(request: Request, username: Annotated[str, Form()], password: Annotated[str, Form()]):
    st = request.app.state
    username = username.lower().strip()
    # Keyed by IP and username together: behind a NATing proxy every visitor
    # shares one IP, and this keeps one guesser from locking everyone out.
    # Passkey sign-in has its own bucket, so a passkey always gets you in.
    st.limiter.check("login", f"{client_ip(request)}|{username}", *st.settings.login_rate)
    user = st.db.get_user_by_username(username)
    if not user or not security.verify_password(user["password_hash"], password):
        return render(request, "landing.html", mode="login", errors={"form": "Invalid username or password."})
    security.login(request, user)
    return RedirectResponse(_safe_next(request.query_params.get("next")), status_code=303)


def _registration_open(request: Request) -> bool:
    st = request.app.state
    return st.settings.security.registration_enabled or st.db.user_count() == 0


@router.get("/register")
def register_page(request: Request):
    if current_user(request):
        return RedirectResponse("/unions", status_code=303)
    if not _registration_open(request):
        flash(request, "Registration is closed.", "error")
        return RedirectResponse("/login", status_code=303)
    return render(request, "landing.html", mode="register", errors={}, code_required=_code_required(request))


def _code_required(request: Request) -> bool:
    return bool(request.app.state.settings.security.registration_code)


@router.post("/register", dependencies=[Depends(verify_csrf)])
def register_submit(request: Request, username: Annotated[str, Form()], password: Annotated[str, Form()],
                    confirm: Annotated[str, Form()], code: Annotated[str, Form()] = ""):
    st = request.app.state
    ip = client_ip(request)
    if not _registration_open(request):
        flash(request, "Registration is closed.", "error")
        return RedirectResponse("/login", status_code=303)
    st.limiter.check("register", ip, *st.settings.register_rate)
    username = username.lower().strip()
    errors = {}
    if _code_required(request):
        # Count bad codes separately and tighter, so a guesser is cut off
        # regardless of how many usernames they try.
        st.limiter.peek("bad_code", ip, *st.settings.bad_code_rate)
        if not secrets.compare_digest(code.strip(), st.settings.security.registration_code):
            st.limiter.check("bad_code", ip, *st.settings.bad_code_rate)
            errors["code"] = "Invalid registration code."
    if not (3 <= len(username) <= 32) or not username.replace("_", "").replace("-", "").isalnum():
        errors["username"] = "3 to 32 letters, digits, - or _."
    elif st.db.get_user_by_username(username):
        errors["username"] = "Username already taken."
    if len(password) < 8:
        errors["password"] = "At least 8 characters."
    elif password != confirm:
        errors["confirm"] = "Passwords do not match."
    if errors:
        return render(request, "landing.html", mode="register", errors=errors, username=username,
                      code_required=_code_required(request))
    st.db.create_user(username, security.hash_password(password))
    flash(request, "Account created. Please sign in.", "success")
    return RedirectResponse("/login", status_code=303)


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request):
    security.logout(request)
    return RedirectResponse("/login", status_code=303)


@router.get("/account")
def account(request: Request, user: dict = Depends(require_user_page)):
    return render(request, "account.html", passkeys=request.app.state.db.get_passkeys(user["id"]))


# ── Passkeys ─────────────────────────────────────────────────────────────────

def _parse_reg_credential(body: dict) -> RegistrationCredential:
    resp = body.get("response", {})
    transports = [AuthenticatorTransport(t) for t in (resp.get("transports") or [])]
    return RegistrationCredential(
        id=body["id"],
        raw_id=webauthn.base64url_to_bytes(body["rawId"]),
        response=AuthenticatorAttestationResponse(
            client_data_json=webauthn.base64url_to_bytes(resp["clientDataJSON"]),
            attestation_object=webauthn.base64url_to_bytes(resp["attestationObject"]),
            transports=transports or None,
        ),
        type=PublicKeyCredentialType.PUBLIC_KEY,
    )


def _parse_auth_credential(body: dict) -> AuthenticationCredential:
    resp = body.get("response", {})
    return AuthenticationCredential(
        id=body["id"],
        raw_id=webauthn.base64url_to_bytes(body["rawId"]),
        response=AuthenticatorAssertionResponse(
            client_data_json=webauthn.base64url_to_bytes(resp["clientDataJSON"]),
            authenticator_data=webauthn.base64url_to_bytes(resp["authenticatorData"]),
            signature=webauthn.base64url_to_bytes(resp["signature"]),
            user_handle=webauthn.base64url_to_bytes(resp["userHandle"]) if resp.get("userHandle") else None,
        ),
        type=PublicKeyCredentialType.PUBLIC_KEY,
    )


def _json_options(options) -> Response:
    return Response(content=webauthn.options_to_json(options), media_type="application/json")


@router.post("/passkey/register/begin", dependencies=[Depends(verify_csrf)])
def passkey_register_begin(request: Request, user: dict = Depends(require_user_json)):
    st = request.app.state
    st.limiter.check("pk-reg", client_ip(request), 10, 60)
    existing = st.db.get_passkeys(user["id"])
    options = webauthn.generate_registration_options(
        rp_id=st.settings.rp_id,
        rp_name=st.settings.server.name,
        user_id=int(user["id"]).to_bytes(8, "big"),
        user_name=user["username"],
        user_display_name=user["username"],
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=c["credential_id"]) for c in existing],
    )
    request.session["webauthn_reg_challenge"] = base64.b64encode(options.challenge).decode()
    return _json_options(options)


@router.post("/passkey/register/complete", dependencies=[Depends(verify_csrf)])
async def passkey_register_complete(request: Request, user: dict = Depends(require_user_json)):
    st = request.app.state
    challenge_b64 = request.session.pop("webauthn_reg_challenge", None)
    if not challenge_b64:
        return JSONResponse({"error": "No pending registration."}, status_code=400)
    try:
        body = await request.json()
        verified = webauthn.verify_registration_response(
            credential=_parse_reg_credential(body),
            expected_challenge=base64.b64decode(challenge_b64),
            expected_rp_id=st.settings.rp_id,
            expected_origin=st.settings.origin,
        )
        st.db.save_passkey(
            user_id=user["id"], credential_id=verified.credential_id,
            public_key=verified.credential_public_key, sign_count=verified.sign_count,
            transports=json.dumps(body.get("response", {}).get("transports") or []),
            name=(body.get("name") or "Passkey")[:64],
        )
        return {"ok": True}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@router.post("/passkey/auth/begin")
def passkey_auth_begin(request: Request):
    st = request.app.state
    st.limiter.check("passkey", client_ip(request), 60, 900)
    options = webauthn.generate_authentication_options(
        rp_id=st.settings.rp_id, user_verification=UserVerificationRequirement.PREFERRED,
    )
    request.session["webauthn_auth_challenge"] = base64.b64encode(options.challenge).decode()
    return _json_options(options)


@router.post("/passkey/auth/complete")
async def passkey_auth_complete(request: Request):
    st = request.app.state
    st.limiter.check("passkey", client_ip(request), 60, 900)
    challenge_b64 = request.session.pop("webauthn_auth_challenge", None)
    if not challenge_b64:
        return JSONResponse({"error": "No pending authentication."}, status_code=400)
    try:
        body = await request.json()
        raw_id = webauthn.base64url_to_bytes(body["rawId"])
        stored = st.db.get_passkey_by_credential_id(raw_id)
        if not stored:
            return JSONResponse({"error": "Unknown passkey."}, status_code=401)
        verified = webauthn.verify_authentication_response(
            credential=_parse_auth_credential(body),
            expected_challenge=base64.b64decode(challenge_b64),
            expected_rp_id=st.settings.rp_id,
            expected_origin=st.settings.origin,
            credential_public_key=bytes(stored["public_key"]),
            credential_current_sign_count=stored["sign_count"],
        )
        st.db.update_passkey_sign_count(raw_id, verified.new_sign_count)
        user = st.db.get_user_by_id(stored["user_id"])
        if not user:
            return JSONResponse({"error": "User not found."}, status_code=401)
        security.login(request, user)
        return {"redirect": "/unions"}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=401)


@router.post("/passkey/{pk_id}/delete", dependencies=[Depends(verify_csrf)])
def passkey_delete(request: Request, pk_id: int, user: dict = Depends(require_user_page)):
    request.app.state.db.delete_passkey(pk_id, user["id"])
    flash(request, "Passkey removed.", "success")
    return RedirectResponse("/account", status_code=303)
