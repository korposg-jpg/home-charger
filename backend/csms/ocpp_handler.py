"""OCPP 1.6J handling for one connected charge point."""

from __future__ import annotations

import json
import logging
from typing import Any

from ocpp.routing import after, on
from ocpp.v16 import ChargePoint, call, call_result
from ocpp.v16 import datatypes as dt
from ocpp.v16 import enums as en

from .db import iso
from .hub import Hub

log = logging.getLogger(__name__)

# Charging profile ids we own. Using fixed ids means a new limit replaces the old one.
MAX_PROFILE_ID = 100
TX_DEFAULT_PROFILE_ID_BASE = 110


class LoggingConnection:
    """Adapts a FastAPI WebSocket to the recv/send interface the ocpp library wants,
    and records every raw frame in the OCPP log."""

    def __init__(self, websocket, hub: Hub, cp_id: str):
        self._ws = websocket
        self._hub = hub
        self._cp_id = cp_id

    async def recv(self) -> str:
        msg = await self._ws.receive_text()
        await self._hub.db.log_ocpp(self._cp_id, "in", msg)
        return msg

    async def send(self, msg: str) -> None:
        await self._hub.db.log_ocpp(self._cp_id, "out", msg)
        await self._ws.send_text(msg)


def _to_wh(value: str, unit: str | None) -> float:
    v = float(value)
    return v * 1000 if (unit or "Wh") == "kWh" else v


def _to_w(value: str, unit: str | None) -> float:
    v = float(value)
    return v * 1000 if (unit or "W") == "kW" else v


def parse_meter_values(meter_value: list[dict]) -> dict[str, Any]:
    """Pull energy register, total power and per-phase current out of MeterValues.

    Chargers differ a lot here (some report per phase only, some in kWh), so this is
    deliberately forgiving and ignores anything it doesn't understand."""
    energy_wh: float | None = None
    power_w: float | None = None
    phase_power: list[float] = []
    currents: dict[str, float] = {}
    for mv in meter_value:
        for sv in mv.get("sampled_value", []):
            measurand = sv.get("measurand", "Energy.Active.Import.Register")
            unit, phase = sv.get("unit"), sv.get("phase")
            try:
                if measurand == "Energy.Active.Import.Register" and not phase:
                    energy_wh = _to_wh(sv["value"], unit)
                elif measurand == "Power.Active.Import":
                    if phase:
                        phase_power.append(_to_w(sv["value"], unit))
                    else:
                        power_w = _to_w(sv["value"], unit)
                elif measurand == "Current.Import" and phase:
                    currents[phase[:2]] = float(sv["value"])
            except (KeyError, ValueError):
                continue
    if power_w is None and phase_power:
        power_w = sum(phase_power)
    return {
        "energy_wh": energy_wh,
        "power_w": power_w,
        "current_a": [currents[p] for p in sorted(currents)],
    }


class CommandError(Exception):
    """The charger refused a request or didn't answer."""


