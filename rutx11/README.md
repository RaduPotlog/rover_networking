<p align="center">
  <img src="icons/Logo-Arm-WhiteOrange-372x372-1.png" alt="Mechatronics Academy" width="140">
</p>

# rover_networking / rutx11

Everything about the Rover A1's **Teltonika RUTX11** router:

- **`icons/`**: the Mechatronics Academy logos, used by the web page.
- **The network page** (`uplink_manager/`): a small web app that switches the router's Wi-Fi
  uplink to another network **without breaking the firewall/NAT rules**. It runs on the rover
  (container `rover-a1-network` in [rover_docker](https://github.com/RaduPotlog/rover_docker))
  or on any laptop connected to the rover.

Contents:

- [The router](#the-router)
- [Why the network page exists](#why-the-network-page-exists)
- [Using the page](#using-the-page)
- [What the health check verifies](#what-the-health-check-verifies)
- [How a change is applied (and rolled back)](#how-a-change-is-applied-and-rolled-back)
- [Tested on the rover](#tested-on-the-rover)
- [Run it from a laptop](#run-it-from-a-laptop)
- [Run it on the rover](#run-it-on-the-rover)
- [Configuration](#configuration)
- [HTTP API](#http-api)
- [Security](#security)
- [Development](#development)
- [Troubleshooting](#troubleshooting)
- [Limitations](#limitations)

## The router

| | |
|---|---|
| Model / firmware | Teltonika RUTX11, RutOS **7.25.2** (OpenWrt based: `uci`, fw3/iptables, `ubus`, mwan3) |
| Management | Web UI `https://192.168.1.1`, SSH `root@192.168.1.1` (password auth) |
| Internet | **Only** the Wi-Fi client (STA) uplink. There is no SIM and no wired WAN |

**Networks** (uci names):

| Network | Zone | Subnet | What is on it |
|---------|------|--------|---------------|
| `lan` (br-lan, wired) | `lan` | 192.168.1.0/24, router .1 | ROS controller **.201 and .102** (same MAC); RS16 lidar .200 (sends UDP to .102); dev laptop Ethernet .184 (static). DHCP pool .110–.249. **.102 must never be handed to another device.** |
| `WWAN` (the rover's own AP "ROVER-A1-001-2.4GHz", radio0) | `lan` | 192.168.77.0/24, router .1 | BMS .201, front LED .202, laptops. DHCP pool .100–.149 |
| Wi-Fi uplink (STA) | `wwan` (masquerade) | from the site's DHCP | Uplink to the site network, e.g. "Tekwill" / "Orange-Tekwill". `WWAN1` in the 2026-09-28 backup; a later RutOS join made it `wan1`. The page detects which one it is. |

The GPS module's NMEA is forwarded over UDP to `192.168.1.201:10110`. It is configured by
`rover_sensors/rover_gps/scripts/rutx11_gps_nmea_forwarding.sh`.

The firewall has two zones:
- `lan` = `lan WWAN`, with input/forward ACCEPT and **masq off**.
- `wwan` = the uplink, with input/forward REJECT, **masq on** and mtu_fix.

A `lan → wwan` forwarding gives both client networks internet. Failover (mwan3) is in `mwan`
mode, but its interfaces are disabled.

RutOS pitfalls this repo has run into:

- **Zone `network` option.** RutOS keeps a zone's `network` as **one space-separated option**,
  not a list, so `uci add_list` / `uci del_list` on it silently do nothing. Always write the
  whole value: `uci set firewall.6.network='wan1'`.
- **Logical names.** A zone must list the **logical (uci) name** of an interface, not the name
  RutOS shows in the UI.
- **Missing lidar in ARP.** The router's ARP table never shows the lidar, because it talks to
  the controller through the switch. That is not a sign the lidar is down.
- **No helper tools.** The router has no `sshpass` or `diff`. Drive it with SSH from another
  machine (this app uses paramiko).

## Why the network page exists

Joining a new Wi-Fi network in RutOS (*Network → Wireless → Scan → Join*) **creates a new
interface** (`wan1`, `wan2`, …) instead of reusing the existing uplink. Nothing else is updated:

- The `wwan` zone doesn't list the new interface, so there is **no MASQUERADE**. The router
  itself is online, but **every LAN and AP client loses internet**.
- The new interface can land in the `lan` zone. That exposes the router's SSH and web UI to
  the foreign network.
- Forwardings and failover entries keep pointing at the old interface.

The network page never creates an interface. It **rewrites the SSID, password and encryption of
the existing uplink in place**, so every zone, forwarding and NAT rule that references it stays
valid. It also repairs a router that a RutOS join has already broken.

## Using the page

Open it (see [laptop](#run-it-from-a-laptop) / [rover](#run-it-on-the-rover)). There are three
cards:

- **Internet uplink.** It shows:
  - which network the router is associated to, the configured SSID if it differs, signal and
    channel
  - the address and gateway
  - the uplink network name ("auto-detected")
  - whether the **router** reaches the internet
  - whether **LAN / AP clients** do, meaning NAT is active on the uplink device.

  *Online* means all of these are fine.
- **Connect to a Wi-Fi network.**
  1. Press **Scan**; it scans both radios, 2.4 and 5 GHz. A network available on both bands
     gets one row per band.
  2. Click a network and type its password.
  3. Press **Connect**.

  **Hidden network…** lets you type an SSID and pick its security by hand. A job log shows each
  step; see [How a change is applied](#how-a-change-is-applied-and-rolled-back).
- **Health check.** Every rule from the next section, as ✓ / ! / ✗.
  - **Repair** applies all automatic fixes. *Commands Repair will run* shows the exact `uci`
    commands first, with passwords masked.
  - A **second Wi-Fi client** (typically left by a RutOS join) gets two buttons:
    - **Use '<SSID>' as the uplink** moves its SSID and password into the uplink, then deletes
      it together with its network, zone and failover entries.
    - **Delete this extra interface** removes it and keeps the current uplink.

If someone has unsaved changes in the RutOS web UI (`uci changes` is not empty), the page shows
them and refuses to switch until they are saved or discarded there. Otherwise it would commit
them along with its own change.

## What the health check verifies

The uplink is found automatically (`UPLINK_IFACE=auto`): the Wi-Fi client that is enabled and
already in the `wwan` zone, else the enabled one, else the one in the zone. The other checks
follow from it.

| Check | Healthy when | Repair |
|-------|--------------|--------|
| Wi-Fi uplink interface | exactly one STA, enabled | enables it; a second STA → Adopt/Delete buttons |
| Radio of the uplink | not disabled (e.g. after moving to 5 GHz) | enables it |
| Zone `wwan`: interfaces | lists the uplink, and nothing that doesn't exist or belongs to the LAN | rewrites the option |
| Zone `wwan`: NAT | `masq=1`, `mtu_fix=1`, input not ACCEPT | sets them |
| Zone `lan`: interfaces | exactly `lan WWAN`, never the uplink | rewrites the option |
| Zone `lan`: no NAT | `masq=0` | sets it |
| Other zones | contain neither the uplink nor the client networks | removes them |
| Forwarding `lan → wwan` | present and enabled | adds/enables it |
| Stale forwarding | none point at a missing zone | deletes them |
| Firewall rules / port forwards | none refer to a missing zone | *warning only*, fix in RutOS |
| Failover (mwan3) | members/interfaces exist | *warning only*: RutOS ships entries for `wan`/`mob1s*a1`, which this router lacks |
| DHCP on `lan` / `WWAN` | an active pool | *warning only* |
| Live | associated, IPv4 address, router pings 1.1.1.1/8.8.8.8 **through the uplink**, MASQUERADE on the uplink device | (switch or repair) |

## How a change is applied (and rolled back)

Every switch, repair, adopt or delete is **one transaction**:

1. **Refuse** if RutOS has uncommitted changes, or if another job is running.
2. **Back up** `wireless network firewall mwan3` to `/etc/netui-backup/<timestamp>/` on the
   router. The last 5 are kept.
3. **Arm a rollback on the router itself**: a detached timer (`trap '' HUP` subshell) that
   restores the backup after **180 s**. It fires even if the laptop, the controller or this
   service dies mid-switch.
4. **Apply**:
   - `uci batch` (the Wi-Fi password goes only through its stdin), then `uci commit`.
   - Reload whatever changed: network → wifi → firewall → mwan3. The reloads run detached, so a
     dropped SSH session can't interrupt them.
5. **Verify.** For a Wi-Fi change, the router must associate, get an IPv4 address, ping out
   through the uplink and have MASQUERADE, within **75 s**. For a pure firewall repair when the
   uplink was already down, a consistent config and a reachable router are enough.
6. **Confirm**, which disarms the timer. **Or roll back** straight away:
   - A wrong password, an out-of-range network, or no DHCP brings back the previous network.
   - The log says why.

The rover's own LAN and AP are never modified by a switch, so the page stays reachable.

Files on the router:

| Path | What |
|------|------|
| `/etc/netui-backup/<ts>/` | config copy per transaction (flash, survives reboot) |
| `/tmp/netui-rollback-<ts>.sh` / `.log` | the rollback script and what it restored |
| `/tmp/netui-done-<ts>/` | "decided" flag: confirm and rollback race for it with `mkdir` |

To roll back by hand:

```sh
ssh root@192.168.1.1
ls /etc/netui-backup/                         # pick a timestamp
TS=20260929-101500
for p in wireless network firewall mwan3; do
  [ -f /etc/netui-backup/$TS/$p ] && cp /etc/netui-backup/$TS/$p /etc/config/$p
done
/etc/init.d/network reload; wifi reload; /etc/init.d/firewall reload
```

## Tested on the rover

The tests ran on 2026-09-29 against the real router and were driven from the ROS controller
(wired). The uplink was auto-detected as `wan1` (section `wireless.3`, radio0).

| Test | Outcome | Duration | AP (rover Wi-Fi) |
|------|---------|----------|------------------|
| Read-only audit + scan | all checks green; 33 networks, merged to one row per SSID | 2.6 s scan | untouched |
| Switch to Tekwill with a **wrong password** | not associated after 75 s → rolled back to Orange-Tekwill, healthy 10 s later | ~90 s | ~1 s blink per failed retry (every ~25 s) |
| Switch to Tekwill (WPA2) | associated, DHCP, internet + NAT verified, kept | 18 s | a few 1–5 s blinks |
| Back to Orange-Tekwill (open) | key removed, verified, kept | 16 s | a few 1–5 s blinks |

Every switch changed only `wireless.3` (`ssid` / `encryption` / `key`). The interface, zones and
NAT were never touched, and all router-side rollback timers stood down afterwards.

## Run it from a laptop

The laptop has to reach the router over SSH. Either:

- plug into the **rover LAN** (Ethernet), or
- join the **rover AP "ROVER-A1-001-2.4GHz"**.

`192.168.1.1` answers from both. From the uplink side, the router rejects SSH by design.
Switching the uplink never drops a laptop that is on the rover LAN or AP.

**Linux / macOS / WSL**:

```bash
git clone https://github.com/RaduPotlog/rover_networking.git && cd rover_networking/rutx11
./run-local.sh --open
```

`run-local.sh` does the following:
1. Creates `.venv`, with `uv` if it is installed, otherwise `python3 -m venv` (on Ubuntu:
   `sudo apt install python3-venv`).
2. Installs the app in editable mode.
3. **Asks for the router's root password.** It is never a command-line argument, so it stays
   out of shell history.
4. Serves `http://localhost:5080` with **no login**. That is allowed only on loopback.

To skip the prompt, export `RUTX11_PASSWORD` first. Other examples:

```bash
./run-local.sh --demo --open            # no router needed: a simulated RUTX11 (see below)
./run-local.sh --router 192.168.77.1    # any router address works
./run-local.sh --port 8080
```

**Windows (PowerShell)**, with Python 3.10+ from python.org:

```powershell
git clone https://github.com/RaduPotlog/rover_networking.git; cd rover_networking\rutx11
py -m venv .venv
.venv\Scripts\pip install -e .
.venv\Scripts\rover-netui --open
```

WSL reaches the router only while **Windows** is connected to the rover LAN or AP.

**Demo mode.** `--demo` needs no router. By default it simulates the state the router was in
on the evening of 2026-09-28:
- the RutOS-created `wan1` is in the `lan` zone and not NATed
- the old `WWAN1` client still exists

Click **Repair**, then **Delete** or **Use as the uplink**, to watch it get fixed. `--demo PATH`
loads any `uci -X show wireless network firewall mwan3 dhcp` dump. In the demo, the
networks that "accept" a password are the ones in `uplink_manager/infrastructure/demo_router.py`
(e.g. `Cafe` / `password1`, `Guest` open).

**Letting others on the LAN use your laptop's instance.** Serve on all interfaces with a
login; without `NETUI_PASSWORD` it refuses to start:

```bash
NETUI_PASSWORD='choose-one' ./run-local.sh --bind 0.0.0.0
```

**One instance at a time.** Don't run it on a laptop and on the rover at once, and don't run
it while someone is editing the router in the RutOS web UI. Each instance
serializes its own jobs, but two instances switching the router together would fight. The
router-side rollback keeps the router safe either way.

## Run it on the rover

[rover_docker](https://github.com/RaduPotlog/rover_docker) builds the service
`rover-a1-network` from this folder. Its Dockerfile clones `master` (pin a commit with
`--build-arg ROVER_NETWORKING_REF=<sha>`) and runs the tests during the build, so a failing test
fails the push. Deploy with `balena push … --nocache`, then open
`http://<rover-lan-ip>:5080/` and log in as `rover` with `NETUI_PASSWORD`.

Set both passwords as **service** variables, so no other container sees the router's root
password:

```bash
balena env set RUTX11_PASSWORD '<router root password>' --device <uuid> --service rover-a1-network
balena env set NETUI_PASSWORD  '<page password>'        --device <uuid> --service rover-a1-network
```

Without either one, the container idles and logs why, instead of crash-looping. The router's
host key and the job log persist in the volume `rover-network` (`/data`).

## Configuration

Every option can come from the command line or the environment. The container uses the
environment.

| Option | Env | Default | Meaning |
|--------|-----|---------|---------|
| `--router` | `RUTX11_HOST` | `192.168.1.1` | Router address. |
| `--user` | `RUTX11_USER` | `root` | Router SSH user. |
| – | `RUTX11_PASSWORD` | *(prompt)* | Router SSH password; prompted in a terminal when unset. |
| `--bind` | `NETUI_BIND` | `127.0.0.1` (container: `0.0.0.0`) | Address to serve on; non-loopback requires `NETUI_PASSWORD`. |
| `--port` | `NETUI_PORT` | `5080` | Port. |
| – | `NETUI_USER` / `NETUI_PASSWORD` | `rover` / *(none)* | Page login (basic auth). No password = no login, loopback only. |
| `--data-dir` | `NETUI_DATA` | `~/.local/state/rover-netui` (container: `/data`) | `known_hosts` (router key) and `jobs.log`. |
| `--icons` | `NETUI_ICONS` | `<repo>/rutx11/icons` | Logo folder. |
| `--uplink` | `UPLINK_IFACE` | `auto` | uci network of the Wi-Fi uplink, or `auto`. |
| `--uplink-zone` | `UPLINK_ZONE` | `wwan` | Zone that NATs the uplink. |
| `--lan-zone` | `LAN_ZONE` | `lan` | Zone of the client networks. |
| `--client-nets` | `CLIENT_NETS` | `lan WWAN` | Networks that must reach the internet. |
| `--open` | – | off | Open the browser. |
| `--demo [PATH]` | – | off | Simulated router (see above). |
| – | `ROVER_NETWORK_ENABLE` | `true` | Container only: `false` idles it. |

## HTTP API

The page uses the API below. All endpoints except `/healthz` and `/icons/*` need the login when
one is set. POSTs also need the header `X-Requested-With: netui`.

| Method + path | Does |
|---------------|------|
| `GET /healthz` | liveness (doesn't touch the router) |
| `GET /api/status` | configured vs live uplink, `healthy`, resolved settings |
| `GET /api/scan` | networks seen by both radios (one row per SSID, security and band) |
| `GET /api/audit` | checks, the repair commands (masked), pending RutOS changes |
| `POST /api/switch` `{ssid, encryption, key?, radio?}` | switch the uplink → `{job}` |
| `POST /api/repair` | apply the automatic fixes → `{job}` |
| `POST /api/action` `{id: "adopt:<section>" \| "delete:<section>"}` | handle a second STA → `{job}` |
| `GET /api/jobs`, `GET /api/jobs/<id>` | job list / one job's step log |

`encryption` is an OpenWrt value: `psk2`, `psk-mixed`, `psk`, `sae`, `sae-mixed`, `none`, `owe`.

## Security

- **Router credentials.** The router password comes only from `RUTX11_PASSWORD` or the prompt.
  It is used only by paramiko, and it is never logged, never on a command line and never sent
  to the browser.
- **Wi-Fi password.** It travels only inside `uci batch` stdin. It is masked (`********`) in job
  logs, previews and API responses.
- **Router host key.** It is pinned on first use in `<data-dir>/known_hosts`. A changed key is
  refused.
- **Login.** Basic auth, compared in constant time. The no-login mode serves on loopback only
  and accepts only `Host: localhost` / `127.0.0.1`, which blocks DNS-rebinding pages.
- **Cross-site POSTs.** POSTs need `X-Requested-With: netui`, which a cross-site form cannot
  send.
- **Shell safety.** Every name that reaches a shell (sections, devices, packages, transaction
  ids) is validated first.

## Development

The code follows the Clean Architecture layering used across the rover repos:

```
uplink_manager/
├── domain/          # pure Python: uci model + parser, Wi-Fi scan parsing, the invariants
│                    #   and the plans that restore them (invariants.py), runtime checks
├── application/     # use cases (service.py: status/scan/audit/switch/repair/action and the
│                    #   transaction), single-flight jobs, the RouterGateway port
├── infrastructure/  # ssh_router.py (paramiko → RutOS), demo_router.py (in-memory router)
└── presentation/    # api.py (FastAPI), cli.py (rover-netui), static/index.html (no build step)
```

```bash
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest
```

The tests cover:
- the parser and the invariants, including **fixtures rebuilt from the real 2026-09-28
  config**: `rutos_20260928.uci` and `rutos_join_wan1.uci`, with every key `<REDACTED>`
- the use cases against the in-memory router: wrong-password rollback, adopt, the single-flight
  lock
- the router-side rollback script, executed under `sh` with stubbed `uci`/`wifi`
- the HTTP API and the CLI modes

The repo sits in the ROS workspace's `src/`; the `COLCON_IGNORE` at its root keeps colcon away
from it.

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| *Router unreachable* / `cannot reach router` | Not on the rover LAN or AP, or a wrong `--router`. Check with `ssh root@192.168.1.1`. |
| `router host key changed` | The router was reset or replaced. Delete `<data-dir>/known_hosts`. |
| *uncommitted changes (RutOS web UI?)* | Save or discard them in the RutOS UI, then retry. |
| Switch failed: *did not associate* | Wrong password, or the network is out of range. The previous uplink was restored. |
| *associated but got no IP address* | The site's DHCP didn't answer (captive or MAC-filtered network?). Restored. |
| *NAT (masquerade) missing* | fw3 didn't pick the interface up; run **Repair**. |
| `uci commit failed: uci: I/O error` | Another process held RutOS's `uci` lock (seen once live). The page retries 3 times; if it still fails, nothing was changed (rolled back). Try again. |
| A job looks stuck | A job lasts at most apply (≤ 90 s) + verify (75 s). The router restores itself after 180 s regardless. Check `/tmp/netui-rollback-*.log` on the router. |
| Where are the logs? | Job history: `<data-dir>/jobs.log` (container: `/data/jobs.log`, `balena logs … --service rover-a1-network`). Router side: `/tmp/netui-*`. |

## Limitations

- **Security types.** WEP and 802.1X / WPA-Enterprise networks are not supported, and neither
  are captive portals.
- **Shared radio.** The uplink and the rover AP share `radio0` (2.4 GHz), and the AP follows
  the uplink network's channel. It is configured for ch 11 but runs wherever the uplink is. Every
  switch therefore makes the AP **blink** (see [Tested on the rover](#tested-on-the-rover)); AP
  clients (BMS, front LED, laptops) reconnect by themselves within seconds.
- **5 GHz.** `radio1` (5 GHz) has no interface and is disabled while nothing uses it. It is still
  scanned, through a temporary interface (`iwinfo radio1 scan`), which makes a scan take ~9 s
  instead of ~3 s. Picking a 5 GHz network moves the uplink to `radio1` and enables that radio.
  The uplink then no longer shares a radio with the AP, so the AP keeps its own channel and
  stops blinking on switches.
- **Wrong passwords are slow to detect.** A wrong password is only detected when the 75 s
  verification window ends. wpa_supplicant logs `reason=WRONG_KEY` after ~25 s, which could be
  used to fail faster; not done yet.
