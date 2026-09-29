"""End-to-end tests: the CSMS against the simulated DE-2, driven through the app API."""

from __future__ import annotations

import asyncio
import json

import pytest
from websockets.asyncio.client import connect

from conftest import wait_for
from csms.ocpp_handler import parse_meter_values

CP = "DE2-SIM"


def connector(env, n):
    return env.hub.chargers[CP].connectors[n]


async def test_boot_registers_charger_with_two_connectors(env):
    await env.connect_station()
    async with env.client("partner") as c:
        charger = (await c.get(f"/api/chargers/{CP}")).json()
    assert charger["online"] is True
    assert charger["vendor"] == "Depow"
    assert charger["supports_smart_charging"] is True
    assert [x["connector_id"] for x in charger["connectors"]] == [1, 2]
    assert all(x["status"] == "Available" for x in charger["connectors"])


async def test_remote_start_and_stop_is_attributed_to_user(env):
    station = await env.connect_station()
    await station.plug(1)
    await wait_for(lambda: connector(env, 1).status == "Preparing")

    async with env.client("partner") as c:
        r = await c.post(f"/api/chargers/{CP}/connectors/1/start")
        assert r.status_code == 200, r.text
        await wait_for(lambda: connector(env, 1).status == "Charging")
        # One outlet alone gets the full 16 A on three phases.
        await wait_for(lambda: (connector(env, 1).power_w or 0) > 11000)
        await wait_for(lambda: (connector(env, 1).session_energy_wh() or 0) > 0)
        live = (await c.get(f"/api/chargers/{CP}")).json()["connectors"][0]
        assert live["user_name"] == "Partner"

        r = await c.post(f"/api/chargers/{CP}/connectors/1/stop")
        assert r.status_code == 200, r.text
        await wait_for(lambda: connector(env, 1).status == "Finishing")

        sessions = (await c.get("/api/sessions")).json()
    assert len(sessions) == 1
    s = sessions[0]
    assert s["user_name"] == "Partner"
    assert s["stop_reason"] == "Remote"
    assert s["energy_wh"] > 0

    async with env.client("gabor") as c:
        detail = (await c.get(f"/api/sessions/{s['id']}")).json()
    assert len(detail["samples"]) > 0


async def test_remote_start_before_plugging_in_waits_for_car(env):
    station = await env.connect_station()
    async with env.client("gabor") as c:
        r = await c.post(f"/api/chargers/{CP}/connectors/2/start")
        assert r.status_code == 200
    assert connector(env, 2).transaction_id is None
    await station.plug(2)
    await wait_for(lambda: connector(env, 2).status == "Charging")
    assert connector(env, 2).user_name == "Gabor"


async def test_two_cars_share_supply_and_limits_apply(env):
    station = await env.connect_station(supply_max_a=16)
    await station.plug(1)
    await station.plug(2)
    async with env.client("gabor") as g, env.client("partner") as p:
        assert (await g.post(f"/api/chargers/{CP}/connectors/1/start")).status_code == 200
        assert (await p.post(f"/api/chargers/{CP}/connectors/2/start")).status_code == 200
        await wait_for(lambda: connector(env, 2).status == "Charging")
        await wait_for(lambda: station.connectors[1].current_a == 8)
        assert station.connectors[2].current_a == 8

        r = await g.put(f"/api/chargers/{CP}/connectors/1/limit", json={"amps": 6})
        assert r.status_code == 200, r.text
        assert connector(env, 1).limit_a == 6
        await wait_for(lambda: station.connectors[1].current_a == 6)

        # 0 A pauses the outlet; the charger reports it as suspended by the EVSE.
        r = await g.put(f"/api/chargers/{CP}/connectors/1/limit", json={"amps": 0})
        assert r.status_code == 200
        await wait_for(lambda: connector(env, 1).status == "SuspendedEVSE")

        r = await g.delete(f"/api/chargers/{CP}/connectors/1/limit")
        assert r.json()["status"] == "Accepted"
        await wait_for(lambda: connector(env, 1).status == "Charging")
        assert connector(env, 1).limit_a is None

        # Values between 0 and 6 A aren't valid charging currents.
        r = await g.put(f"/api/chargers/{CP}/connectors/1/limit", json={"amps": 3})
        assert r.status_code == 422


async def test_limit_reports_when_charger_lacks_smart_charging(env):
    await env.connect_station(supports_smart_charging=False)
    async with env.client("gabor") as c:
        r = await c.put(f"/api/chargers/{CP}/connectors/1/limit", json={"amps": 10})
        charger = (await c.get(f"/api/chargers/{CP}")).json()
    assert r.status_code == 409
    assert "NotSupported" in r.json()["detail"]
    assert charger["supports_smart_charging"] is False


