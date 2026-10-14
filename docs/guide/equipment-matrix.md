# Supported equipment

Eight instruments are supported today. They are peers — there is no base
instrument the others extend, and you install only what you have.

This page answers two questions: *is my instrument supported*, and *which of
these should I be using for the measurement I am trying to make*. The second
one matters more, because the wrong instrument gives you a plausible number
rather than an error.

## The matrix

| Instrument | Role | Wire stack | Extra install | Switches / energises? |
|---|---|---|---|---|
| Qoitech Otii Arc / Arc Pro | Source-measure unit | USB CDC-ACM, vendor binary protocol | none | **yes** — sources into the DUT |
| Eastwood Tech QR10x | Programmable resistance standard | USB-serial (CH340), AT commands | none | no — passive |
| Rigol DL3031A | Electronic load | USB-TMC + SCPI | `pyvisa` | **yes** — sinks current |
| Rigol DP2031 | Triple-output supply | USB-TMC + SCPI | `pyvisa` | **yes** — sources into the DUT |
| Siglent SDM4065A | 6½-digit multimeter | USB-TMC + SCPI (or LXI) | `pyvisa` | no — measure-only |
| CyberPower PDU41002 | 8-outlet switched PDU | vendor CLI over serial **or** SSH | none | **yes — mains** |
| Ontrak ADU218 | 8 relays + 8 digital inputs | USB HID, raw ioctls | none | **yes** — signal contacts |
| Silicon Labs CP2112 | 8 open-drain control lines | USB HID feature reports | none | **yes** — drives a DUT pin |

Four of the eight need no third-party package at all. Two of those talk USB HID
directly out of the standard library, which is what makes them viable on a small
bench-host board with no compiler and no package manager.

