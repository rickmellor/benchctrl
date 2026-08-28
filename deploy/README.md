# Deploying the bench side

Everything here runs on the **bench machine** — the one with the instruments
physically attached. The client side (MCP server, notebooks, scripts) needs
none of it; see [`docs/remote.md`](../docs/remote.md) for the client config.

| | |
|---|---|
| [`install-agent.sh`](install-agent.sh) | the agent as a systemd service, disarming the bench on stop |
| [`install-display-hotplug.sh`](install-display-hotplug.sh) | optional: makes HDMI-through-a-USB-C-hub work on an Uno Q |
| [`verify-ch341-qr10x.sh`](verify-ch341-qr10x.sh) | required for a QR10x on a kernel without `ch341`: installs the udev rule, then proves the instrument end to end |
| [`install-fui.sh`](install-fui.sh) | optional: the read-only HDMI status display (`benchctrl-fui`) |
| [`install-kiosk.sh`](install-kiosk.sh) | optional: boots the board straight into that display, **no login prompt** — run `install-fui.sh` first |
| [`udev/61-benchctrl-usbtmc.rules`](udev/61-benchctrl-usbtmc.rules) | required for USB-TMC instruments (SDM4065A, both Rigols) on a kernel without `usbtmc` |
| [`udev/62-benchctrl-ftdi.rules`](udev/62-benchctrl-ftdi.rules) | recommended for the CyberPower PDU41002's serial console: grants no permissions (`ftdi_sio` already does), but pins a stable symlink so `ttyUSB0` cannot be handed to another adapter |
| [`udev/63-benchctrl-adu218.rules`](udev/63-benchctrl-adu218.rules) | required for the Ontrak ADU218 — a USB HID device with **no** kernel driver, so raw `USBDEVFS` is the only route in |
| [`sync-board.sh`](sync-board.sh) | during development: push this checkout to a board and **prove** it landed — runs from your workstation, not the board |
| [`board_sync_manifest.py`](board_sync_manifest.py) | what `sync-board.sh` compares with; also useful on its own to answer "is the board current?" |
| [`board_apply_sync.sh`](board_apply_sync.sh) | the board-side half of that: extract, then delete what the tarball did not carry |

The `install-*.sh` scripts are POSIX `sh`, root-only, and idempotent. The three
sync files are the exception: they run **unprivileged** and never ask for root,
and `board_sync_manifest.py` is Python (stdlib only, so it works on a board with
no pip).

`install-kiosk.sh` is the one with a way to hurt you: it removes the board's only
local login. It refuses to run unless ssh is active and the display is already
installed, and `--undo` (or deleting one lightdm drop-in) reverses it. See
`docs/dashboard.md`.

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

## USB-TMC instruments on a kernel without `usbtmc`

| File | Installs to |
|---|---|
| `udev/61-benchctrl-usbtmc.rules` | `/etc/udev/rules.d/` (0644) |

Same mechanism as the CH341 rule above, different missing driver. The Uno Q
builds without `CONFIG_USB_TMC`, so there is no `/dev/usbtmc0` for the SDM4065A
or either Rigol; pyvisa-py drives them over libusb instead, which again needs
write access to `/dev/bus/usb/BBB/DDD`.

**The failure mode is worse than a permission error.** Without the rule the
instrument is *invisible*, not merely unopenable:

```
$ python3 -c "from benchctrl.drivers.siglent_sdm4065a import discover; print(discover())"
[]
```

pyvisa-py needs to read the USB **string descriptors** to build a resource
name, and that read is a control transfer — so it fails, and the device is
omitted from `list_resources()` entirely. The driver then reports "no SDM4065A
found", which reads exactly like a bad cable. Confirmed on the bench board:
`usb.core.find()` locates `f4ec:1220` fine, and `os.access(node, os.W_OK)` is
`False` while `os.R_OK` is `True`.

```bash
sudo install -m 0644 udev/61-benchctrl-usbtmc.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger --action=add     # existing devices need this, see above
```

Needs `pyvisa` and `pyvisa-py` on the board alongside `pyusb`. All three are
pure Python — unzip the wheels next to `benchctrl`, same as `pyserial`.
`pyvisa-py` also wants `typing_extensions`.

## The Ontrak ADU218 — a HID device with no driver to detach

| File | Installs to |
|---|---|
| `udev/63-benchctrl-adu218.rules` | `/etc/udev/rules.d/` (0644) |

```bash
sudo install -m 0644 udev/63-benchctrl-adu218.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger --action=add
```

This one is **not** a missing-module problem like the two above. `usbhid` is
loaded and working — it has bound the board's keyboard perfectly well. It leaves
the ADU218 alone *on purpose*: Ontrak's vendor id is in the kernel's
`hid_ignore_list` (`drivers/hid/hid-quirks.c`), which is exactly what this
driver wants, because it means `CLAIMINTERFACE` succeeds with no kernel driver
to detach first.

The consequence is that there is no `/dev/hidraw*` node either, so the only
route in is raw `USBDEVFS` ioctls on `/dev/bus/usb/BBB/DDD` — which the kernel
creates `root:root 0664`. Interrupt transfers need **write** access, so without
the rule `os.open(path, O_RDWR)` fails `EACCES` before any protocol work starts.
That failure is at least honest: the driver catches it and names this rules file
in the error, rather than reporting the device as absent the way the USB-TMC
case does.

**No board-side dependency at all.** Unlike every other instrument here, the
ADU218 driver imports nothing outside the standard library — no pyserial, no
pyusb, no pyvisa. `deploy/sync-board.sh` carries it and there is nothing else to
vendor.

