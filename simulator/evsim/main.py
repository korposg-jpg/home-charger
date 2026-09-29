"""Runs the simulated charger: connects to the CSMS (reconnecting if it drops) and
serves a small HTTP control API for plugging cars in and out.

    python -m evsim --csms ws://localhost:9000/ocpp --control-port 9100

Control API (all POST unless noted):
    GET  /state
    /connectors/{n}/plug          /connectors/{n}/unplug
    /connectors/{n}/swipe?id_tag=CARD1
    /connectors/{n}/car-full      /connectors/{n}/car-max?amps=10
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os

import uvicorn
from fastapi import FastAPI, HTTPException
from websockets.asyncio.client import connect

from .station import SimStation, StationConfig

log = logging.getLogger("evsim")


class Runner:
    def __init__(self, csms_url: str, config: StationConfig):
        self.url = csms_url.rstrip("/") + "/" + config.charge_point_id
        self.config = config
        self.station: SimStation | None = None
        # Connector state survives reconnects, like a real charger's would.
        self._connectors = None

    async def run_forever(self) -> None:
        delay = 1.0
        while True:
            try:
                async with connect(self.url, subprotocols=["ocpp1.6"]) as ws:
                    log.info("connected to %s", self.url)
                    delay = 1.0
                    station = SimStation(self.config, ws)
                    if self._connectors is not None:
                        station.connectors = self._connectors
                    self._connectors = station.connectors
                    self.station = station
                    await station.run()
            except (OSError, Exception) as e:  # noqa: BLE001 - keep retrying like a charger
                log.warning("connection to %s lost: %s; retrying in %.0fs", self.url, e, delay)
            self.station = None
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)


def control_app(runner: Runner) -> FastAPI:
    app = FastAPI(title="DE-2 simulator control")

    def station() -> SimStation:
        if runner.station is None:
            raise HTTPException(503, "Simulator is not connected to the CSMS")
        return runner.station

    def check(n: int) -> None:
        if n not in (1, 2):
            raise HTTPException(404, "Connector must be 1 or 2")

    @app.get("/state")
    async def state():
        return station().snapshot()

    @app.post("/connectors/{n}/plug")
    async def plug(n: int):
        check(n)
        await station().plug(n)
        return station().snapshot()

    @app.post("/connectors/{n}/unplug")
    async def unplug(n: int):
        check(n)
        await station().unplug(n)
        return station().snapshot()

    @app.post("/connectors/{n}/swipe")
    async def swipe(n: int, id_tag: str):
        check(n)
        return {"result": await station().swipe(n, id_tag)}

    @app.post("/connectors/{n}/car-full")
    async def car_full(n: int, full: bool = True):
        check(n)
        await station().set_car_full(n, full)
        return station().snapshot()

    @app.post("/connectors/{n}/car-max")
    async def car_max(n: int, amps: float):
        check(n)
        station().connectors[n].car_max_a = amps
        return station().snapshot()

    return app


def main() -> None:
    p = argparse.ArgumentParser(prog="evsim")
    p.add_argument("--csms", default=os.environ.get("SIM_CSMS_URL", "ws://localhost:9000/ocpp"))
    p.add_argument("--id", default=os.environ.get("SIM_CHARGE_POINT_ID", "DE2-SIM"))
    p.add_argument("--supply-amps", type=float, default=float(os.environ.get("SIM_SUPPLY_A", 16)))
    p.add_argument(
        "--meter-interval", type=float, default=float(os.environ.get("SIM_METER_INTERVAL", 10))
    )
    p.add_argument("--time-scale", type=float, default=float(os.environ.get("SIM_TIME_SCALE", 1)))
    p.add_argument("--no-smart-charging", action="store_true")
    p.add_argument("--control-host", default=os.environ.get("SIM_CONTROL_HOST", "0.0.0.0"))
    p.add_argument(
        "--control-port", type=int, default=int(os.environ.get("SIM_CONTROL_PORT", 9100))
    )
    a = p.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    config = StationConfig(
        charge_point_id=a.id,
        supply_max_a=a.supply_amps,
        meter_interval_s=a.meter_interval,
        time_scale=a.time_scale,
        supports_smart_charging=not a.no_smart_charging,
    )
    runner = Runner(a.csms, config)

    async def amain() -> None:
        server = uvicorn.Server(
            uvicorn.Config(control_app(runner), host=a.control_host, port=a.control_port)
        )
        task = asyncio.create_task(runner.run_forever())
        try:
            await server.serve()
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    asyncio.run(amain())
