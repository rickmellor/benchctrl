# Setting up a bench host

A bench host is a small Linux machine that holds the USB cables. The
instruments plug into it, it runs the benchctrl agent as a service, and your
code — or your agent, or a run spec — reaches it over TCP.

This page walks through standing one up on a small single-board computer. The
reference platform is an **Arduino Uno Q**, and every measured behaviour below
came off one, but nothing here is specific to it: any machine that can see the
instruments and run Python 3.9+ will do, and on a normal Linux box with `pip`
most of the awkward steps disappear.

Why bother:

- The bench keeps running when your laptop closes.
- Two people can use one bench, with the writer claim arbitrating.
- Unattended runs survive a host disconnect and replay what you missed.
- Control loops run next to the instrument instead of across wifi.

## What you need

- A Linux machine with USB ports, on the same network as your workstation
- Python 3.9 or newer (`python3`; no `pip` required)
- Your user in `dialout`
- The instruments, plugged in

## 1. Get the code onto the board

If the board has `pip`, this is the whole step:

```bash
pip install benchctrl
```

Most small boards do not, so the deployment path is designed to need no package
manager at all. **benchctrl is pure Python, and a wheel is a zip:**

```bash
# on your workstation
adb push src/benchctrl /home/arduino/benchctrl/src/     # or scp
# on the board
cd /home/arduino/benchctrl/src && unzip pyserial-3.5-py2.py3-none-any.whl
PYTHONPATH=. python3 -m benchctrl.agent.main --simulate
```

Unzipping `pyserial` next to the package is enough. The board then runs the real
drivers, not a cut-down version.

### Instruments that need more than pyserial

| Instrument class | Extra wheels to unzip |
|---|---|
| Source-measure unit, resistance standard, PDU (serial) | none — `pyserial` only |
| ADU218, CP2112 | **none at all** — stdlib only |
| USB-TMC (both Rigols, the DMM) | `pyvisa`, `pyvisa-py`, `typing_extensions`, `pyusb` |

All four of those are pure Python too:

```bash
pip download --no-deps -d /tmp/w pyvisa pyvisa-py typing_extensions pyusb
cd /tmp/w && for w in *.whl; do unzip -q -o "$w" -d staged; done
rm -rf staged/*.dist-info
scp -O -r staged/. board:/home/arduino/benchctrl/src/
```

`libusb-1.0.so.0` is a system library and was already present on the reference
board.

The two USB-HID instruments needing nothing vendored is not a coincidence — it
is why they are viable on a board like this at all.

## 2. Install the udev rules

**Do this before you conclude an instrument is broken.** Each rule is a
privileged operation, so it is the operator's to run.

| Rule | Required for | Failure without it |
|---|---|---|
| `61-benchctrl-usbtmc.rules` | both Rigols, the DMM | the instrument is **invisible**: `discover()` returns `[]` and the driver says "not found" — reads exactly like a bad cable |
| `63-benchctrl-adu218.rules` | ADU218 | `open()` raises `EACCES` and names the rules file |
| `64-benchctrl-cp2112.rules` | CP2112 | `open()` fails on `/dev/hidraw*` |
| `60-benchctrl-ch341.rules` | QR10x, **only** where the kernel lacks `ch341` | no `/dev/ttyUSB*` appears at all |
| `62-benchctrl-ftdi.rules` | PDU serial console | grants nothing; pins a stable symlink so `ttyUSB0` cannot be handed to a different adapter |

```bash
sudo install -m 0644 deploy/udev/61-benchctrl-usbtmc.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger --action=add        # existing devices need this
```

Four things learned the hard way:

- **Re-triggering is not optional.** udev applies permissions at *event* time, so
  a reload alone changes nothing for an already-plugged device.
- **A one-off `chmod` looks like it works and then stops.** The node is recreated
  on every replug with a different number.
- **Every shipped rule is scoped to a VID/PID.** Do not widen the CP2112 rule to
  `SUBSYSTEM=="hidraw"` to save a line — on the reference board that also matches
  the attached USB keyboard, which makes it a keylogging surface rather than a
  bench rule.
