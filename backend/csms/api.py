"""REST + live WebSocket API for the Android app."""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, status
from fastapi.websockets import WebSocketDisconnect
from pydantic import BaseModel, Field

from . import auth
from .hub import Hub
from .ocpp_handler import ChargePointSession, CommandError

router = APIRouter(prefix="/api")


def get_hub(request: Request) -> Hub:
    return request.app.state.hub


HubDep = Annotated[Hub, Depends(get_hub)]


def _bearer(header: str | None) -> str | None:
    if header and header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


async def current_user(request: Request, hub: HubDep) -> dict[str, Any]:
    token = _bearer(request.headers.get("authorization"))
    user = token and await auth.user_for_token(hub.db, token, hub.settings.token_ttl_days)
    if not user:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Not logged in",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


UserDep = Annotated[dict, Depends(current_user)]


async def admin_user(user: UserDep) -> dict[str, Any]:
    if not user["is_admin"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admins only")
    return user


AdminDep = Annotated[dict, Depends(admin_user)]


def _session(hub: Hub, charger_id: str) -> ChargePointSession:
    if charger_id not in hub.chargers:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown charger")
    session = hub.session(charger_id)
    if session is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "The charger is offline")
    return session


def _check_connector(connector_id: int, allow_zero: bool = False) -> None:
    if connector_id < (0 if allow_zero else 1) or connector_id > 2:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown connector")


async def _run(coro) -> str:
    try:
        return await coro
    except CommandError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from e


def _require_accepted(result: str, what: str) -> dict[str, str]:
    if result != "Accepted":
        raise HTTPException(status.HTTP_409_CONFLICT, f"The charger refused to {what} ({result})")
    return {"status": result}


