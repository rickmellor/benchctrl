#!/bin/sh
# Re-enumerate the Axelera Metis after boot, on hosts that enumerate PCIe
# before the card has finished booting itself. Installed by install-vision.sh
# as /usr/local/bin/benchctrl-metis-rescan and run once by
# benchctrl-metis-rescan.service.
#
# The problem, found on a Raspberry Pi 5 (2026-09-12): the kernel enumerates
# the bus ~7 s after power-on and assigns the card's BARs; a few seconds later
# the card's own firmware finishes booting and resets its PCIe configuration,
# so the BARs no longer hold what the kernel wrote. Every host read of the
# card's memory then returns 0xFF (the card rejects it as an Unsupported
# Request), the driver logs "vmsi not available" and every command times out
# with "IRQ MSI timeout". An x86 BIOS enumerates tens of seconds after
# power-on, which is why the same card is fine in a desktop. Removing the
# device and rescanning the bus makes the kernel re-assign the BARs against
# the now-booted card, and the driver binds healthily ("vmsi configured").
#
# Idempotent and self-checking: it only rescans when the driver reports the
# card unhealthy, verifies the fix by reading the driver's log, and always
# aligns the PCIe Max Payload Size of card and root port (see align_payload).
set -eu

VENDOR_DEVICE=${VENDOR_DEVICE:-1f9d:1100}
ATTEMPTS=${ATTEMPTS:-4}
SETTLE_S=${SETTLE_S:-6}

if [ "$(id -u)" -ne 0 ]; then
    echo "must run as root" >&2
    exit 1
fi

find_dev() {
    lspci -D -d "$VENDOR_DEVICE" 2>/dev/null | awk '{print $1}' | head -1
}

healthy() {
    # The driver's own verdict, from the most recent probe of this device.
    dmesg | grep -E "axl $1:" | grep -E "vmsi (configured|not available)" | tail -1 | grep -q "vmsi configured"
}

# Max Payload Size: the card runs at 128 bytes and the Raspberry Pi 5 root
# port defaults to 512, `pci=pcie_bus_safe` notwithstanding. Every completion
# the root port then returns is malformed to the card (UESta MalfTLP+,
# CmpltTO+) and its DMA of the runtime firmware never lands, so the runtime
# reports "USR_DMA_XFER failed" while the mailbox keeps working. Set both
# ends to 128 (DevCtl bits 7:5 = MPS, 14:12 = MaxReadReq), after every
# rescan too, because re-enumeration re-derives them.
align_payload() {
    for d in "$1" "$2"; do
        cur=$(setpci -s "$d" CAP_EXP+0x8.w)
        new=$(printf '%04x' $(( 0x$cur & ~0x70e0 )))
        [ "$cur" = "$new" ] || setpci -s "$d" CAP_EXP+0x8.w="$new"
        setpci -s "$d" CAP_EXP+0xa.w=000f   # clear DevSta error bits
    done
    echo "metis-rescan: MaxPayload/MaxReadReq 128 on $2 and its root port $1"
}

root_port() {
    basename "$(readlink -f "/sys/bus/pci/devices/$1/..")"
}

dev=$(find_dev)
if [ -z "$dev" ]; then
    echo "metis-rescan: no $VENDOR_DEVICE on the bus; nothing to do"
    exit 0
fi
if healthy "$dev"; then
    echo "metis-rescan: $dev healthy (vmsi configured); nothing to do"
    align_payload "$(root_port "$dev")" "$dev"
    exit 0
fi

n=0
while [ $n -lt "$ATTEMPTS" ]; do
    n=$((n + 1))
    echo "metis-rescan: $dev unhealthy (BARs not honoured) — remove + rescan, attempt $n/$ATTEMPTS"
    echo 1 > "/sys/bus/pci/devices/$dev/remove"
    sleep 2
    echo 1 > /sys/bus/pci/rescan
    sleep "$SETTLE_S"
    dev=$(find_dev)
    if [ -z "$dev" ]; then
        echo "metis-rescan: card left the bus after rescan; waiting" >&2
        sleep "$SETTLE_S"
        dev=$(find_dev)
        [ -z "$dev" ] && continue
    fi
    if healthy "$dev"; then
        echo "metis-rescan: $dev healthy after rescan (vmsi configured)"
        align_payload "$(root_port "$dev")" "$dev"
        ls -l /dev/metis* 2>/dev/null || true
        exit 0
    fi
done
echo "metis-rescan: $dev still unhealthy after $ATTEMPTS attempts" >&2
exit 1
