"""Test harness: a real CSMS server on a free port with the simulated DE-2 connected."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import time

import httpx
import pytest
import uvicorn
from evsim.station import SimStation, StationConfig
from websockets.asyncio.client import connect

from csms import auth
from csms.app import create_app
from csms.config import Settings


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def wait_for(predicate, timeout: float = 5.0, interval: float = 0.05):
    """Poll an (async or sync) predicate until it returns something truthy."""
    deadline = time.monotonic() + timeout
    while True:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return result
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(interval)


class Env:
    def __init__(self, base_url: str, ws_url: str, app, server):
        self.base_url = base_url
        self.ws_url = ws_url
        self.app = app
        self.server = server
        self.tokens: dict[str, str] = {}
        self.station: SimStation | None = None
        self._station_task: asyncio.Task | None = None

    @property
    def hub(self):
        return self.app.state.hub

    def client(self, user: str | None = None) -> httpx.AsyncClient:
        headers = {"Authorization": f"Bearer {self.tokens[user]}"} if user else {}
        return httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=10)

    async def connect_station(self, **config) -> SimStation:
        cfg = StationConfig(meter_interval_s=0.1, time_scale=600, **config)
        ready = asyncio.Event()

        async def run():
            async with connect(
                f"{self.ws_url}/ocpp/{cfg.charge_point_id}", subprotocols=["ocpp1.6"]
            ) as ws:
                self.station = SimStation(cfg, ws)
                ready.set()
                await self.station.run()

        self._station_task = asyncio.create_task(run())
        await asyncio.wait_for(ready.wait(), 5)
        # Surface a crash in the simulator instead of timing out on the boot wait.
        booted = asyncio.create_task(self.station.booted.wait())
        await asyncio.wait({booted, self._station_task}, timeout=5, return_when="FIRST_COMPLETED")
        if self._station_task.done():
            self._station_task.result()
        assert booted.done(), "simulator did not boot"

        # Wait until the CSMS has finished its post-boot probe too.
        await wait_for(
            lambda: (
                self.hub.chargers.get(cfg.charge_point_id)
                and self.hub.chargers[cfg.charge_point_id].supported_profiles
            )
        )
        return self.station

    async def disconnect_station(self) -> None:
        if self._station_task:
            self._station_task.cancel()
            with contextlib.suppress(BaseException):
                await self._station_task
            self._station_task = None


@pytest.fixture
async def env(tmp_path):
    port = free_port()
    settings = Settings(db_path=str(tmp_path / "csms.db"), port=port, ocpp_response_timeout=5)
    app = create_app(settings)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    )
    serve_task = asyncio.create_task(server.serve())
    await wait_for(lambda: server.started)

    e = Env(f"http://127.0.0.1:{port}", f"ws://127.0.0.1:{port}", app, server)
    db = app.state.hub.db
    await auth.create_user(db, "gabor", "Gabor", "pw-gabor", is_admin=True)
    await auth.create_user(db, "partner", "Partner", "pw-partner")
    async with e.client() as c:
        for user, pw in (("gabor", "pw-gabor"), ("partner", "pw-partner")):
            r = await c.post("/api/auth/login", json={"username": user, "password": pw})
            e.tokens[user] = r.json()["token"]
    try:
        yield e
    finally:
        await e.disconnect_station()
        server.should_exit = True
        await serve_task
