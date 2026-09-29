"""A simulated dual-connector OCPP 1.6J wallbox, modelled on the Depow DE-2.

It behaves like a real charger as far as the central system can tell: it boots,
reports both connectors, authorizes RFID swipes, accepts remote start/stop,
honours current limits from charging profiles, shares a supply between the two
outlets, and sends meter values while charging. Cars are plugged in, unplugged
and "filled up" through the methods at the bottom (or the control API)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ocpp.routing import after, on
from ocpp.v16 import ChargePoint, call, call_result
from ocpp.v16 import enums as en

log = logging.getLogger(__name__)

VOLTAGE = 230.0
PHASES = 3


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class SimConnector:
    connector_id: int
    status: str = "Available"
    error_code: str = "NoError"
    plugged: bool = False
    # The most the car will draw, per phase.
    car_max_a: float = 16.0
    car_full: bool = False
    meter_wh: float = 125_000.0
    transaction_id: int | None = None
    id_tag: str | None = None
    # A remote start that arrived before the car was plugged in.
    pending_id_tag: str | None = None
    # Limits from charging profiles, keyed by profile id.
    profile_limits: dict[int, float] = field(default_factory=dict)
    current_a: float = 0.0

    @property
    def profile_limit(self) -> float | None:
        return min(self.profile_limits.values()) if self.profile_limits else None


@dataclass
class StationConfig:
    charge_point_id: str = "DE2-SIM"
    vendor: str = "Depow"
    model: str = "DE-2 11kW SIM"
    # Max current per outlet (11 kW = 3 x 16 A).
    outlet_max_a: float = 16.0
    # Current shared by both outlets (the "dynamic load distribution" setting).
    # The real value depends on how the installer configures the box.
    supply_max_a: float = 16.0
    meter_interval_s: float = 10.0
    # Speeds up simulated time: 60 means one real second charges like one minute.
    time_scale: float = 1.0
    supports_smart_charging: bool = True


class SimStation(ChargePoint):
    def __init__(self, config: StationConfig, connection):
        super().__init__(config.charge_point_id, connection, response_timeout=15)
        self.config = config
        self.connectors = {n: SimConnector(n) for n in (1, 2)}
        self.station_limits: dict[int, float] = {}
        self.heartbeat_interval = 300
        self.ocpp_config: dict[str, str] = {
            "HeartbeatInterval": "300",
            "MeterValueSampleInterval": str(int(config.meter_interval_s)),
            "MeterValuesSampledData": "Energy.Active.Import.Register,Power.Active.Import,"
            "Current.Import",
            "NumberOfConnectors": "2",
            "AuthorizeRemoteTxRequests": "false",
            "ConnectionTimeOut": "120",
            "LocalPreAuthorize": "false",
            "SupportedFeatureProfiles": "Core,FirmwareManagement,LocalAuthListManagement,"
            "RemoteTrigger" + (",SmartCharging" if config.supports_smart_charging else ""),
            "ChargingScheduleAllowedChargingRateUnit": "Current",
            "ChargeProfileMaxStackLevel": "8",
        }
        self._tasks: list[asyncio.Task] = []
        self.booted = asyncio.Event()

    # ----------------------------------------------------------------- lifecycle

    async def run(self) -> None:
        """Receive loop plus boot, heartbeat and metering. Returns when the socket closes."""
        receiver = asyncio.create_task(self.start())
        try:
            await self.boot()
            self._tasks = [
                asyncio.create_task(self._heartbeat_loop()),
                asyncio.create_task(self._meter_loop()),
            ]
            await receiver
        finally:
            for t in [receiver, *self._tasks]:
                t.cancel()
            for t in [receiver, *self._tasks]:
                with contextlib.suppress(BaseException):
                    await t

    async def boot(self) -> None:
        res = await self.call(
            call.BootNotification(
                charge_point_vendor=self.config.vendor,
                charge_point_model=self.config.model,
                firmware_version="sim-0.1",
            )
        )
        self.heartbeat_interval = res.interval or 300
        for n in (0, 1, 2):
            await self.send_status(n)
        self.booted.set()

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            await self.call(call.Heartbeat())

    async def _meter_loop(self) -> None:
        interval = self.config.meter_interval_s
        while True:
            await asyncio.sleep(interval)
            self.recompute_currents()
            for c in self.connectors.values():
                if c.transaction_id is None:
                    continue
                c.meter_wh += (
                    c.current_a * VOLTAGE * PHASES * interval / 3600 * (self.config.time_scale)
                )
                await self.send_meter_values(c)

    # ------------------------------------------------------------------- physics

    def recompute_currents(self) -> None:
        """Split the available supply between outlets whose car wants current."""
        station_cap = self.config.supply_max_a
        if self.station_limits:
            station_cap = min(station_cap, min(self.station_limits.values()))
        drawing = [
            c
            for c in self.connectors.values()
            if c.transaction_id is not None and c.plugged and not c.car_full
        ]
        for c in self.connectors.values():
            if c not in drawing:
                c.current_a = 0.0
        if not drawing:
            return
        share = station_cap / len(drawing)
        for c in drawing:
            cap = min(self.config.outlet_max_a, c.car_max_a, share)
            if c.profile_limit is not None:
                cap = min(cap, c.profile_limit)
            # Below 6 A an EV can't charge (IEC 61851), so the outlet pauses.
            c.current_a = cap if cap >= 6 else 0.0

    def _charging_status(self, c: SimConnector) -> str:
        self.recompute_currents()
        if c.car_full:
            return "SuspendedEV"
        return "Charging" if c.current_a > 0 else "SuspendedEVSE"

    # ------------------------------------------------------------ OCPP messages

    async def send_status(self, connector_id: int) -> None:
        if connector_id == 0:
            status, error = "Available", "NoError"
        else:
            c = self.connectors[connector_id]
            status, error = c.status, c.error_code
        await self.call(
            call.StatusNotification(
                connector_id=connector_id, error_code=error, status=status, timestamp=now_iso()
            )
        )

    async def set_status(self, c: SimConnector, status: str) -> None:
        if c.status != status:
            c.status = status
            await self.send_status(c.connector_id)

    async def send_meter_values(self, c: SimConnector) -> None:
        sampled = [
            {
                "value": f"{c.meter_wh:.0f}",
                "measurand": "Energy.Active.Import.Register",
                "unit": "Wh",
                "context": "Sample.Periodic",
            },
            {
                "value": f"{c.current_a * VOLTAGE * PHASES:.0f}",
                "measurand": "Power.Active.Import",
                "unit": "W",
                "context": "Sample.Periodic",
            },
        ]
        for phase in ("L1", "L2", "L3"):
            sampled.append(
                {
                    "value": f"{c.current_a:.1f}",
                    "measurand": "Current.Import",
                    "unit": "A",
                    "phase": phase,
                    "context": "Sample.Periodic",
                }
            )
        await self.call(
            call.MeterValues(
                connector_id=c.connector_id,
                transaction_id=c.transaction_id,
                meter_value=[{"timestamp": now_iso(), "sampled_value": sampled}],
            )
        )

    async def start_transaction(self, c: SimConnector, id_tag: str) -> bool:
        await self.set_status(c, "Preparing")
        res = await self.call(
            call.StartTransaction(
                connector_id=c.connector_id,
                id_tag=id_tag,
                meter_start=int(c.meter_wh),
                timestamp=now_iso(),
            )
        )
        c.transaction_id = res.transaction_id
        c.id_tag = id_tag
        if res.id_tag_info["status"] != "Accepted":
            await self.stop_transaction(c, "DeAuthorized")
            return False
        await self.set_status(c, self._charging_status(c))
        return True

    async def stop_transaction(self, c: SimConnector, reason: str) -> None:
        if c.transaction_id is None:
            return
        tx_id, id_tag = c.transaction_id, c.id_tag
        c.transaction_id, c.id_tag, c.current_a = None, None, 0.0
        # A TxProfile only lives as long as its transaction.
        c.profile_limits = {k: v for k, v in c.profile_limits.items() if k >= 0}
        await self.call(
            call.StopTransaction(
                meter_stop=int(c.meter_wh),
                timestamp=now_iso(),
                transaction_id=tx_id,
                id_tag=id_tag,
                reason=reason,
            )
        )
        await self.set_status(c, "Finishing" if c.plugged else "Available")
        await self._rebalance()

    async def _rebalance(self) -> None:
        """After a change in limits or sessions, update statuses of charging outlets."""
        for c in self.connectors.values():
            if c.transaction_id is not None:
                await self.set_status(c, self._charging_status(c))

    # ------------------------------------------------------------ CSMS requests

    @on(en.Action.remote_start_transaction)
    async def on_remote_start(self, id_tag, connector_id=None, **kwargs):
        c = self.connectors.get(connector_id or 1)
        if c is None or c.transaction_id is not None or c.status in ("Faulted", "Unavailable"):
            return call_result.RemoteStartTransaction(status=en.RemoteStartStopStatus.rejected)
        c.pending_id_tag = id_tag
        return call_result.RemoteStartTransaction(status=en.RemoteStartStopStatus.accepted)

    @after(en.Action.remote_start_transaction)
    async def after_remote_start(self, id_tag, connector_id=None, **kwargs):
        c = self.connectors.get(connector_id or 1)
        if c and c.plugged and c.pending_id_tag == id_tag and c.transaction_id is None:
            c.pending_id_tag = None
            await self.start_transaction(c, id_tag)
        # Otherwise the transaction starts when the car is plugged in.

    @on(en.Action.remote_stop_transaction)
    async def on_remote_stop(self, transaction_id):
        for c in self.connectors.values():
            if c.transaction_id == transaction_id:
                return call_result.RemoteStopTransaction(status=en.RemoteStartStopStatus.accepted)
        return call_result.RemoteStopTransaction(status=en.RemoteStartStopStatus.rejected)

    @after(en.Action.remote_stop_transaction)
    async def after_remote_stop(self, transaction_id):
        for c in self.connectors.values():
            if c.transaction_id == transaction_id:
                await self.stop_transaction(c, "Remote")

    @on(en.Action.set_charging_profile)
    async def on_set_charging_profile(self, connector_id, cs_charging_profiles):
        if not self.config.supports_smart_charging:
            return call_result.SetChargingProfile(status=en.ChargingProfileStatus.not_supported)
        p = cs_charging_profiles
        schedule = p["charging_schedule"]
        if schedule["charging_rate_unit"] != "A":
            return call_result.SetChargingProfile(status=en.ChargingProfileStatus.rejected)
        limit = float(schedule["charging_schedule_period"][0]["limit"])
        pid, purpose = p["charging_profile_id"], p["charging_profile_purpose"]
        if purpose == "ChargePointMaxProfile":
            if connector_id != 0:
                return call_result.SetChargingProfile(status=en.ChargingProfileStatus.rejected)
            self.station_limits[pid] = limit
        elif connector_id == 0:
            for c in self.connectors.values():
                c.profile_limits[pid] = limit
        elif connector_id in self.connectors:
            c = self.connectors[connector_id]
            # Store TxProfiles under a negative key so they're dropped when the
            # transaction ends (see stop_transaction).
            c.profile_limits[-pid if purpose == "TxProfile" else pid] = limit
        else:
            return call_result.SetChargingProfile(status=en.ChargingProfileStatus.rejected)
        return call_result.SetChargingProfile(status=en.ChargingProfileStatus.accepted)

    @after(en.Action.set_charging_profile)
    async def after_set_charging_profile(self, **kwargs):
        await self._rebalance()

    @on(en.Action.clear_charging_profile)
    async def on_clear_charging_profile(self, id=None, **kwargs):
        found = False
        if id is None:
            found = bool(self.station_limits) or any(
                c.profile_limits for c in self.connectors.values()
            )
            self.station_limits.clear()
            for c in self.connectors.values():
                c.profile_limits.clear()
        else:
            found = self.station_limits.pop(id, None) is not None
            for c in self.connectors.values():
                found |= c.profile_limits.pop(id, None) is not None
                found |= c.profile_limits.pop(-id, None) is not None
        status = (
            en.ClearChargingProfileStatus.accepted
            if found
            else en.ClearChargingProfileStatus.unknown
        )
        return call_result.ClearChargingProfile(status=status)

    @after(en.Action.clear_charging_profile)
    async def after_clear_charging_profile(self, **kwargs):
        await self._rebalance()

    @on(en.Action.get_configuration)
    async def on_get_configuration(self, key=None):
        wanted = key or list(self.ocpp_config)
        known = [
            {"key": k, "readonly": False, "value": self.ocpp_config[k]}
            for k in wanted
            if k in self.ocpp_config
        ]
        unknown = [k for k in wanted if k not in self.ocpp_config]
        return call_result.GetConfiguration(configuration_key=known, unknown_key=unknown or None)

    @on(en.Action.change_configuration)
    async def on_change_configuration(self, key, value):
        if key not in self.ocpp_config:
            return call_result.ChangeConfiguration(status=en.ConfigurationStatus.not_supported)
        self.ocpp_config[key] = value
        if key == "MeterValueSampleInterval":
            self.config.meter_interval_s = max(1.0, float(value))
        return call_result.ChangeConfiguration(status=en.ConfigurationStatus.accepted)

    @on(en.Action.trigger_message)
    async def on_trigger_message(self, requested_message, connector_id=None):
        supported = {"StatusNotification", "Heartbeat", "MeterValues", "BootNotification"}
        status = (
            en.TriggerMessageStatus.accepted
            if requested_message in supported
            else en.TriggerMessageStatus.not_implemented
        )
        return call_result.TriggerMessage(status=status)

    @after(en.Action.trigger_message)
    async def after_trigger_message(self, requested_message, connector_id=None):
        if requested_message == "StatusNotification":
            for n in [connector_id] if connector_id is not None else [0, 1, 2]:
                if n == 0 or n in self.connectors:
                    await self.send_status(n)
        elif requested_message == "Heartbeat":
            await self.call(call.Heartbeat())
        elif requested_message == "BootNotification":
            await self.boot()
        elif requested_message == "MeterValues":
            for c in self.connectors.values():
                if c.transaction_id is not None and connector_id in (None, c.connector_id):
                    await self.send_meter_values(c)

    @on(en.Action.reset)
    async def on_reset(self, type):
        return call_result.Reset(status=en.ResetStatus.accepted)

    @after(en.Action.reset)
    async def after_reset(self, type):
        for c in self.connectors.values():
            await self.stop_transaction(c, "HardReset" if type == "Hard" else "SoftReset")
        await self.boot()

    @on(en.Action.unlock_connector)
    async def on_unlock_connector(self, connector_id):
        # The DE-2 has fixed cables, so there's nothing to unlock.
        return call_result.UnlockConnector(status=en.UnlockStatus.not_supported)

    @on(en.Action.data_transfer)
    async def on_data_transfer(self, vendor_id, **kwargs):
        return call_result.DataTransfer(status=en.DataTransferStatus.unknown_vendor_id)

    # ------------------------------------------------ things a person does to it

    async def plug(self, connector_id: int) -> None:
        c = self.connectors[connector_id]
        if c.plugged:
            return
        c.plugged, c.car_full = True, False
        await self.set_status(c, "Preparing")
        if c.pending_id_tag:
            id_tag, c.pending_id_tag = c.pending_id_tag, None
            await self.start_transaction(c, id_tag)

    async def unplug(self, connector_id: int) -> None:
        c = self.connectors[connector_id]
        if not c.plugged:
            return
        c.plugged = False
        if c.transaction_id is not None:
            await self.stop_transaction(c, "EVDisconnected")
        await self.set_status(c, "Available")

    async def swipe(self, connector_id: int, id_tag: str) -> str:
        """Present an RFID card. Starts a session, or stops the one this card started."""
        c = self.connectors[connector_id]
        if c.transaction_id is not None:
            if c.id_tag == id_tag:
                await self.stop_transaction(c, "Local")
                return "stopped"
            return "busy"
        res = await self.call(call.Authorize(id_tag=id_tag))
        if res.id_tag_info["status"] != "Accepted":
            return "rejected"
        if c.plugged:
            await self.start_transaction(c, id_tag)
            return "started"
        c.pending_id_tag = id_tag
        return "waiting_for_car"

    async def set_car_full(self, connector_id: int, full: bool = True) -> None:
        c = self.connectors[connector_id]
        c.car_full = full
        if c.transaction_id is not None:
            await self.set_status(c, self._charging_status(c))
            await self._rebalance()

    def snapshot(self) -> dict:
        self.recompute_currents()
        return {
            "id": self.config.charge_point_id,
            "station_limits": self.station_limits,
            "connectors": [
                {
                    "connector_id": c.connector_id,
                    "status": c.status,
                    "plugged": c.plugged,
                    "car_full": c.car_full,
                    "current_a": c.current_a,
                    "power_w": c.current_a * VOLTAGE * PHASES,
                    "meter_wh": round(c.meter_wh),
                    "transaction_id": c.transaction_id,
                    "id_tag": c.id_tag,
                    "profile_limit_a": c.profile_limit,
                }
                for c in self.connectors.values()
            ],
        }
