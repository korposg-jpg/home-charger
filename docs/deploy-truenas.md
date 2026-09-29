# Deploying on TrueNAS SCALE

Needs TrueNAS SCALE 24.10 (Electric Eel) or later, which runs apps on Docker.

## 1. Tailscale

1. In the [Tailscale admin console](https://login.tailscale.com/admin/dns), turn on
   **MagicDNS** and **HTTPS Certificates** (needed for the `https://…ts.net` address).
2. Create an auth key under **Settings → Keys** (one-off, pre-approved).
3. Install Tailscale on both phones and sign in to the same tailnet
   (your wife can be invited as a user, or share the device with her account).

## 2. Dataset

Create a dataset for the app's data, e.g. `tank/apps/home-charger`, owned by the
`apps` user (uid 568). Snapshots of this dataset are the backup: everything lives in
`csms/csms.db` and `tailscale/`.

## 3. Install the app

**Apps → Discover Apps → ⋮ → Install via YAML**, name it `home-charger`, and paste
`deploy/docker-compose.yml` with the variables filled in (TrueNAS's YAML editor
doesn't read a `.env` file), at minimum:

- `TS_AUTHKEY`: the key from step 1
- `DATA_DIR`: `/mnt/tank/apps/home-charger`

The images come from GHCR (`ghcr.io/korposg-jpg/home-charger-*`). If the repository is
private, either make the two container packages public on GitHub (they contain no
secrets), or add a GHCR pull credential in TrueNAS under **Apps → Configuration →
Manage Container Image Registries**.

Port 9000 is published on the server's LAN address for the charger. Don't forward it
on your router.

## 4. Create the two users

Open a shell in the `csms` container (**Apps → home-charger → Containers → Shell**):

```sh
csms add-user gabor --display-name Gabor --admin
csms add-user <wife> --display-name <Name>
csms list
```

## 5. Check it

- `https://home-charger.<tailnet>.ts.net/healthz` from a phone with Tailscale on
  should answer `{"ok": true, ...}`.
- To try it without the charger, add the simulator: in the YAML, remove the
  `profiles: [sim]` line from the `simulator` service, then use
  `http://<server>:9100/docs` to plug in a simulated car.