class ChargePointSession(ChargePoint):
    def __init__(self, cp_id: str, connection: LoggingConnection, hub: Hub):
        super().__init__(cp_id, connection, response_timeout=hub.settings.ocpp_response_timeout)
        self.hub = hub
        self.db = hub.db
        self.state = hub.charger(cp_id)

    # ------------------------------------------------------------------ inbound

    @on(en.Action.boot_notification)
    async def on_boot_notification(self, charge_point_vendor, charge_point_model, **kwargs):
        now = iso()
        await self.db.upsert_charge_point(
            self.id,
            vendor=charge_point_vendor,
            model=charge_point_model,
            serial_number=kwargs.get("charge_point_serial_number")
            or kwargs.get("charge_box_serial_number"),
            firmware_version=kwargs.get("firmware_version"),
            last_boot=now,
            last_seen=now,
        )
        self.state.vendor, self.state.model = charge_point_vendor, charge_point_model
        self.state.firmware_version = kwargs.get("firmware_version")
        self.hub.publish_charger(self.id)
        return call_result.BootNotification(
            current_time=now,
            interval=self.hub.settings.heartbeat_interval,
            status=en.RegistrationStatus.accepted,
        )

    @after(en.Action.boot_notification)
    async def after_boot_notification(self, **kwargs):
        # Learn what the charger supports, then ask it for fresh connector status.
        try:
            await self.fetch_configuration()
        except Exception:  # noqa: BLE001 - a probe failing must not drop the charger
            log.exception("%s: GetConfiguration after boot failed", self.id)
        for connector_id in (0, 1, 2):
            try:
                await self.call(
                    call.TriggerMessage(
                        requested_message=en.MessageTrigger.status_notification,
                        connector_id=connector_id,
                    )
                )
            except Exception:  # noqa: BLE001
                log.warning("%s: TriggerMessage for connector %s failed", self.id, connector_id)

    @on(en.Action.heartbeat)
    async def on_heartbeat(self):
        await self._touch()
        return call_result.Heartbeat(current_time=iso())

    @on(en.Action.status_notification)
    async def on_status_notification(self, connector_id, error_code, status, **kwargs):
        info = kwargs.get("info") or kwargs.get("vendor_error_code")
        await self.db.upsert_connector(self.id, connector_id, status, error_code, info)
        conn = self.state.connector(connector_id)
        conn.status, conn.error_code, conn.info = status, error_code, info
        if status in ("Available", "Unavailable", "Faulted", "Finishing"):
            conn.power_w = 0.0
            conn.current_a = []
        await self._touch()
        self.hub.publish_connector(self.id, connector_id)
        return call_result.StatusNotification()

    @on(en.Action.authorize)
    async def on_authorize(self, id_tag):
        status, _ = await self._authorize(id_tag)
        return call_result.Authorize(id_tag_info=dt.IdTagInfo(status=status))

    @on(en.Action.start_transaction)
    async def on_start_transaction(self, connector_id, id_tag, meter_start, timestamp, **kwargs):
        status, user = await self._authorize(id_tag)
        tx_id = await self.db.start_transaction(
            self.id, connector_id, id_tag, user["id"] if user else None, meter_start, timestamp
        )
        conn = self.state.connector(connector_id)
        conn.transaction_id = tx_id
        conn.transaction_started_at = timestamp
        conn.meter_start_wh = meter_start
        conn.meter_wh = meter_start
        conn.user_id = user["id"] if user else None
        conn.user_name = user["display_name"] if user else None
        self.hub.publish_connector(self.id, connector_id)
        return call_result.StartTransaction(
            transaction_id=tx_id, id_tag_info=dt.IdTagInfo(status=status)
        )

    @on(en.Action.stop_transaction)
    async def on_stop_transaction(self, meter_stop, timestamp, transaction_id, **kwargs):
        await self.db.stop_transaction(transaction_id, meter_stop, timestamp, kwargs.get("reason"))
        for n, conn in self.state.connectors.items():
            if conn.transaction_id == transaction_id:
                conn.meter_wh = meter_stop
                conn.transaction_id = None
                conn.transaction_started_at = None
                conn.meter_start_wh = None
                conn.user_id = None
                conn.user_name = None
                conn.power_w = 0.0
                conn.current_a = []
                self.hub.publish_connector(self.id, n)
        return call_result.StopTransaction(
            id_tag_info=dt.IdTagInfo(status=en.AuthorizationStatus.accepted)
        )

    # Schema validation is skipped: meter values are where chargers are least compliant,
    # and parse_meter_values copes with odd shapes.
    @on(en.Action.meter_values, skip_schema_validation=True)
    async def on_meter_values(self, connector_id, meter_value, **kwargs):
        parsed = parse_meter_values(meter_value)
        conn = self.state.connector(connector_id)
        if parsed["energy_wh"] is not None:
            conn.meter_wh = parsed["energy_wh"]
        if parsed["power_w"] is not None:
            conn.power_w = parsed["power_w"]
        if parsed["current_a"]:
            conn.current_a = parsed["current_a"]
        tx_id = kwargs.get("transaction_id") or conn.transaction_id
        if tx_id:
            await self.db.add_meter_sample(tx_id, iso(), parsed["energy_wh"], parsed["power_w"])
        await self._touch()
        self.hub.publish_connector(self.id, connector_id)
        return call_result.MeterValues()

    @on(en.Action.data_transfer)
    async def on_data_transfer(self, vendor_id, **kwargs):
        # Vendor-specific; logged in the OCPP log so we can see what the DE-2 sends.
        return call_result.DataTransfer(status=en.DataTransferStatus.unknown_vendor_id)

    @on(en.Action.firmware_status_notification)
    async def on_firmware_status_notification(self, status):
        return call_result.FirmwareStatusNotification()

    @on(en.Action.diagnostics_status_notification)
    async def on_diagnostics_status_notification(self, status):
        return call_result.DiagnosticsStatusNotification()

    # ----------------------------------------------------------------- outbound

    async def _call(self, payload):
        try:
            return await self.call(payload, suppress=False)
        except TimeoutError as e:
            raise CommandError("The charger didn't answer in time.") from e
        except Exception as e:  # CallError from the charger
            raise CommandError(f"The charger returned an error: {e}") from e

    async def remote_start(self, connector_id: int, id_tag: str) -> str:
        res = await self._call(
            call.RemoteStartTransaction(id_tag=id_tag, connector_id=connector_id)
        )
        return res.status

    async def remote_stop(self, transaction_id: int) -> str:
        res = await self._call(call.RemoteStopTransaction(transaction_id=transaction_id))
        return res.status

    async def set_current_limit(self, connector_id: int, amps: float) -> str:
        """Connector 0 caps the whole charger; 1 and 2 cap one outlet."""
        if connector_id == 0:
            profile_id = MAX_PROFILE_ID
            purpose = en.ChargingProfilePurposeType.charge_point_max_profile
        else:
            profile_id = TX_DEFAULT_PROFILE_ID_BASE + connector_id
            purpose = en.ChargingProfilePurposeType.tx_default_profile
        profile = dt.ChargingProfile(
            charging_profile_id=profile_id,
            stack_level=0,
            charging_profile_purpose=purpose,
            charging_profile_kind=en.ChargingProfileKindType.relative,
            charging_schedule=dt.ChargingSchedule(
                charging_rate_unit=en.ChargingRateUnitType.amps,
                charging_schedule_period=[dt.ChargingSchedulePeriod(start_period=0, limit=amps)],
            ),
        )
        res = await self._call(
            call.SetChargingProfile(connector_id=connector_id, cs_charging_profiles=profile)
        )
        if res.status == en.ChargingProfileStatus.accepted:
            if connector_id == 0:
                self.state.limit_a = amps
                self.hub.publish_charger(self.id)
            else:
                self.state.connector(connector_id).limit_a = amps
                self.hub.publish_connector(self.id, connector_id)
        return res.status

    async def clear_current_limit(self, connector_id: int) -> str:
        profile_id = (
            MAX_PROFILE_ID if connector_id == 0 else TX_DEFAULT_PROFILE_ID_BASE + connector_id
        )
        res = await self._call(call.ClearChargingProfile(id=profile_id))
        if res.status == en.ClearChargingProfileStatus.accepted:
            if connector_id == 0:
                self.state.limit_a = None
                self.hub.publish_charger(self.id)
            else:
                self.state.connector(connector_id).limit_a = None
                self.hub.publish_connector(self.id, connector_id)
        return res.status

    async def fetch_configuration(self) -> list[dict]:
        res = await self._call(call.GetConfiguration())
        keys = res.configuration_key or []
        await self.db.upsert_charge_point(self.id, configuration=json.dumps(keys))
        for k in keys:
            if k.get("key") == "SupportedFeatureProfiles":
                self.state.supported_profiles = [
                    p.strip() for p in (k.get("value") or "").split(",") if p.strip()
                ]
        self.hub.publish_charger(self.id)
        return keys

    async def change_configuration(self, key: str, value: str) -> str:
        res = await self._call(call.ChangeConfiguration(key=key, value=value))
        return res.status

    async def reset(self, hard: bool = False) -> str:
        res = await self._call(call.Reset(type=en.ResetType.hard if hard else en.ResetType.soft))
        return res.status

    async def unlock(self, connector_id: int) -> str:
        res = await self._call(call.UnlockConnector(connector_id=connector_id))
        return res.status

    # ------------------------------------------------------------------ helpers

    async def _authorize(self, id_tag: str) -> tuple[str, dict | None]:
        user = await self.db.user_for_id_tag(id_tag)
        if user and not user["blocked"]:
            return en.AuthorizationStatus.accepted, user
        if user and user["blocked"]:
            return en.AuthorizationStatus.blocked, None
        await self.db.record_unknown_tag(id_tag)
        log.warning("%s: unknown id tag %s", self.id, id_tag)
        if self.hub.settings.accept_unknown_id_tags:
            return en.AuthorizationStatus.accepted, None
        return en.AuthorizationStatus.invalid, None

    async def _touch(self) -> None:
        now = iso()
        self.state.last_seen = now
        await self.db.upsert_charge_point(self.id, last_seen=now)