async def test_unknown_rfid_is_rejected_and_listed_then_assignable(env):
    station = await env.connect_station()
    await station.plug(1)
    assert await station.swipe(1, "CARD-0042") == "rejected"
    async with env.client("gabor") as c:
        unknown = (await c.get("/api/id-tags/unknown")).json()
    assert [t["id_tag"] for t in unknown] == ["CARD-0042"]

    db = env.hub.db
    user = await db.fetchone("SELECT id FROM users WHERE username = 'partner'")
    await db.execute(
        "INSERT INTO id_tags (id_tag, user_id, label) VALUES ('CARD-0042', ?, 'card')",
        (user["id"],),
    )
    assert await station.swipe(1, "CARD-0042") == "started"
    await wait_for(lambda: connector(env, 1).user_name == "Partner")
    assert await station.swipe(1, "CARD-0042") == "stopped"


async def test_unplugging_ends_session(env):
    station = await env.connect_station()
    await station.plug(1)
    async with env.client("gabor") as c:
        await c.post(f"/api/chargers/{CP}/connectors/1/start")
        await wait_for(lambda: connector(env, 1).status == "Charging")
        await station.unplug(1)
        await wait_for(lambda: connector(env, 1).status == "Available")
        sessions = (await c.get("/api/sessions")).json()
    assert sessions[0]["stop_reason"] == "EVDisconnected"


async def test_api_requires_login_and_admin(env):
    await env.connect_station()
    async with env.client() as anon:
        assert (await anon.get("/api/chargers")).status_code == 401
        bad = await anon.post("/api/auth/login", json={"username": "gabor", "password": "x"})
        assert bad.status_code == 401
    async with env.client("partner") as c:
        assert (await c.get(f"/api/chargers/{CP}/configuration")).status_code == 403
    async with env.client("gabor") as c:
        config = (await c.get(f"/api/chargers/{CP}/configuration")).json()
        keys = {k["key"] for k in config}
        assert "SupportedFeatureProfiles" in keys
        log = (await c.get("/api/ocpp-log?limit=5")).json()
        assert len(log) == 5


async def test_offline_charger(env):
    await env.connect_station()
    await env.disconnect_station()
    await wait_for(lambda: not env.hub.chargers[CP].online)
    async with env.client("gabor") as c:
        r = await c.post(f"/api/chargers/{CP}/connectors/1/start")
        charger = (await c.get(f"/api/chargers/{CP}")).json()
    assert r.status_code == 503
    assert charger["online"] is False
    # The last known state is still there for the app to show.
    assert charger["connectors"][0]["status"] == "Available"


async def test_live_feed_sends_snapshot_then_changes(env):
    station = await env.connect_station()
    headers = {"Authorization": f"Bearer {env.tokens['partner']}"}
    async with connect(f"{env.ws_url}/api/live", additional_headers=headers) as ws:
        first = json.loads(await ws.recv())
        assert first["type"] == "snapshot"
        assert first["chargers"][0]["id"] == CP
        await station.plug(2)
        # Status events from the post-boot TriggerMessage may still be in flight, so
        # read until connector 2 reports the plug-in.
        while True:
            event = json.loads(await asyncio.wait_for(ws.recv(), 5))
            c = event.get("connector") or {}
            if c.get("connector_id") == 2 and c.get("status") == "Preparing":
                break


async def test_live_feed_rejects_missing_token(env):
    with pytest.raises(Exception):  # noqa: B017 - handshake/close error types vary
        async with connect(f"{env.ws_url}/api/live") as ws:
            await ws.recv()


def test_parse_meter_values_handles_kwh_and_per_phase_power():
    parsed = parse_meter_values(
        [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "sampled_value": [
                    {"value": "12.5", "unit": "kWh"},
                    {
                        "value": "1.2",
                        "measurand": "Power.Active.Import",
                        "unit": "kW",
                        "phase": "L1",
                    },
                    {"value": "1300", "measurand": "Power.Active.Import", "phase": "L2"},
                    {"value": "5.1", "measurand": "Current.Import", "phase": "L1-N"},
                    {"value": "junk", "measurand": "Current.Import", "phase": "L2"},
                ],
            }
        ]
    )
    assert parsed["energy_wh"] == 12500
    assert parsed["power_w"] == 2500
    assert parsed["current_a"] == [5.1]
