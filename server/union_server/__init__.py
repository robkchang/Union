"""Union server application factory."""
from __future__ import annotations

import logging
import pathlib
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import config, hubkeys
from .db import Database
from .presence import Registry
from .security import LoginRequired, RateLimiter
from .signing import NonceCache

SERVER_ROOT = pathlib.Path(__file__).resolve().parent.parent
__version__ = "0.1.0"
log = logging.getLogger("union")


def create_app(settings: config.Settings | None = None) -> FastAPI:
    settings = settings or config.load()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await app.state.registry.start()
        log.info("Union ready: %s (hub fingerprint %s)", settings.server.public_url, app.state.hub_keys.fingerprint)
        yield
        await app.state.registry.stop()

    from .docs import API_DESCRIPTION, TAGS, install_openapi

    app = FastAPI(
        title="Union API",
        version=__version__,
        description=API_DESCRIPTION,
        openapi_tags=TAGS,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )

    st = app.state
    st.settings = settings
    st.db = Database(settings.data_dir / "union.db")
    st.db.init()
    st.db.mark_all_offline()
    st.hub_keys = hubkeys.load_or_create(settings.data_dir)
    st.registry = Registry(settings.data_dir / "blobs", settings.relay.delivery_window_seconds,
                           settings.relay.blob_window_seconds)
    st.limiter = RateLimiter()
    st.nonces = NonceCache()
    st.templates = Jinja2Templates(directory=str(SERVER_ROOT / "templates"))

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session.secret_key,
        session_cookie="union_session",
        max_age=settings.session.remember_me_days * 86400,
        same_site="lax",
        https_only=settings.https,
    )
    app.mount("/static", StaticFiles(directory=str(SERVER_ROOT / "static")), name="static")

    from . import auth, docs, relay, unions
    app.include_router(auth.router)
    app.include_router(unions.router)
    app.include_router(relay.router)
    app.include_router(docs.router)
    install_openapi(app)

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        return RedirectResponse(f"/login?next={exc.next_url}", status_code=303)

    @app.exception_handler(HTTPException)
    async def _http_exc(request: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {"error": "error", "detail": str(exc.detail)}
        return JSONResponse(detail, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        return JSONResponse({"error": "validation", "detail": exc.errors()}, status_code=422)

    return app
