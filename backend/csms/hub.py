"""Live state of connected chargers, and an event bus the app's live feed listens on."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from .config import Settings
from .db import Database, iso

if TYPE_CHECKING:
    from .ocpp_handler import ChargePointSession


@dataclass
class ConnectorState:
    connector_id: int
    status: str = "Unknown"
    error_code: str = "NoError"
    info: str | None = None
    power_w: float | None = None
    # Latest energy register reading of the meter (absolute, Wh).
    meter_wh: float | None = None
    current_a: list[float] = field(default_factory=list)
    transaction_id: int | None = None
    transaction_started_at: str | None = None
    meter_start_wh: float | None = None
    user_id: int | None = None
    user_name: str | None = None
    # Current limit we last set successfully, in amps (None = charger default).
    limit_a: float | None = None
    updated_at: str | None = None

    def session_energy_wh(self) -> float | None:
        if self.transaction_id is None or self.meter_wh is None or self.meter_start_wh is None:
            return None
        return max(0.0, self.meter_wh - self.meter_start_wh)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["session_energy_wh"] = self.session_energy_wh()
        return d


@dataclass
class ChargerState:
    id: str
    online: bool = False
    vendor: str | None = None
    model: str | None = None
    firmware_version: str | None = None
    last_seen: str | None = None
    supported_profiles: list[str] = field(default_factory=list)
    # Total current cap across both connectors (ChargePointMaxProfile), if set.
    limit_a: float | None = None
    connectors: dict[int, ConnectorState] = field(default_factory=dict)

    def connector(self, connector_id: int) -> ConnectorState:
        if connector_id not in self.connectors:
            self.connectors[connector_id] = ConnectorState(connector_id)
        return self.connectors[connector_id]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "online": self.online,
            "vendor": self.vendor,
            "model": self.model,
            "firmware_version": self.firmware_version,
            "last_seen": self.last_seen,
            "supports_smart_charging": "SmartCharging" in self.supported_profiles,
            "supported_profiles": self.supported_profiles,
            "limit_a": self.limit_a,
            # Connector 0 is the charger as a whole; the app only shows the outlets.
            "connectors": [c.to_dict() for n, c in sorted(self.connectors.items()) if n != 0],
        }


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish(self, event: dict[str, Any]) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # A slow client just misses updates; it gets a full snapshot on reconnect.
                pass


class Hub:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.bus = EventBus()
        self.chargers: dict[str, ChargerState] = {}
        self.sessions: dict[str, ChargePointSession] = {}

    async def load(self) -> None:
        """Seed state from the database so the app shows something while offline."""
        for cp in await self.db.fetchall("SELECT * FROM charge_points"):
            state = self.charger(cp["id"])
            state.vendor, state.model = cp["vendor"], cp["model"]
            state.firmware_version, state.last_seen = cp["firmware_version"], cp["last_seen"]
        for c in await self.db.fetchall("SELECT * FROM connectors"):
            conn = self.charger(c["charge_point_id"]).connector(c["connector_id"])
            conn.status, conn.error_code = c["status"], c["error_code"]
            conn.info, conn.updated_at = c["info"], c["updated_at"]
        for cp_id in list(self.chargers):
            await self.restore_transactions(cp_id)

    async def restore_transactions(self, cp_id: str) -> None:
        state = self.charger(cp_id)
        for tx in await self.db.open_transactions(cp_id):
            conn = state.connector(tx["connector_id"])
            conn.transaction_id = tx["id"]
            conn.transaction_started_at = tx["started_at"]
            conn.meter_start_wh = tx["meter_start_wh"]
            conn.user_id = tx["user_id"]
            if tx["user_id"] is not None:
                user = await self.db.fetchone(
                    "SELECT display_name FROM users WHERE id = ?", (tx["user_id"],)
                )
                conn.user_name = user["display_name"] if user else None

    def charger(self, cp_id: str) -> ChargerState:
        if cp_id not in self.chargers:
            self.chargers[cp_id] = ChargerState(cp_id)
        return self.chargers[cp_id]

    def publish_connector(self, cp_id: str, connector_id: int) -> None:
        conn = self.charger(cp_id).connector(connector_id)
        conn.updated_at = iso()
        self.bus.publish({"type": "connector", "charger_id": cp_id, "connector": conn.to_dict()})

    def publish_charger(self, cp_id: str) -> None:
        self.bus.publish({"type": "charger", "charger": self.charger(cp_id).to_dict()})

    def session(self, cp_id: str) -> ChargePointSession | None:
        return self.sessions.get(cp_id)
