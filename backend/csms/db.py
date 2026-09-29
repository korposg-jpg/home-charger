"""SQLite persistence. One connection, shared by the whole process."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);

-- An id tag is what the charger sees: an RFID card uid, or the tag the app uses
-- for remote starts. Every tag belongs to a user so sessions can be attributed.
CREATE TABLE IF NOT EXISTS id_tags (
    id_tag     TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    label      TEXT NOT NULL DEFAULT '',
    is_app_tag INTEGER NOT NULL DEFAULT 0,
    blocked    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS api_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    last_used  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS charge_points (
    id               TEXT PRIMARY KEY,
    vendor           TEXT,
    model            TEXT,
    serial_number    TEXT,
    firmware_version TEXT,
    last_boot        TEXT,
    last_seen        TEXT,
    -- JSON dump of the last GetConfiguration answer.
    configuration    TEXT
);

CREATE TABLE IF NOT EXISTS connectors (
    charge_point_id TEXT NOT NULL,
    connector_id    INTEGER NOT NULL,
    status          TEXT NOT NULL,
    error_code      TEXT NOT NULL DEFAULT 'NoError',
    info            TEXT,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (charge_point_id, connector_id)
);

CREATE TABLE IF NOT EXISTS transactions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    charge_point_id TEXT NOT NULL,
    connector_id    INTEGER NOT NULL,
    id_tag          TEXT NOT NULL,
    user_id         INTEGER REFERENCES users(id) ON DELETE SET NULL,
    meter_start_wh  INTEGER NOT NULL,
    meter_stop_wh   INTEGER,
    started_at      TEXT NOT NULL,
    stopped_at      TEXT,
    stop_reason     TEXT
);
CREATE INDEX IF NOT EXISTS ix_tx_started ON transactions(started_at);

CREATE TABLE IF NOT EXISTS meter_samples (
    transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    ts             TEXT NOT NULL,
    energy_wh      REAL,
    power_w        REAL
);
CREATE INDEX IF NOT EXISTS ix_samples_tx ON meter_samples(transaction_id, ts);

-- Raw OCPP traffic. The DE-2 has no public OCPP documentation, so this log is how
-- we learn what it actually sends.
CREATE TABLE IF NOT EXISTS ocpp_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    charge_point_id TEXT NOT NULL,
    direction       TEXT NOT NULL,
    message         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ocpp_log_ts ON ocpp_log(ts);

-- id tags the charger presented that we didn't recognise (new RFID cards).
CREATE TABLE IF NOT EXISTS unknown_id_tags (
    id_tag     TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    count      INTEGER NOT NULL DEFAULT 1
);
"""


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).isoformat(timespec="seconds").replace("+00:00", "Z")


class Database:
    def __init__(self, path: str):
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if self.path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "database not connected"
        return self._conn

    async def execute(self, sql: str, params: tuple | dict = ()) -> int:
        cur = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cur.lastrowid

    async def fetchone(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        async with self.conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def fetchall(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ---- users and tags -------------------------------------------------

    async def user_for_id_tag(self, id_tag: str) -> dict[str, Any] | None:
        return await self.fetchone(
            "SELECT u.id, u.username, u.display_name, t.blocked FROM id_tags t "
            "JOIN users u ON u.id = t.user_id WHERE t.id_tag = ?",
            (id_tag,),
        )

    async def app_tag_for_user(self, user_id: int) -> str | None:
        row = await self.fetchone(
            "SELECT id_tag FROM id_tags WHERE user_id = ? AND is_app_tag = 1 AND blocked = 0",
            (user_id,),
        )
        return row["id_tag"] if row else None

    async def record_unknown_tag(self, id_tag: str) -> None:
        now = iso()
        await self.execute(
            "INSERT INTO unknown_id_tags (id_tag, first_seen, last_seen) VALUES (?, ?, ?) "
            "ON CONFLICT(id_tag) DO UPDATE SET last_seen = excluded.last_seen, count = count + 1",
            (id_tag, now, now),
        )

    # ---- charge points --------------------------------------------------

    async def upsert_charge_point(self, cp_id: str, **fields: Any) -> None:
        await self.execute(
            "INSERT INTO charge_points (id) VALUES (?) ON CONFLICT(id) DO NOTHING", (cp_id,)
        )
        if fields:
            cols = ", ".join(f"{k} = ?" for k in fields)
            await self.execute(
                f"UPDATE charge_points SET {cols} WHERE id = ?", (*fields.values(), cp_id)
            )

    async def upsert_connector(
        self, cp_id: str, connector_id: int, status: str, error_code: str, info: str | None
    ) -> None:
        await self.execute(
            "INSERT INTO connectors (charge_point_id, connector_id, status, error_code, info, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(charge_point_id, connector_id) "
            "DO UPDATE SET status = excluded.status, error_code = excluded.error_code, "
            "info = excluded.info, updated_at = excluded.updated_at",
            (cp_id, connector_id, status, error_code, info, iso()),
        )

    # ---- transactions ---------------------------------------------------

    async def start_transaction(
        self,
        cp_id: str,
        connector_id: int,
        id_tag: str,
        user_id: int | None,
        meter_start_wh: int,
        started_at: str,
    ) -> int:
        return await self.execute(
            "INSERT INTO transactions (charge_point_id, connector_id, id_tag, user_id, "
            "meter_start_wh, started_at) VALUES (?, ?, ?, ?, ?, ?)",
            (cp_id, connector_id, id_tag, user_id, meter_start_wh, started_at),
        )

    async def stop_transaction(
        self, tx_id: int, meter_stop_wh: int, stopped_at: str, reason: str | None
    ) -> None:
        await self.execute(
            "UPDATE transactions SET meter_stop_wh = ?, stopped_at = ?, stop_reason = ? "
            "WHERE id = ? AND stopped_at IS NULL",
            (meter_stop_wh, stopped_at, reason, tx_id),
        )

    async def open_transactions(self, cp_id: str) -> list[dict[str, Any]]:
        return await self.fetchall(
            "SELECT * FROM transactions WHERE charge_point_id = ? AND stopped_at IS NULL",
            (cp_id,),
        )

    async def add_meter_sample(
        self, tx_id: int, ts: str, energy_wh: float | None, power_w: float | None
    ) -> None:
        await self.execute(
            "INSERT INTO meter_samples (transaction_id, ts, energy_wh, power_w) "
            "VALUES (?, ?, ?, ?)",
            (tx_id, ts, energy_wh, power_w),
        )

    # ---- OCPP log -------------------------------------------------------

    async def log_ocpp(self, cp_id: str, direction: str, message: str) -> None:
        await self.conn.execute(
            "INSERT INTO ocpp_log (ts, charge_point_id, direction, message) VALUES (?, ?, ?, ?)",
            (iso(), cp_id, direction, message),
        )
        await self.conn.commit()

    async def prune(self, ocpp_log_days: int, token_ttl_days: int) -> None:
        await self.execute(
            "DELETE FROM ocpp_log WHERE ts < ?", (iso(utcnow() - timedelta(days=ocpp_log_days)),)
        )
        await self.execute(
            "DELETE FROM api_tokens WHERE last_used < ?",
            (iso(utcnow() - timedelta(days=token_ttl_days)),),
        )
