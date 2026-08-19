# Deploying the bench side

Everything here runs on the **bench machine** — the one with the instruments
physically attached. The client side (MCP server, notebooks, scripts) needs
none of it; see [`docs/remote.md`](../docs/remote.md) for the client config.

| | |
|---|---|
| [`install-agent.sh`](install-agent.sh) | the agent as a systemd service, disarming the bench on stop |
| [`install-display-hotplug.sh`](install-display-hotplug.sh) | optional: makes HDMI-through-a-USB-C-hub work on an Uno Q |
| [`verify-ch341-qr10x.sh`](verify-ch341-qr10x.sh) | required for a QR10x on a kernel without `ch341`: installs the udev rule, then proves the instrument end to end |

All three are POSIX `sh`, root-only, and idempotent.

## The agent service

```bash
git clone https://github.com/rickmellor/benchctrl   # or scp deploy/ across
cd benchctrl/deploy
sudo ./install-agent.sh
```

It prints the generated token — that is what the client puts in its own
config. Nothing else reveals it afterwards in plaintext; the beacon carries
only a fingerprint.

Installed layout:

| Path | Mode | What |
|---|---|---|
| `/etc/systemd/system/benchctrl-agent.service` | 0644 | the unit |
| `/etc/benchctrl/agent.json` | 0640 `root:arduino` | config **and token** |
| `/etc/benchctrl/agent.env` | 0644 | `PYTHONPATH`, no secrets |

Tune with environment variables:

```bash
sudo SRC_DIR=/opt/benchctrl/src RUN_USER=bench ./install-agent.sh
sudo PYTHON=/opt/venv/bin/python ./install-agent.sh     # pip/venv install
```

`SRC_DIR` is the directory holding **both** `benchctrl/` and `serial/`. The
script imports them before touching systemd, because a wrong `PYTHONPATH`
otherwise shows up only as a unit flapping every 5 s with an `ImportError`
buried in the journal.

Re-running upgrades the unit and **keeps an existing `agent.json`**, so an
upgrade doesn't silently rotate the token out from under every client.

### Why `python3 -m` and not the console script

`docs/remote.md` suggests `ExecStart=/usr/local/bin/benchctrl-agent`. That
assumes a pip install, and the Uno Q **has no pip** — the documented
deployment unzips the `pyserial` wheel next to the package and relies on
`PYTHONPATH`. The module form works for both, so it is what the unit ships;
`PYTHON=` covers the venv case.

### Why not an App Lab app

App Lab apps run in Docker containers and cannot reach `/dev/ttyACM*` —
passthrough is brick-level and limited to camera/mic/speaker classes. A native
service is the only way the agent sees an instrument.

### The safety-critical line

```ini
ExecStopPost=... --safe-stop
```

Without it, `systemctl restart` leaves the DUT rail live across the gap
between the old process dying and the new one binding. `TimeoutStopSec=60`
exists for the same reason: disarming talks to real instruments over serial,
and a SIGKILL half-way through is exactly the case the flag is meant to
prevent.

