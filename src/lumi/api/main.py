"""The web backend.

Two jobs: authenticate users, and bridge their browser to the bus. The live data
path is the generic contract-driven bridge in `bridge.py`; there are no
per-capability routes and none are generated. Adding a capability to the contract
makes it reachable from the browser with no change here.

Beside the `/ws` bridge sits a small HTTP surface -- login and account management
(`/auth/*`) and per-user UI settings (`/users/me/settings`) -- backed by a SQLite
user store (`db.py`). Login mints a JWT carrying the user's role; the browser
presents it on the `/ws` upgrade, and `auth.authenticate` resolves it to an
`Identity(user, role)`. Everything the browser can then do on the bus is gated by
that role, enforced in `BridgeSession` with the same predicate the broker uses
(`lumi.contracts.policy`). The broker is never exposed to the browser.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from aio_pika import connect_robust
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from lumi.config import settings

from .auth import authenticate
from .bridge import BridgeSession, BusProxy
from .db import UserStore, bootstrap_default_user, ensure_anonymous_user
from .deps import auth_enabled
from .routes import auth as auth_routes
from .routes import settings as settings_routes

log = logging.getLogger(__name__)


def _amqp_url() -> str:
    """Where the bridge connects to the bus.

    LUMI_AMQP_URL wins if set -- a single explicit override for deployment and for
    tests, so pointing the backend at a specific broker never depends on dynaconf's
    env-var caching (which reads settings.toml's lab IP if any module touched settings
    before an env override was set).
    """
    if url := os.environ.get("LUMI_AMQP_URL"):
        return url
    api = settings.get("api", {})
    return (
        f"amqp://{api.get('bus_user', 'guest')}:"
        f"{api.get('bus_password', 'guest')}@{settings.rabbitmq.host}/"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The user store comes up first and independently of the bus, so login and
    # account management keep working even if the broker is unreachable.
    user_store = UserStore()
    await user_store.connect()
    await bootstrap_default_user(user_store)
    # `auth.enabled = false` is the gate: only a wide-open (simulation/dev) backend
    # gets the anonymous row, so a production database never grows one. Nothing else
    # needs configuring -- production keeps auth on, and then this is skipped.
    if not auth_enabled():
        await ensure_anonymous_user(user_store)
    app.state.user_store = user_store

    connection = None
    proxy = None
    try:
        # One privileged connection to the bus, shared by every browser session.
        # Browsers never get broker credentials -- that is the whole point of the
        # backend. A short connect timeout so a wrong/unreachable broker fails fast
        # (~5s) rather than hanging on the TCP default (~135s).
        connection = await connect_robust(_amqp_url(), timeout=5.0)
        channel = await connection.channel()
        proxy = BusProxy(connection, channel)
        await proxy.start()
        app.state.proxy = proxy
        yield
    finally:
        if proxy is not None:
            await proxy.stop()
        if connection is not None:
            await connection.close()
        await user_store.close()


app = FastAPI(title="lumi", lifespan=lifespan)

# The browser origin(s). Tighten for production rather than allowing "*".
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.get("api", {}).get("allow_origins", ["*"]),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Login / account management and per-user UI settings. Protected endpoints gate
# themselves with the get_current_user dependency; there is no global middleware.
app.include_router(auth_routes.router)
app.include_router(settings_routes.router)


@app.get("/health")
async def health() -> dict:
    proxy: BusProxy = app.state.proxy
    return {"ok": True, "capabilities": sorted(proxy.clients)}


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    """The one browser endpoint. Authenticate, then hand off to the generic bridge."""
    identity = await authenticate(websocket)
    if identity is None:
        await websocket.close(code=4401)  # unauthorized
        return

    await websocket.accept()
    session = BridgeSession(
        ws=websocket,
        proxy=app.state.proxy,
        role=identity.role,
        user=identity.user,
    )
    try:
        await session.run()
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("bridge session crashed")
