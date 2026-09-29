# Home charger

Control a Depow DE-2 2×11 kW dual wallbox from Android phones, at home and away,
through a small self-hosted OCPP 1.6J central system.

```
Depow DE-2 ──OCPP 1.6J (LAN)──▶ csms (this repo, Docker on the home server) ◀──HTTPS over Tailscale── Android app
```

| Folder | What |
|---|---|
| `backend/` | `csms`: OCPP 1.6J central system + REST/WebSocket API for the app (Python, FastAPI, [`ocpp`](https://github.com/mobilityhouse/ocpp), SQLite) |
| `simulator/` | `evsim`: a simulated dual-connector DE-2, for developing before (and alongside) the real charger |
| `deploy/` | Docker Compose for TrueNAS SCALE: csms + Tailscale sidecar (+ simulator profile) |
| `docs/` | [Architecture plan](docs/architecture-plan.md), [TrueNAS deployment](docs/deploy-truenas.md), [install-day checklist](docs/install-day.md), [API](docs/api.md) |
| `android/` | The app (coming next) |

## Run it locally

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e "backend[dev]" -e simulator

csms add-user gabor --display-name Gabor --admin     # prompts for a password
csms serve                                           # OCPP + API on :9000

# in another terminal: a simulated charger with a control API on :9100
evsim --csms ws://localhost:9000/ocpp --time-scale 60
```

Then open <http://localhost:9100/docs> to plug in a car or swipe a card, and
<http://localhost:9000/docs> for the app API (log in with `/api/auth/login`, then
use the token as `Authorization: Bearer ...`).

## Tests

```sh
cd backend && pytest     # end-to-end: CSMS against the simulator over real WebSockets
ruff check . && ruff format --check .
```
