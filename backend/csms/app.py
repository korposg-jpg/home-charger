"""FastAPI application: the charger's OCPP endpoint and the app API on one port."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket
from fastapi.websockets import WebSocketDisconnect

from .api import router as api_router
from .config import Settings, get_settings
from .db import Database
from .hub import Hub
from .ocpp_handler import ChargePointSession, LoggingConnection

log = logging.getLogger(__name__)

OCPP_SUBPROTOCOL = "ocpp1.6"


def _basic_auth_ok(websocket: WebSocket, cp_id: str, password: str) -> bool:
    header = websocket.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return False
    try:
        user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
    except ValueError:
        return False
    return user == cp_id and pw == password


async def _prune_loop(hub: Hub) -> None:
    while True:
        try:
            await hub.db.prune(hub.settings.ocpp_log_retention_days, hub.settings.token_ttl_days)
        except Exception:  # noqa: BLE001
            log.exception("prune failed")
        await asyncio.sleep(6 * 3600)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(settings.db_path)
        await db.connect()
        hub = Hub(settings, db)
        await hub.load()
        app.state.hub = hub
        pruner = asyncio.create_task(_prune_loop(hub))
        yield
        pruner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pruner
        await db.close()

    app = FastAPI(title="Home charger CSMS", version="0.1.0", lifespan=lifespan)
    app.include_router(api_router)

    @app.get("/healthz")
    async def healthz() -> dict:
        hub: Hub = app.state.hub
        return {"ok": True, "chargers_online": sorted(hub.sessions)}

    async def ocpp_endpoint(websocket: WebSocket, cp_id: str) -> None:
        hub: Hub = websocket.app.state.hub
        allowed = settings.allowed_ids()
        if allowed and cp_id not in allowed:
            log.warning("rejected unknown charge point %s", cp_id)
            await websocket.close(code=1008)
            return
        if settings.ocpp_basic_auth_password and not _basic_auth_ok(
            websocket, cp_id, settings.ocpp_basic_auth_password
        ):
            log.warning("rejected charge point %s: bad credentials", cp_id)
            await websocket.close(code=1008)
            return

        offered = [
            p.strip()
            for p in websocket.headers.get("sec-websocket-protocol", "").split(",")
            if p.strip()
        ]
        if OCPP_SUBPROTOCOL in offered:
            await websocket.accept(subprotocol=OCPP_SUBPROTOCOL)
        else:
            # Spec says the charger must offer ocpp1.6, but being strict here only
            # locks out a charger that would otherwise work.
            log.warning("%s connected without offering %s (%s)", cp_id, OCPP_SUBPROTOCOL, offered)
            await websocket.accept()

        previous = hub.sessions.get(cp_id)
        if previous is not None:
            log.info("%s reconnected; dropping the old connection", cp_id)
            with contextlib.suppress(Exception):
                await previous._connection._ws.close()

        await hub.db.upsert_charge_point(cp_id)
        state = hub.charger(cp_id)
        await hub.restore_transactions(cp_id)
        session = ChargePointSession(cp_id, LoggingConnection(websocket, hub, cp_id), hub)
        hub.sessions[cp_id] = session
        state.online = True
        hub.publish_charger(cp_id)
        log.info("%s connected", cp_id)
        try:
            await session.start()
        except WebSocketDisconnect:
            pass
        except RuntimeError:
            # Raised when we closed this socket ourselves because the charger reconnected.
            pass
        finally:
            if hub.sessions.get(cp_id) is session:
                del hub.sessions[cp_id]
                state.online = False
                hub.publish_charger(cp_id)
            log.info("%s disconnected", cp_id)

    # Chargers append their own id to the configured URL, so accept both
    # ws://server:9000/<id> and ws://server:9000/ocpp/<id>.
    app.add_api_websocket_route("/ocpp/{cp_id}", ocpp_endpoint)
    app.add_api_websocket_route("/{cp_id}", ocpp_endpoint)
    return app


def run() -> None:
    import uvicorn

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = get_settings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, ws_ping_interval=None)
