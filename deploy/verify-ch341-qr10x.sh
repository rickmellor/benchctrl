#!/bin/sh
# Install the CH341 udev rule, then prove the QR10x works through the
# userspace bridge end to end.
#
#     sudo ./verify-ch341-qr10x.sh
#
# Safe to re-run. Read-only as far as the instrument is concerned: it asks
# the QR10x for its identity and never sets a resistance or drives a load.

set -eu

SRC_DIR=${SRC_DIR:-/home/arduino/benchctrl-1.2.0/src}
PYTHON=${PYTHON:-/usr/bin/python3}
RULE=60-benchctrl-ch341.rules

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ "$(id -u)" -ne 0 ]; then
    echo "must run as root (try: sudo $0)" >&2
    exit 1
fi

# --- 1. the udev rule ------------------------------------------------------
if [ -f "$here/udev/$RULE" ]; then
    rule_src="$here/udev/$RULE"
elif [ -f "$here/$RULE" ]; then
    rule_src="$here/$RULE"
else
    echo "cannot find $RULE next to this script" >&2
    exit 1
fi

install -m 0644 "$rule_src" /etc/udev/rules.d/
udevadm control --reload-rules
# The device is already plugged in, so a rule reload alone changes nothing —
# permissions are applied at event time. Re-trigger to apply them now.
udevadm trigger --action=add --subsystem-match=usb
sleep 2
echo "installed /etc/udev/rules.d/$RULE"

# --- 2. show the resulting node -------------------------------------------
echo
echo "--- CH340 USB node permissions:"
found=0
for d in /sys/bus/usb/devices/*/; do
    [ "$(cat "$d/idVendor" 2>/dev/null)" = "1a86" ] || continue
    [ "$(cat "$d/idProduct" 2>/dev/null)" = "7523" ] || continue
    busnum=$(cat "$d/busnum" 2>/dev/null)
    devnum=$(cat "$d/devnum" 2>/dev/null)
    node=$(printf "/dev/bus/usb/%03d/%03d" "$busnum" "$devnum")
    ls -l "$node"
    found=1
done
if [ "$found" -eq 0 ]; then
    echo "no CH340 (1a86:7523) found on the bus — is it plugged in?" >&2
    exit 1
fi

# --- 3. end-to-end: bridge + QR10x identity -------------------------------
# Run as the service user, not root: proving it works for root would prove
# the wrong thing, since the agent runs as arduino.
echo
echo "--- opening the bridge and querying the QR10x (as arduino):"
run_user=${RUN_USER:-arduino}
su -s /bin/sh "$run_user" -c "PYTHONPATH='$SRC_DIR' '$PYTHON' -" <<'PYEOF'
import sys, time
from benchctrl.transports.ptybridge import open_ch341_pty

bridge = open_ch341_pty(baudrate=115200)
print(f"  bridge pty: {bridge.port}")
try:
    from benchctrl.drivers.eastwood_qr10x import QR10x

    qr = QR10x.open(bridge.port)
    try:
        info = qr.info()
        print("  QR10x REACHED over the userspace CH341 bridge:")
        for field, value in info.to_dict().items():
            if value not in (None, ""):
                print(f"    {field:28} = {value}")
        # A live measurement too: identity alone could in principle come from
        # a cached descriptor, but a setpoint readback is a real round-trip.
        print(f"    {'setpoint_ohms (live read)':28} = {qr.get_setpoint()}")
        print(f"    {'actual_ohms (live read)':28} = {qr.actual_resistance()}")
        print(f"  bridge moved {bridge.host_to_device_bytes} B out, "
              f"{bridge.device_to_host_bytes} B in")
    finally:
        qr.close()
except Exception as exc:
    print(f"  FAILED: {type(exc).__name__}: {exc}")
    sys.exit(1)
finally:
    bridge.close()
PYEOF

echo
echo "CH341 + QR10x verified."