**This is damage limitation, not a safety certificate.** If the driver thread
is wedged in a blocking read, no software path can command the output off, and
closing a serial port does not disarm an Arc — it holds its last commanded
state. For unattended overnight runs the only real guarantee is a hardware
interlock (a relay on the DUT rail, or the Arc's own GPO). See
[`KNOWN_LIMITATIONS.md`](../KNOWN_LIMITATIONS.md).

### Verifying

`Type=simple` reports `active` as soon as `fork()` succeeds, so `is-active`
alone does not mean the port is bound. Read the journal:

```bash
systemctl status benchctrl-agent --no-pager
journalctl -u benchctrl-agent -n 20 --no-pager
# want: "benchctrl-agent 1.2.0 serving otii_arc on 0.0.0.0:9737"
# a trailing "(SIMULATED)" means simulate is still true in agent.json
```

Then prove the safety path actually fires, since it is the reason the unit
exists and it only runs on stop:

```bash
sudo systemctl stop benchctrl-agent
journalctl -u benchctrl-agent -n 10 --no-pager   # want: "safe-stop: otii_arc disarmed"
```

A `safe-stop: otii_arc not open` line is also fine — it means the device was
never opened, so there was nothing to disarm.

### Hardening notes

`User=arduino` + `SupplementaryGroups=dialout` is enough: serial devices are
`root:dialout 0660`, so group membership grants access and the agent never
needs root. Two settings are deliberately *not* tightened:

- `PrivateDevices=no` — `yes` hides `/dev/ttyACM*`, which is the entire job.
- `ProtectSystem=full`, not `strict` — `strict` also makes `/home` read-only,
  where `blob_dir` and `runs_dir` live.

## The QR10x on a kernel without `ch341`

Only needed where the kernel omits the CH340/CH341 driver — Arduino's Uno Q
does (`# CONFIG_USB_SERIAL_CH341 is not set`, and no generic fallback). The
symptom is a device that enumerates as `USB Serial` but binds no driver, so no
`/dev/ttyUSB*` appears and the QR10x is unreachable.

`benchctrl.transports.ch341` drives the chip from userspace over libusb and
hands back a **real pty**, so the QR10x driver is untouched — it opens a
`/dev/pts/N` path with ordinary `serial.Serial`.

```bash
sudo ./verify-ch341-qr10x.sh
```

That installs `udev/60-benchctrl-ch341.rules` and then proves the whole chain,
running the instrument half **as the service user** rather than as root —
proving it for root would prove the wrong thing.

### Why a udev rule is required, and why `chmod` won't do

libusb writes to `/dev/bus/usb/BBB/DDD`, which the kernel creates `root:root
0664`. Control transfers need *write*, so an unprivileged agent fails with
`[Errno 13] Access denied`. The rule makes those nodes `root:dialout 0660`.

A one-off `chmod` looks like it works and then stops: the node is recreated on
every replug, and `DDD` changes each time. The rule is also scoped to
`1a86:7523` alone — deliberately not a blanket `usb_device` rule, which would
hand the bench user write access to every USB device on the box.

Re-triggering matters too. udev applies permissions at *event* time, so
`udevadm control --reload-rules` alone changes nothing for an already-plugged
device; the script follows it with `udevadm trigger --action=add`.

Needs `pyusb` on the board (pure Python — unzip the wheel next to `benchctrl`,
same as `pyserial`; see [`docs/remote.md`](../docs/remote.md)).

Verified end to end on a real QR101A-1M-R1 (serial 00000248, fw 5.967KS): a
100.0 Ω setpoint reading back 100.038 Ω, repeatable across open/close cycles.

## Display hotplug

Only needed on a board whose display arrives as **DisplayPort altmode** over
USB-C. Verified on an Uno Q + Anker A8346 7-in-1 hub.

```bash
sudo ./install-display-hotplug.sh
```

| File | Installs to |
|---|---|
| `benchctrl-display-hotplug` | `/usr/local/bin/` (0755) |
| `systemd/benchctrl-display-hotplug.service` | `/etc/systemd/system/` (0644) |
| `udev/99-benchctrl-display-hotplug.rules` | `/etc/udev/rules.d/` (0644) |

### The problem

The Uno Q has **no HDMI port of its own**. HDMI arrives as DP altmode on the
USB-C spare lanes, driven by the SoC's display controller — so it is *not* a
USB video device. `lsusb` shows only hubs, and the panel appears as the single
DRM connector `card0-DP-1` on Qualcomm `msmdrmfb`.

If the hub negotiates DP **after** Xorg's startup probe, Xorg logs

```
(II) modeset(0): Output DP-1 disconnected
(WW) modeset(0): No outputs definitely connected, trying again...
```

and never assigns a CRTC. The kernel *does* see the later hotplug — sysfs
`status` flips to `connected` and `modes` fills in — but nothing tells X to
use the output, so `enabled` stays `disabled` and the panel stays dark.

A startup-ordering problem, **not** a missing driver and not a bad EDID. (The
monitor under test was a DELL SE198WFP, a 2007-era 1440x900 panel — hence a
128-byte EDID with no CTA extension, normal for its vintage.)

### Three design decisions

**A systemd unit, not `RUN+=` in the udev rule.** udev kills `RUN` helpers
after ~59 s and forbids sleeping ones. This helper *must* wait: a hotplug can
fire before the connector's `modes` file is populated, and `xrandr --auto`
with an empty mode list silently does nothing.

**`DISPLAY`/`XAUTHORITY` are parsed from Xorg's `/proc/PID/cmdline`**, not
hardcoded to `/var/run/lightdm/root/:0` — that path is lightdm-specific and
has changed across versions. Note the parsing hazard: the auth path itself
*ends in* `:0`, so a naive "find the `:N` argument" detector picks up the auth
file instead of the display. The helper tracks the token after `-auth`
separately.

**The unit is installed but not `enable`d.** The udev rule pulls it in on
demand via `SYSTEMD_WANTS`; enabling it would additionally run it once at
every boot.

Idempotent by construction — connectors already `enabled` are skipped — which
is what makes hotplug bursts (replug, DP link retrain) safe. It is also
connector-agnostic: it strips the `cardN-` prefix, so `HDMI-A-1` works the
same as `DP-1`, and the official Arduino USB-C Hub (8-in-1) takes the same
DP-altmode path.

### Verifying

```bash
udevadm verify /etc/udev/rules.d/99-benchctrl-display-hotplug.rules
sudo sh -c 'XAUTHORITY=/var/run/lightdm/root/:0 DISPLAY=:0 xrandr --output DP-1 --off'
cat /sys/class/drm/card0-DP-1/enabled          # disabled
sudo udevadm trigger --action=change --subsystem-match=drm
sleep 6
cat /sys/class/drm/card0-DP-1/enabled          # enabled
journalctl -t display-hotplug -n 5 --no-pager  # "enabled DP-1 (1440x900)"
```

**Trust the journal, not `systemctl show`.** For a `Type=oneshot` unit
`ActiveEnterTimestamp=` reads empty even after a successful run, and a stale
`Result=success` from a previous invocation is easy to misread as evidence
this one fired. `journalctl -t display-hotplug -b` is the reliable signal.

Verified 2026-08-19 in all three cases: cold boot with the display already
linked (Xorg configures it itself and the helper correctly never runs); a
synthetic `udevadm trigger`; and the real race — booted with HDMI unplugged so
Xorg started blind at t=17.9 s, attached ~3 min later, helper fired in the
same second and the panel came up unattended. It ran **exactly once** despite
the multi-event plug-in burst, and the mode-wait poll earned its place:
`modes` populated right around the event.