Every instrument also has a wire-protocol simulator, so you can write and test
the whole measurement before the hardware arrives. See
[Theory of operation](theory-of-operation.md#the-simulators-are-not-mocks) for
what that does and does not prove.

## Choosing an instrument

### To source power into a DUT

| You want | Use |
|---|---|
| Fine current resolution and a native ~4 kHz current stream | **Otii Arc / Arc Pro** |
| Three independent rails, series/parallel pairing, per-channel OVP/OCP | **Rigol DP2031** |
| A DUT to believe it is running on a specific cell | **Arc** with the battery emulator |
| Mains, switched | **CyberPower PDU41002** |

The Arc is the instrument the project started with and the only one that
implements the `SourceMeasurementUnit` Protocol today, so it is the one the
battery emulator, the profiler and the run engine instantiate.

Its ceiling matters for cell emulation: **the Arc Pro high range caps at
≈ 4.2 V under load**, so a fresh LiPo's open-circuit voltage (~4.31 V) clamps.
Single-cell chemistries at moderate current are fine. Multi-cell packs are not.

### To apply a load

This is the choice most likely to give you a wrong number instead of an error,
and both instruments were run through the same matrix to find the crossover.

| Regime | Use | Why |
|---|---|---|
| Below ~1 mA — sleep, quiescent, standby | **QR10x** | it is a passive resistor network, so it stays correct as low as the SMU can measure |
| Above ~1 mA — active, TX, inrush | **DL3031A** | 60 A, 350 W, and firmware sequencing |
| Sub-100 µs load steps | **DL3031A** LIST mode | firmware plays the sequence; a host loop cannot |
| A cell's collapse point at real current | **DL3031A** | no 1 W dissipation ceiling to work around |

The crossover is not a rule of thumb. An active electronic load needs enough
current for its regulation loop to work; below roughly 1 mA the DL3031A's CR
mode loses regulation and reads as effectively open.

Measured on the bench at the 10 kΩ step against a CR123A cell (3.20 V, so
0.32 mA expected): **the QR10x measured 0.322 mA; the DL3031A measured
0.012 mA** — the SMU's own current noise floor. Neither instrument raised
an error. The DL3031A simply reported a number, and it was wrong by a factor
of 27.

The reverse case is just as real. The QR10x's built-in safety minimum keeps it
within a ~1 W continuous rating, so characterising a cold LiPo at 12 Ω has to
be clamped to 20 Ω — and the clamped measurement understates the sag by nearly
500 mV. See [Power characterization](examples/power-characterization.md).

So: **QR10x for the sleep phase, DL3031A for the active phase.** Having both is
not redundancy.

The QR10x also has a floor of its own that shapes what you can ask of it:
relay switching takes 30–95 ms, so it cannot resolve a load step faster than
that. It sets the phase duration you can trust in a duty-cycle pattern.

### To measure independently

Use the **SDM4065A**. It sources nothing, so there is no output an agent or a
typo can energise — which makes it the instrument to reach for when you want a
second opinion on a number rather than another way to drive the DUT.

Cross-checking one measurement with a second instrument is worth the trouble.
On this bench a 100 Ω standard read **100.038 Ω** on the DMM and **100.0425 Ω**
through the resistance standard's own report — agreement that made both
believable in a way either alone would not have been.

Because it is measure-only, its failure mode is a **plausible wrong number**
rather than a refusal, and the API is shaped around that: ranges are pinned
rather than autoranged where it matters, and an out-of-range reading raises
instead of returning the `9.9E37` sentinel as a believable float.

### To switch or hold a line

| You want | Use | Rating |
|---|---|---|
| Cut and restore mains to a DUT or an instrument | **PDU41002** | 8 outlets, 120 V / 20 A |
| Break a signal lead, or read a digital input | **ADU218** | 8 SSRs, 1 A, to 120 V |
| Hold a reset or boot-strap pin | **CP2112** | 8 open-drain lines |
| A hardware interlock that works with no software running | **ADU218 watchdog** | see below |

These three are not interchangeable, and the distinction is about what is on
the other side of the contact:

- The **PDU** switches *mains*. Its allowlist is a required argument with no
  "all" default, no method accepts an aggregate target, and the rendered command
  is pattern-checked before it goes on the wire — because the vendor CLI has a
  single line that de-powers the whole rack.
- The **ADU218** switches *signal-level* circuits — 1 A solid-state relays on
  instrument leads. Its allowlist defaults to all eight, deliberately: the
  consequence of a mistake is different, and the hardware watchdog is the
  per-test interlock.
- The **CP2112** does not switch a circuit at all; it pulls a net low and
  releases it. Open-drain is the only mode this driver offers, so it cannot
  fight a target's rail and it fails safe on unplug.

**The ADU218's watchdog is the only real interlock in the list.** `WDn` sets and
arms in one command; while armed, the device de-energises all eight relays by
itself if no command arrives within the interval, with no software in the
decision path. A wedged process, a killed agent, an unplugged cable and a
panicking kernel all look identical to it, and all drop the load. The 1-second
setting was bisected on hardware — the trip falls in **(0.90, 1.10] s**.

That property is why it is the recommended interlock for anything unattended.
See [Theory of operation](theory-of-operation.md#the-limit-worth-stating-plainly)
for why software alone cannot make the same promise, and
[Unattended runs](examples/unattended-runs.md) for wiring it in.

### To free up an instrument

Worth stating on its own, because it is the reason one of these drivers exists.

During a board bring-up on this bench, an Arc Pro spent a session doing nothing
but toggling a reset pin — a precision source-measure unit acting as a switch.
The **CP2112 is a ~$15 board that does exactly that job**, which puts the SMU
back on the measurement. If you are holding a DUT in reset with something that
can measure microamps, you are one cheap part away from a better bench.

## What is not supported

Being specific about this saves you an afternoon:

- **No GPIB, no RS-232, no LAN** on the Rigols — USB-TMC only, deliberately.
  The SDM4065A is the exception; it accepts a `TCPIP::` resource string.
- **No SNMP** on the PDU. Its CLI covers switching, state, metering and
  identity, so SNMP would have added a second differently-shaped protocol for
  no capability — and it is disabled on the device.
- **No I²C/SMBus** on the CP2112. Only the GPIO half is implemented.
- **No daisy-chained PDUs.** The vendor CLI supports it; an off-by-one would
  switch a different physical box.
- **The Arc's expansion port, GPO and UART are exposed**, but nothing in the
  guide depends on them.

## Firmware quirks you will meet

These are the ones likely to look like a benchctrl bug. Each is documented with
what was measured in [`KNOWN_LIMITATIONS.md`](../../KNOWN_LIMITATIONS.md).

| Instrument | Behaviour |
|---|---|
| Arc Pro | high-range output caps at ≈ 4.2 V under load |
| Arc | the battery emulator and `record()` cannot run concurrently on one Arc — the run engine refuses the combination rather than deadlocking |
| DL3031A | a 4-step LIST program is a firmware bug; the driver rejects it at the SDK level |
| DL3031A | `:SOUR:FUNC:MODE FIXed` is one-way — only a power cycle returns the device to FIX mode |
| DP2031 | channel-pair SERies/PARallel state survives `*RST` |
| PDU41002 | exactly **one CLI session exists on the whole device**, across all transports. So serial and network alternate; they are never concurrent. Closing the port does not end the session — the driver sends `exit`, and must |
| PDU41002 | SSH group-exchange key negotiation fails on firmware 1.3.4; the driver forces a fixed group. Pubkey auth is refused outright, so it authenticates with a password |
| ADU218 | the device has **no error reply**. An unknown command, an out-of-range argument and a write-only command are byte-identical on the wire: nothing comes back. This is why writes verify by read-back |
| ADU218 | *any* command refeeds the watchdog, including a plain state read — so a status-polling loop silently neuters it |
| CP2112 | every transition is a separate USB control transfer, so timing is bus-bound. `trigger_reset_pulse` refuses durations under 5 ms rather than silently stretching them |

## Bringing your own

The `SourceMeasurementUnit` Protocol is the extension point that matters: the
battery emulator, the profiler and the run engine depend on it and never name a
concrete driver, so a conforming instrument slots in without touching them.

For a load, a switch or a meter there is no Protocol yet — deliberately, because
one implementation is not enough evidence to design an interface from. Your
driver is a peer class, and everything that uses it names it directly.

See [Adding a driver](adding-a-driver.md).
