# Home charger: architecture plan

Status: agreed 2026-09-29. Repo: `korposg-jpg/home-charger`. Home server: TrueNAS SCALE on an i7-9700, Home Assistant in a VM on it. Both phones will run Tailscale.

## 1. What the charger gives us

The charger is the **dé (Depow) DE-2 2×11 kW Dual Wallbox**.

| Item | What's known | Source |
|---|---|---|
| Protocol | OCPP 1.6J (JSON over WebSocket) | [product page](https://depow.de/products/de-2-11-kw-dual-wallbox) |
| Connectivity | Wi-Fi, Ethernet, Bluetooth | product page |
| Outlets | Two independently controlled Type 2 cables (7 m and 10 m), 11 kW each (3-phase 16 A) | product page |
| Load sharing | Built-in dynamic distribution when both cars charge, so the breaker doesn't trip | product page |
| Local auth | RFID (3 cards included), vendor app "CP Tool", or plug-and-charge | product page |
| OCPP setup | The OCPP server URL is entered in the CP Tool app or the wallbox's own web interface | [nord-domotique test of the 2×7.4 kW sibling](https://blog.nord-domotique.com/test-de-dual-wallbox-2x74-kw/) |
| Proven against | Home Assistant's OCPP integration on the LAN with plain `ws://192.168.x.x:9000`; after setup the charger reboots, then shows up as one charge point with connectors 1 and 2 | same article |

**Inferred, to be confirmed on the real unit** (Depow publishes no OCPP spec):

- It registers as **one charge point with connectorId 1 and 2** (connectorId 0 = the whole box).
- Whether it accepts `wss://` (TLS) and OCPP security profiles (Basic Auth / TLS). Cheap chargers often only do profile 0 (no auth, plain `ws://`). The design below doesn't depend on TLS.
- Which feature profiles it supports. The key one is **Smart Charging** (`SetChargingProfile`), which we need for per-car current limits and schedules. If it's missing, we fall back to `ChangeConfiguration` keys or simple start/stop scheduling done by our server.
- Whether the CP Tool app keeps working once the OCPP URL points to our server. It probably keeps working over Bluetooth, but that's a guess.
- MID-certified metering isn't mentioned, so treat kWh numbers as indicative, not billing-grade.

First job on install day: point the charger at our server and run a **capability probe** (a script that logs `BootNotification`, calls `GetConfiguration` with no keys to dump everything, and tries `SetChargingProfile` on each connector). That settles all of the above in about ten minutes.

## 2. Architecture overview

```
            Home LAN (charger on IoT VLAN / SSID)
 ┌───────────────┐   OCPP 1.6J ws://   ┌──────────────────────────────┐
 │ Depow DE-2    │ ──────────────────▶ │ Home server (Docker Compose)  │
 │ connector 1,2 │                     │  ├─ csms   (OCPP + REST API)  │
 └───────────────┘                     │  │   └─ SQLite on a dataset   │
                                       │  └─ tailscale (HTTPS for app) │
                                       └──────────────┬───────────────┘
                                                      │ HTTPS + WebSocket (app API)
                                   ┌──────────────────┴─────────────────┐
                                   │ Tailscale (home and away)           │
                                   └──────────────────┬─────────────────┘
                                                      ▼
                                     Android app (Gabor's + wife's phones)
                                     + push notifications via FCM
```

The charger only ever talks to the server on the LAN. The phones never talk to the charger directly; they talk to our server's app API. That keeps the charger's weak (likely unencrypted) OCPP link off the internet entirely.

## 3. Central system (CSMS): build a small one

I compared the realistic open-source options:

| Option | Fit | Why not / why |
|---|---|---|
| **SteVe** (Java, MySQL) | Good OCPP 1.6 coverage, Docker image, mature | Built for operators; web UI is admin-oriented and its REST API is thin, so the app would need a second backend anyway. Heavy for one box. |
| **Home Assistant OCPP integration** | Proven with this exact charger family | Ties the app to HA's API and auth model; fine if you already run HA, awkward otherwise. |
| **CitrineOS** | Modern, TypeScript | Focus is OCPP 2.0.1; 1.6 support is secondary. Overkill. |
| **Open e-Mobility** | Full platform | Very heavy (multi-tenant, billing, roaming); not a good fit for a home. |
| **Own small CSMS** on the [mobilityhouse `ocpp`](https://github.com/mobilityhouse/ocpp) Python library | Exactly what we need | ~1–2k lines: OCPP handlers + a REST/WebSocket API shaped for the app. Full control over quirks. |

**Recommendation: own small CSMS**, one container:

- **Python 3.12, FastAPI, `ocpp` library**, one process serving two things:
  - `ws://<server>:9000/ocpp/<chargePointId>` for the charger (LAN only, not exposed).
  - `/api/...` REST + `/api/live` WebSocket for the app.
- **Storage:** SQLite to start (one charger, two users). Postgres is a drop-in if it ever grows.
- **Handles:** BootNotification, Heartbeat, StatusNotification, Authorize, Start/StopTransaction, MeterValues, DataTransfer (logged), and sends RemoteStart/StopTransaction, SetChargingProfile / ClearChargingProfile, ChangeConfiguration, GetConfiguration, Reset, UnlockConnector, TriggerMessage.
- **Users and ID tags:** each person has an account and their own `idTag` (and can map their RFID card to it), so every session is attributed to Gabor or to your wife. Remote starts from the app use that person's `idTag`.
- **Scheduler:** server-side schedules (for example "charge connector 2 from 23:00 to 06:00" or "stop at X kWh"), implemented as charging profiles if the charger supports them, otherwise as timed RemoteStart/Stop.
- **Notifications:** Firebase Cloud Messaging push for "car plugged in", "charging started/finished", "fault", "charger offline".
- **Auth for the app:** per-user login issuing a long-lived refresh token plus short-lived access token. Only two accounts, created from a CLI command; no public sign-up.
- **Observability:** a raw OCPP message log (invaluable for this undocumented charger), plus a `/healthz`.

Home Assistant: the charger can only have one OCPP server, and ours takes that role, so HA's OCPP integration isn't used. Instead the CSMS will publish charger state to HA over MQTT (planned for a later milestone), so dashboards and automations still work.

## 4. Remote access

Recommendation: **Tailscale** (free for personal use).

- The server joins your tailnet; both phones run the Tailscale app. The app API is reachable at the server's tailnet name from anywhere, with **no ports opened on the router**.
- The app always uses the Tailscale address. At home Tailscale connects directly over the LAN, so there's no extra hop, and there's only one address to configure.
- A Tailscale sidecar container terminates HTTPS with a `*.ts.net` certificate (`tailscale serve`), so no Caddy and no tokens in clear text.
- Push notifications go through FCM, so they arrive even when Tailscale is off on the phone.

Alternative if your wife shouldn't have to run a VPN app: **Cloudflare Tunnel** to a public hostname, with Cloudflare Access in front. No open ports either, but it depends on a third party and puts the API on the internet (behind login). Port-forwarding plus Let's Encrypt is possible but is the option I'd avoid.

Charger-side hardening, regardless of the choice above:

- Put the charger on an IoT VLAN / guest SSID that can reach only the server's port 9000 (plus NTP, and the vendor cloud if you want firmware updates).
- Never expose port 9000 to the internet.
- If the probe shows it supports Basic Auth (security profile 1), turn it on.

## 5. Android app

| Layer | Choice |
|---|---|
| Language / UI | Kotlin, Jetpack Compose, Material 3 (dynamic colour) |
| Architecture | MVVM, single-activity, Navigation Compose, Hilt for DI |
| Networking | Retrofit + OkHttp (REST), OkHttp WebSocket for live status, kotlinx.serialization |
| Local state | DataStore (server URLs, tokens encrypted with Android Keystore); Room for offline session history cache |
| Background | WorkManager for periodic sync; FCM for push |
| Extras | Home-screen widget (Glance) and Quick Settings tile for one-tap start/stop per connector |
| Min SDK | 26 (Android 8), target latest |
| Distribution | Signed APK installed directly, or a private Play Console internal-testing track |

Screens for v1:

1. **Dashboard:** both connectors side by side, each with its state (available / plugged in / charging / finished / fault), live kW, session kWh, elapsed time, who started it, and Start/Stop.
2. **Current limit:** per-connector amps slider (6–16 A), if the charger supports smart charging.
3. **Schedules:** off-peak windows and "ready by" or kWh targets.
4. **History:** sessions per person and per month with kWh, and an optional €/kWh price for a cost estimate.
5. **Settings:** server addresses, notifications, RFID card to person mapping.

## 6. Developing before the charger is live

- A **dual-connector charger simulator** in the same repo, written on the same Python `ocpp` library: it boots, reports connectors 1 and 2, simulates plug-in, charging with realistic MeterValues, and responds to RemoteStart/Stop and SetChargingProfile. It runs as a Compose service, so the whole stack runs on a laptop or the home server.
- [MicroOcppSimulator](https://github.com/matth-x/MicroOcppSimulator) (Docker) is a second, independent client to cross-check our CSMS against a different implementation.
- On install day, run the capability probe, then adjust the simulator to mirror whatever the real charger does.

## 7. Repository and deployment

One monorepo, `korposg-jpg/home-charger`:

```
backend/      FastAPI + ocpp CSMS, Dockerfile, tests
simulator/    dual-connector charge point simulator
android/      Gradle project
deploy/       docker-compose.yml (csms + tailscale sidecar + optional simulator), .env.example
docs/         this plan, charger probe results
```

- GitHub Actions: backend tests and lint, simulator-driven integration test, Android build producing a signed APK artifact, and amd64 images pushed to GHCR.
- Deploy: a TrueNAS SCALE custom app (Install via YAML) using `deploy/docker-compose.yml`, data on a dataset covered by TrueNAS snapshots.

## 8. Milestones

1. **Backend skeleton and simulator:** charger connects, status and meter values stored, REST API to read state and remote start/stop. Tested entirely against the simulator.
2. **Android v1:** login, dashboard, start/stop, live updates over LAN.
3. **Remote and push:** Tailscale plus Caddy on the server, FCM notifications.
4. **Install day:** capability probe on the real DE-2, fix quirks, update the simulator.
5. **Smart features:** current limits, schedules, history and cost, widget and tile.

Milestones 1 and 2 fit before the grid connection; 4 slots in whenever the charger goes live.

## 9. Decisions

- Server: TrueNAS SCALE, x86 (i7-9700). Deployed as a custom app.
- Home Assistant runs in a VM; integrate via MQTT, not HA's OCPP integration.
- Remote access: Tailscale on both phones.
- Repo: `korposg-jpg/home-charger`, recommended private.