# ---------------------------------------------------------------------- auth


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/auth/login")
async def login(body: LoginRequest, hub: HubDep) -> dict[str, Any]:
    user = await hub.db.fetchone("SELECT * FROM users WHERE username = ?", (body.username,))
    if not user or not auth.verify_password(body.password, user["password_hash"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong username or password")
    token = await auth.issue_token(hub.db, user["id"])
    return {
        "token": token,
        "user": {
            "id": user["id"],
            "username": user["username"],
            "display_name": user["display_name"],
            "is_admin": bool(user["is_admin"]),
        },
    }


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, hub: HubDep, user: UserDep) -> None:
    await auth.revoke_token(hub.db, _bearer(request.headers.get("authorization")) or "")


@router.get("/me")
async def me(hub: HubDep, user: UserDep) -> dict[str, Any]:
    tags = await hub.db.fetchall(
        "SELECT id_tag, label, is_app_tag, blocked FROM id_tags WHERE user_id = ?", (user["id"],)
    )
    return {**user, "is_admin": bool(user["is_admin"]), "id_tags": tags}


# ------------------------------------------------------------------ chargers


@router.get("/chargers")
async def list_chargers(hub: HubDep, user: UserDep) -> list[dict[str, Any]]:
    return [c.to_dict() for c in hub.chargers.values()]


@router.get("/chargers/{charger_id}")
async def get_charger(charger_id: str, hub: HubDep, user: UserDep) -> dict[str, Any]:
    if charger_id not in hub.chargers:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown charger")
    return hub.chargers[charger_id].to_dict()


@router.post("/chargers/{charger_id}/connectors/{connector_id}/start")
async def start_charging(
    charger_id: str, connector_id: int, hub: HubDep, user: UserDep
) -> dict[str, str]:
    _check_connector(connector_id)
    session = _session(hub, charger_id)
    if hub.charger(charger_id).connector(connector_id).transaction_id is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Already charging on this connector")
    id_tag = await hub.db.app_tag_for_user(user["id"])
    if not id_tag:
        raise HTTPException(status.HTTP_409_CONFLICT, "Your account has no active app tag")
    result = await _run(session.remote_start(connector_id, id_tag))
    return _require_accepted(result, "start charging")


@router.post("/chargers/{charger_id}/connectors/{connector_id}/stop")
async def stop_charging(
    charger_id: str, connector_id: int, hub: HubDep, user: UserDep
) -> dict[str, str]:
    _check_connector(connector_id)
    session = _session(hub, charger_id)
    tx_id = hub.charger(charger_id).connector(connector_id).transaction_id
    if tx_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Nothing is charging on this connector")
    result = await _run(session.remote_stop(tx_id))
    return _require_accepted(result, "stop charging")


class LimitRequest(BaseModel):
    # 0 A pauses charging; otherwise IEC 61851 needs at least 6 A.
    amps: float = Field(ge=0, le=32)


@router.put("/chargers/{charger_id}/connectors/{connector_id}/limit")
async def set_limit(
    charger_id: str, connector_id: int, body: LimitRequest, hub: HubDep, user: UserDep
) -> dict[str, str]:
    """Connector 0 limits the charger as a whole."""
    _check_connector(connector_id, allow_zero=True)
    if 0 < body.amps < 6:
        raise HTTPException(422, "Use 0 A or at least 6 A")
    session = _session(hub, charger_id)
    result = await _run(session.set_current_limit(connector_id, body.amps))
    return _require_accepted(result, "set the current limit")


@router.delete("/chargers/{charger_id}/connectors/{connector_id}/limit")
async def clear_limit(
    charger_id: str, connector_id: int, hub: HubDep, user: UserDep
) -> dict[str, str]:
    _check_connector(connector_id, allow_zero=True)
    session = _session(hub, charger_id)
    result = await _run(session.clear_current_limit(connector_id))
    # "Unknown" means there was no limit to clear, which is fine.
    return {"status": result}


@router.post("/chargers/{charger_id}/connectors/{connector_id}/unlock")
async def unlock(charger_id: str, connector_id: int, hub: HubDep, user: UserDep) -> dict[str, str]:
    _check_connector(connector_id)
    session = _session(hub, charger_id)
    return {"status": await _run(session.unlock(connector_id))}


# ----------------------------------------------------------------- sessions


def _session_row(row: dict[str, Any]) -> dict[str, Any]:
    stop = row["meter_stop_wh"]
    row["energy_wh"] = None if stop is None else max(0, stop - row["meter_start_wh"])
    return row


@router.get("/sessions")
async def list_sessions(
    hub: HubDep,
    user: UserDep,
    limit: int = Query(50, ge=1, le=500),
    before_id: int | None = None,
    user_id: int | None = None,
) -> list[dict[str, Any]]:
    sql = (
        "SELECT t.*, u.display_name AS user_name FROM transactions t "
        "LEFT JOIN users u ON u.id = t.user_id WHERE 1=1"
    )
    params: list[Any] = []
    if before_id is not None:
        sql += " AND t.id < ?"
        params.append(before_id)
    if user_id is not None:
        sql += " AND t.user_id = ?"
        params.append(user_id)
    sql += " ORDER BY t.id DESC LIMIT ?"
    params.append(limit)
    return [_session_row(r) for r in await hub.db.fetchall(sql, tuple(params))]


@router.get("/sessions/{session_id}")
async def get_session(session_id: int, hub: HubDep, user: UserDep) -> dict[str, Any]:
    row = await hub.db.fetchone(
        "SELECT t.*, u.display_name AS user_name FROM transactions t "
        "LEFT JOIN users u ON u.id = t.user_id WHERE t.id = ?",
        (session_id,),
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown session")
    row = _session_row(row)
    row["samples"] = await hub.db.fetchall(
        "SELECT ts, energy_wh, power_w FROM meter_samples WHERE transaction_id = ? ORDER BY ts",
        (session_id,),
    )
    return row


# -------------------------------------------------------------------- admin


@router.get("/chargers/{charger_id}/configuration")
async def get_configuration(
    charger_id: str, hub: HubDep, admin: AdminDep, refresh: bool = False
) -> list[dict[str, Any]]:
    """The charger's OCPP configuration keys. refresh=true asks the charger again."""
    if refresh:
        return await _run(_session(hub, charger_id).fetch_configuration())
    row = await hub.db.fetchone(
        "SELECT configuration FROM charge_points WHERE id = ?", (charger_id,)
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown charger")
    return json.loads(row["configuration"] or "[]")


class ConfigValue(BaseModel):
    value: str


@router.put("/chargers/{charger_id}/configuration/{key}")
async def change_configuration(
    charger_id: str, key: str, body: ConfigValue, hub: HubDep, admin: AdminDep
) -> dict[str, str]:
    session = _session(hub, charger_id)
    return {"status": await _run(session.change_configuration(key, body.value))}


@router.post("/chargers/{charger_id}/reset")
async def reset(
    charger_id: str, hub: HubDep, admin: AdminDep, hard: bool = False
) -> dict[str, str]:
    session = _session(hub, charger_id)
    return {"status": await _run(session.reset(hard))}


@router.get("/id-tags/unknown")
async def unknown_tags(hub: HubDep, admin: AdminDep) -> list[dict[str, Any]]:
    """Tags the charger presented that aren't registered, newest first: this is how
    you find an RFID card's id before assigning it with the CLI."""
    return await hub.db.fetchall("SELECT * FROM unknown_id_tags ORDER BY last_seen DESC")


@router.get("/ocpp-log")
async def ocpp_log(
    hub: HubDep, admin: AdminDep, limit: int = Query(200, ge=1, le=5000)
) -> list[dict[str, Any]]:
    return await hub.db.fetchall("SELECT * FROM ocpp_log ORDER BY id DESC LIMIT ?", (limit,))


# --------------------------------------------------------------------- live


@router.websocket("/live")
async def live(websocket: WebSocket) -> None:
    """Pushes a snapshot of every charger, then each change as it happens.
    Authenticate with the same Authorization: Bearer header as the REST API."""
    hub: Hub = websocket.app.state.hub
    token = _bearer(websocket.headers.get("authorization"))
    user = token and await auth.user_for_token(hub.db, token, hub.settings.token_ttl_days)
    if not user:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    queue = hub.bus.subscribe()
    try:
        await websocket.send_json(
            {"type": "snapshot", "chargers": [c.to_dict() for c in hub.chargers.values()]}
        )
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30)
            except TimeoutError:
                event = {"type": "ping"}
            await websocket.send_json(event)
    except WebSocketDisconnect:
        pass
    finally:
        hub.bus.unsubscribe(queue)
