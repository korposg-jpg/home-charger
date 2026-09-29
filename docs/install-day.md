# Install day: connecting the real DE-2

Depow publishes no OCPP documentation, so the first connection doubles as a probe.
The CSMS logs every OCPP frame, which tells us what this charger really does.

## Before

- The CSMS is running on the server (see [deploy-truenas.md](deploy-truenas.md)).
- Ideally the charger gets its own IoT VLAN or SSID that can reach only the
  server's port 9000 (plus NTP, and the internet if you want Depow firmware updates).
- Give the charger a DHCP reservation so its address doesn't change.

## Point the charger at the server

In the **CP Tool** app (or the charger's web page), set the OCPP server URL to:

```
ws://<server LAN IP>:9000/ocpp
```

The charger adds its own id to the end. It reboots after saving, then connects.
(`ws://`, not `wss://`: the link stays inside the LAN.)

## Probe

As the admin user (API docs at `https://home-charger.<tailnet>.ts.net/docs`):

1. `GET /api/chargers`: the charger should be `online`, with connectors 1 and 2.
   Note its `id`, then set `CSMS_ALLOWED_CHARGE_POINTS` to it and redeploy.
2. `GET /api/chargers/{id}/configuration`: the full configuration dump. Check
   `SupportedFeatureProfiles` (is `SmartCharging` there?),
   `MeterValuesSampledData`, `MeterValueSampleInterval`, `AuthorizeRemoteTxRequests`,
   and whether there's an `AuthorizationKey` (Basic auth support).
3. Plug a car into outlet 1 and start from the API; stop it; repeat on outlet 2.
4. If smart charging is supported, `PUT …/connectors/1/limit {"amps": 8}` during a
   session and watch the power drop.
5. Swipe each RFID card once. They'll show up in `GET /api/id-tags/unknown`; assign
   them with `csms add-tag <user> <tag>`.
6. Save `GET /api/ocpp-log?limit=5000` and share it, so the simulator can be adjusted
   to match the real charger's behaviour.

## Worth trying

- `MeterValueSampleInterval` 10–30 s gives a responsive app without much traffic.
- If the charger supports Basic auth: set `CSMS_OCPP_BASIC_AUTH_PASSWORD`, then set
  the charger's `AuthorizationKey` to the same password.