If a future kernel drops Ontrak from `hid_ignore_list`, `usbhid` will claim the
interface and the symptom will be a claim failure rather than a permission
error. That is the case `tests/test_hardware_ontrak_adu218.py` documents, so the
reason arrives with the failure.

## Keeping a board's source current

For development against a board that stays running. Not part of installation —
`install-agent.sh` handles that. This is for the loop afterwards, where the
board is on a bench and the code changes daily.

```bash
./deploy/sync-board.sh --check     # is the board current? change nothing
./deploy/sync-board.sh             # make it current, then prove it
./deploy/sync-board.sh --restart   # ...and bounce the FUI (never the agent)
```

Runs from your **workstation**. Configure with `BOARD=user@host`,
`REMOTE_SRC=...`, and `SSH_OPTS=...` if your ssh needs explicit options.

### Why it exists

The FUI was installed on the board twice from a staging directory older than the
repo, so `install-fui.sh` faithfully installed pre-fix software both times. The
launchers were missing their fullscreen flags; the deployed `state.py` had none
of the observer-role safety check. Every surface check passed, because the panel
*was* up — just not running the version that had been reviewed.

The root cause was not carelessness, it was that **"deployed" was a claim nobody
could check**. Files went across one at a time with `scp` and nothing compared
the result to anything. So the copy is the easy half of this script; the half
that was missing is that afterwards a `sha256` manifest from each end is
compared and the output either says `IN SYNC — N files identical` or names every
file that differs. Hence `--check` being a first-class mode: most days the useful
question is "is the board current?", not "make it current".

On its first real run against the Uno Q (2026-08-20, repo at `3cfb8dc`) it found
16 files of genuine drift on a board believed to be current: 14 differing and 2
never delivered (`sim/sdm4065a.py`, `transports/autoserial.py`). Every differing
file matched an older commit in `git log --all`, confirming stale deploy rather
than local edits on the board — worth checking before overwriting, since this
tool has no undo.

### Two things it deliberately does not do

**It does not touch anything outside the `benchctrl` package.** The board's
`src/` also holds **vendored dependencies** — `serial`, `usb`, `pyvisa`,
`pyvisa_py` — because it has no pip and no install path. They belong there and
they are not in this repo. Comparing the whole directory buried the real drift
under ~120 phantom differences, and worse, put them in the category the sync
*deletes* ("on the board, absent from the repo") — which would have removed the
board's only copy of pyserial and taken the agent down. So both manifests, the
tarball, and the stale-file sweep are all scoped to `$PACKAGE`.

The sweep itself is not optional: extracting a tarball over the top leaves a
file you deleted in the repo still present on the board, which is how a removed
module keeps running. It lives in `board_apply_sync.sh` rather than inside an
`ssh` string so that the only destructive code in the sync is lintable and
testable — embedded in a quoted argument it was neither, and `sh -n` could not
see into it.

Bytecode is handled asymmetrically, and the direction matters:

- `__pycache__` is **deleted** on the board, not compared. Bytecode from the
  board's 3.13 never matches the workstation's 3.12, so comparing it would report
  permanent unfixable drift.
- A `.pyc` in the **legacy location** (`benchctrl/panel.pyc`, beside the package)
  *is* compared, and swept. That is the form PEP 3147 will import with no source
  present — verified both ways on 3.12: with `pkg/mod.py` deleted, `pkg/mod.pyc`
  imports and returns its value while `pkg/__pycache__/mod.cpython-312.pyc`
  raises `ImportError`. Excluding it would make `--check` structurally blind to
  the one bytecode case that can keep deleted code running.

**It does not restart anything.** The board runs a bench. Bouncing the agent
disconnects instruments; restarting lightdm blanks the panel. Both need root,
which this script does not have, so it prints the commands and leaves the
decision to you.

Two things it will tell you rather than do:

- **A running agent ends up on a mix of old and new modules.** `registry.py`
  imports drivers lazily, inside the opener closure, so an agent that has not yet
  opened a given instrument imports the *new* driver into a process whose core
  modules are the *old* already-loaded ones. Restart it before trusting a driver
  change.
- **`--restart` does not kill the FUI.** `benchctrl-kiosk` starts the dashboard
  once and then `exec`s the browser, which replaces the shell and discards its
  cleanup trap — nothing supervises the dashboard, so nothing respawns it.
  `pkill` would leave the browser on a dead port, on a panel with no keyboard.
  `systemctl restart lightdm` is the thing that works.

It also copies `deploy/`'s launchers across (they drifted independently and
caused their own regression) but does **not** install them, because
`/usr/local/bin` needs root. It tells you the one-line `sudo install` if a
launcher changed.

### What bounds the damage

Two variables are the safety boundary, so both are validated rather than trusted:

- **`PACKAGE`** must be a single plain directory name. `PACKAGE=.` would make the
  sweep the unscoped sweep described above. Enforced in all three files, because
  each is a usable entry point.
- **`REMOTE_SRC`** must end in `/src`. Crude, but it excludes exactly the
  destructive typo: `/home/arduino/benchctrl` — one component from the default —
  is the agent's live blob and runs directory, holding verified run artifacts.

`check_sync` also checks the exit status of every command it runs. That is not
housekeeping: it is called from an `if`, which suspends `set -e`, so an unchecked
failure would leave an empty manifest — and two empty manifests compare as *in
sync*. "IN SYNC — 0 files identical" is the one output this tool must never
produce.

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
