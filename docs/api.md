# App API

All endpoints except login need `Authorization: Bearer <token>`. Base URL:
`https://home-charger.<tailnet>.ts.net`. Interactive docs at `/docs`.

| Method | Path | Notes |
|---|---|---|
| POST | `/api/auth/login` | `{username, password}` → `{token, user}` |
| POST | `/api/auth/logout` | Revokes the token |
| GET | `/api/me` | User, plus their id tags (app tag, RFID cards) |
| GET | `/api/chargers` | Every charger with live connector state |
| GET | `/api/chargers/{id}` | One charger |
| POST | `/api/chargers/{id}/connectors/{n}/start` | Remote start with the caller's app tag. If no car is plugged in yet, it starts when one is |
| POST | `/api/chargers/{id}/connectors/{n}/stop` | Remote stop of the running session |
| PUT | `/api/chargers/{id}/connectors/{n}/limit` | `{amps}`: 0 pauses, else 6–32. `n = 0` caps the whole charger |
| DELETE | `/api/chargers/{id}/connectors/{n}/limit` | Remove the limit |
| GET | `/api/sessions?limit&before_id&user_id` | Charging history, newest first, with `energy_wh` and `user_name` |
| GET | `/api/sessions/{id}` | One session with its meter samples |
| WS | `/api/live` | Same bearer header. Sends `snapshot`, then `charger` / `connector` events, and `ping` every 30 s |

Admin only: `GET /api/chargers/{id}/configuration[?refresh=true]`,
`PUT /api/chargers/{id}/configuration/{key}`, `POST /api/chargers/{id}/reset[?hard=true]`,
`GET /api/id-tags/unknown`, `GET /api/ocpp-log`.

Connector state fields: `status` (OCPP: Available, Preparing, Charging, SuspendedEV,
SuspendedEVSE, Finishing, Faulted, Unavailable), `power_w`, `current_a` (per phase),
`session_energy_wh`, `transaction_id`, `user_name`, `limit_a`.

Errors: 401 not logged in, 403 admin only, 404 unknown charger/connector,
409 the charger refused (the message says why), 502 the charger returned an error or
didn't answer, 503 the charger is offline.