- **The ADU218 case is not a missing driver.** `usbhid` is loaded and working; it
  ignores Ontrak devices on purpose (they are in the kernel's `hid_ignore_list`),
  which is exactly what this driver wants — `CLAIMINTERFACE` succeeds with no
  kernel driver to detach. The consequence is no `/dev/hidraw*` node, so raw
  `USBDEVFS` on `/dev/bus/usb/*` is the only route in, and interrupt transfers
  need **write** access.

The CP2112 needs write access even to *read* a pin, because a HID feature get is
a `GET_REPORT` over the control pipe.

### Stable device names

Ports renumber. Pin them, so a multi-instrument config means the same thing
after a reboot:

```
SUBSYSTEM=="tty", ATTRS{idVendor}=="0fce", ATTRS{idProduct}=="d1e6", \
  SYMLINK+="benchctrl/otii-$attr{serial}", MODE="0660", GROUP="dialout"
```

Then refer to `/dev/benchctrl/otii-<serial>` in the config's `open` block.

Devices identified by *probing* rather than by VID/PID — the QR10x has no
identity of its own behind a generic USB-serial bridge — can only have a
per-adapter symlink, which is safe only when one such adapter is attached.

## 3. Prove one instrument by hand first

Before adding systemd to the picture, confirm the boring parts work:

```bash
cd /home/arduino/benchctrl/src
PYTHONPATH=. python3 -c "
from benchctrl.drivers.otii_arc import OtiiArc
print(OtiiArc.discover())"
```

Do this **as the user the service will run as**, not as root. Proving it for
root proves the wrong thing — the permissions are the whole question.

## 4. Install the service

```bash
cd deploy
sudo ./install-agent.sh
```

It prints the generated token. That is what your workstation puts in its own
config, and **nothing reveals it in plaintext afterwards** — the discovery beacon
carries only a fingerprint. Save it now.

Installed layout:

| Path | Mode | What |
|---|---|---|
| `/etc/systemd/system/benchctrl-agent.service` | 0644 | the unit |
| `/etc/benchctrl/agent.json` | 0640 `root:<group>` | config **and token** |
| `/etc/benchctrl/agent.env` | 0644 | `PYTHONPATH`, no secrets |

Knobs:

```bash
sudo SRC_DIR=/opt/benchctrl/src RUN_USER=bench ./install-agent.sh
sudo PYTHON=/opt/venv/bin/python ./install-agent.sh      # a pip/venv install
```

`SRC_DIR` is the directory holding **both** `benchctrl/` and `serial/`. The
script imports them before touching systemd, because a wrong `PYTHONPATH`
otherwise surfaces only as a unit flapping every 5 s with an `ImportError`
buried in the journal.

Re-running upgrades the unit and **keeps an existing `agent.json`**, so an
upgrade does not silently rotate the token out from under every client.

The unit invokes `python3 -m benchctrl.agent.main` rather than the console
script, because that form works whether or not the board has `pip`.

### Configure which devices it serves

`/etc/benchctrl/agent.json`:

```json
{
  "host": "0.0.0.0", "port": 9737, "token": "...",
  "devices": ["otii_arc", "ontrak_adu218", "silabs_cp2112"],
  "deadman_s": 15, "max_recording_s": 300,
  "blob_dir": "/home/arduino/benchctrl/blobs",
  "runs_dir": "/home/arduino/benchctrl/runs"
}
```

Put `blob_dir` and `runs_dir` on the **large** filesystem. On the reference board
`/home` has 17 GB and `/` has under 2 GB, shared with container images.

A device needing a secret gets it from the agent's own environment, never from
this file: the PDU password comes from `BENCHCTRL_PDU_PASSWORD` where the driver
runs, because `open` arguments are emitted verbatim by `to_dict()` and cross the
plaintext RPC wire.

## 5. The safety-critical line

The unit ships with:

```ini
ExecStopPost=... --safe-stop
TimeoutStopSec=60
```

**Without it, `systemctl restart` leaves the DUT rail live across the gap**
between the old process dying and the new one binding. `TimeoutStopSec=60` is
there for the same reason: disarming talks to real instruments over serial, and a
SIGKILL halfway through is precisely the case the flag exists to prevent.

If you write your own unit, carry both lines.

**This is damage limitation, not a safety certificate.** A driver thread wedged
in a blocking read cannot be reached, and closing a serial port does not command
an output off — a source-measure unit holds its last commanded state. For
unattended overnight work at energies that matter, add a hardware interlock: a
relay on the DUT rail, the instrument's own GPO, or the ADU218's firmware
watchdog. See [Unattended runs](examples/unattended-runs.md).

## 6. Verify — and verify the stop path specifically

`Type=simple` reports `active` as soon as `fork()` succeeds, so `is-active` does
**not** mean the port is bound. Read the journal:

```bash
systemctl status benchctrl-agent --no-pager
journalctl -u benchctrl-agent -n 20 --no-pager
# want: "benchctrl-agent 1.2.0 serving otii_arc on 0.0.0.0:9737"
# a trailing "(SIMULATED)" means simulate is still true in agent.json
```

That trailing `(SIMULATED)` is worth looking for every time. A simulated bench
answers every call successfully, which is exactly what a working bench does.

Then prove the safety path fires, since it only runs on stop and it is the reason
the unit is shaped this way:

```bash
sudo systemctl stop benchctrl-agent
journalctl -u benchctrl-agent -n 10 --no-pager   # want: "safe-stop: otii_arc disarmed"
```

`safe-stop: otii_arc not open` is also fine — the device was never opened, so
there was nothing to disarm.

## 7. Connect from your workstation

```bash
export BENCHCTRL_REMOTE=bench.local:9737
export BENCHCTRL_TOKEN=<the token from step 4>
benchctrl adu218 relay-states
```

Or find it:

```python
from benchctrl.net import beacon
for bench in beacon.listen(timeout=5.0, token=my_token):
    print(bench)
```

See [Local and remote mode](local-vs-remote.md) for the config file, split
benches and precedence.

## Hardening

`User=<bench user>` plus `SupplementaryGroups=dialout` is enough — serial devices
are `root:dialout 0660`, so group membership grants access and **the agent never
needs root.**

Two settings are deliberately *not* tightened:

- `PrivateDevices=no` — `yes` hides `/dev/ttyACM*`, which is the entire job.
- `ProtectSystem=full`, not `strict` — `strict` also makes `/home` read-only,
  where `blob_dir` and `runs_dir` live.

## Keeping a board current

Once a board is on a bench and the code changes daily, "deployed" needs to be a
claim you can check:

```bash
./deploy/sync-board.sh --check     # is the board current? change nothing
./deploy/sync-board.sh             # make it current, then prove it
```

It runs from your **workstation**, copies the package, then compares a `sha256`
manifest from each end and either says `IN SYNC — N files identical` or names
every file that differs.

That verification half exists because of a real failure: a display component was
installed twice from a staging directory older than the repo, so the installer
faithfully installed pre-fix software both times. Every surface check passed,
because the panel *was* up — just not running the version that had been reviewed.
The root cause was not carelessness; it was that "deployed" was a claim nobody
could check. On its first real run against a board believed to be current it
found 16 files of genuine drift.

Two deliberate limits:

- **It touches only the `benchctrl` package.** The board's `src/` also holds
  vendored dependencies — `serial`, `usb`, `pyvisa` — which belong there and are
  not in the repo. Comparing the whole directory buried real drift under ~120
  phantom differences and, worse, put them in the category the sync *deletes*,
  which would have removed the board's only copy of `pyserial` and taken the
  agent down.
- **It restarts nothing.** The board runs a bench; bouncing the agent
  disconnects instruments. It prints the commands and leaves the decision to you.

**A running agent ends up on a mix of old and new modules** after a sync, because
imports are lazy. Restart it deliberately, when nothing is armed.

## Two board-specific notes

- **Container-based app frameworks cannot reach `/dev/ttyUSB*` or
  `/dev/ttyACM*`.** On the reference board, device passthrough is brick-level and
  limited to camera/microphone/speaker classes, so the agent runs as a native
  systemd service. That constraint is why the agent is stdlib + `pyserial` only.
- **Keep a USB cable attached during bring-up.** If wifi drops mid-run it is your
  only out-of-band console, and `adb forward tcp:9737 tcp:9737` gives you a
  working TCP path with no network at all — which is also how the latency figures
  in [Local and remote mode](local-vs-remote.md) were measured.

## Put a monitor on it

If the board has a display attached, it can boot straight into a fullscreen
read-only status panel — what is armed, what is attached, which mains outlets are
energised, what happened recently — so the state of the bench is something you
look up at rather than query:

```bash
sudo ./deploy/install-fui.sh      # the panel
sudo ./deploy/install-kiosk.sh    # boot into it, no login prompt
```

Test the panel over an SSH tunnel **before** enabling the kiosk: the second script
removes the board's only local login, and on a keyboard-less board SSH becomes the
only way back in. Full instructions, how to read the panel, and why it cannot be a
control surface are in [The bench display](bench-display.md).

Also in [`deploy/`](../../deploy/README.md): a fix for a display that arrives as
DisplayPort altmode through a USB-C hub and stays dark after boot.

## Next

- [The bench display](bench-display.md) — the status panel on this board's monitor
- [Local and remote mode](local-vs-remote.md) — what changes once you are remote
- [Unattended runs](examples/unattended-runs.md) — the payoff
- [`deploy/README.md`](../../deploy/README.md) — every script, in detail
