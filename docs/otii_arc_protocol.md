# Wire protocol

What's on the USB cable between the host and an Arc / Arc Pro. This
documents the reverse-engineering result benchctrl's implementation
relies on. Reconstructed from passive observation of legitimate USB
traffic; full decoding history lives in the parent repository.

## Transport

The device enumerates as a USB CDC-ACM device (VID `0x0FCE`,
PID `0xD1E6`). The host opens the resulting virtual COM port at any
baud rate (the value is irrelevant — full-speed USB carries the actual
bytes). DTR / RTS are held low to match the vendor stack's posture.

## Framing

Every URB in both directions is wrapped:

```
+--------+-----------+-----------+----------------+
| magic  | length L  | checksum  |   payload      |
| 4 B    | u16 LE    | u16 LE    |   L B          |
| A3 2C  |           |           |                |
| B5 7F  |           |           |                |
+--------+-----------+-----------+----------------+
```

- `magic`: `A3 2C B5 7F` — constant
- `length`: payload length in bytes
- `checksum`: sum of payload bytes, mod 65536
- `payload`: L bytes

Per-byte sum is sufficient for integrity at this level — the underlying
USB stack handles real CRC.

## Outbound (host → device) payloads

### SET-parameter — type `0x66` (16-byte payload)

```
[seq:u32 LE] [type=0x66:u32 LE] [cmd:u32 LE] [value:u32 LE]
```

- `seq`: monotonic per-connection sequence number (starts at `0x1000`)
- `cmd`: see command table below
- `value`: command-specific unsigned 32-bit value (units below)

### Session-init

Required on every fresh connection before the device will stream.
Three OUTs:

| step | payload (hex) | meaning |
|---|---|---|
| 1 | `01 00 00 00 14 00 00 00` | session wake |
| 2 | `[seq:u32] 64 00 00 00 29 00 00 00 00 00 00 00` | init-step-2 |
| 3 | `[seq:u32] 78 00 00 00 17 00 00 00 01 00 00 00` | init-step-3 / subscribe |

After step 3 the device begins streaming all 12 default channels at
its baseline rate (~6 Hz observed; full rate not yet decoded).

### Recording start / stop — per-channel enable (`type=0x78`)

There is **no single START_RECORDING command**. Recording is set up by
sending one channel-enable frame per requested channel:

```
[seq:u32 LE] [0x78 00 00 00] [wire_id:u32 LE] [value:u32 LE]
```

with `value = 1` to start streaming that channel, `value = 0` to stop.
After the per-channel burst, send a single 8-byte cleanup payload
(`type=0x7C`) to flush:

```
[seq:u32 LE] [0x7C 00 00 00]
```

The "init step 3" sent on session open is mechanically the same command
applied to channel `0x17` (rx) — that's what kicks the device into its
baseline streaming mode.

Once a regular channel (e.g. `mc` `0x00`) is enabled this way, the device
switches from the slow baseline envelope to high-rate packed-sample
frames (see "Inbound packed sample frame" below). Disabling all the
explicitly-enabled channels (val=0) returns the device to baseline.

### Legacy `69 83 2a ff …` payload

The 76-byte `69 83 2a ff 17 02 00 00 …` payload used by historical
versions of `arc_direct` (and benchctrl v0.1) is **not** what the vendor
stack sends. It was a misread of the device's *inbound* packed sample
frame and produced no useful effect when sent host→device.

## Inbound (device → host) payloads

### Baseline sample record (12 B, inside a frame payload)

```
[02 00 08 00] [chan:u32 LE] [value:f32 LE]
```

Emitted when streaming in baseline mode (no channel explicitly enabled
for recording). One frame can carry many of these records back-to-back.
The `chan` field is the channel wire id (see table below).

### Inbound packed sample frame (variable, native-rate)

Emitted as the *entire payload* of a wire frame, once any channel is
enabled for recording via the `type=0x78` mechanism above:

```
[69 83 2a ff] [seq:u32 LE]
[per-channel record …]
[17 00 00 00 00 00 00 00]                       sentinel
```

Each per-channel record:

| Subtype | Size | Layout | Samples carried |
|---|---|---|---|
| 1 | 12 B | `[id:u16][1:u16][rate:u32][value:f32 LE]` | 1 sample |
| 4 | 24 B | `[id:u16][4:u16][rate:u32][v0..v3:f32 LE x4]` | 4 samples |

The frame arrives at the slowest enabled channel's rate (1 kHz for any
sub-1 channel). High-rate (sub-4) channels carry 4 samples per frame,
so `mc` and `mp` arrive at 4 kHz native even though frames come every
1 ms.

A 76-byte frame typical for `mc + mv + mp` decodes as:

```
0000  69 83 2a ff [seq:u32]                                magic + seq
0008  00 00 04 00 [rate=4000] [4 floats: mc samples]       mc sub-4
0020  01 00 01 00 [rate=1000] [1 float:  mv sample]        mv sub-1
002C  06 00 04 00 [rate=4000] [4 floats: mp samples]       mp sub-4
0044  17 00 00 00 00 00 00 00                              sentinel
```

### Error response (16-B payload)

```
[0e 03 99 ff 04 10 00 00] [error_code:i32 LE] [last_good:u32 LE]
```

Sent in response to a rejected SET. `error_code` is signed (e.g. -101
for `set_main_voltage(4.0)` in low range). `last_good` is the value
the parameter reverted to.

### SET ack (16-B payload)

```
[0e 03 99 ff] [0x1000 | cmd_code:u32 LE] [field:u32 LE] [value:u32 LE]
```

Sent after every accepted SET. The library parses these but does not
yet expose them as a public stream (could be wired into a "verify
last SET" API in v0.2).

## Command vocabulary (SET, type `0x66`)

| cmd code | meaning | value units |
|---|---|---|
| `0x05` | SET_4WIRE | 1 / 0 |
| `0x06` | SET_SRC_CUR_LIMIT_ENABLED | 1 (CC) / 0 (cutoff) |
| `0x08` | SET_RANGE | 0 = low, 1 = high |
| `0x09` | SET_MAIN_OUTPUT | 1 / 0 |
| `0x0B` | SET_MAIN_VOLTAGE | microvolts |
| `0x0C` | SET_OC_PROTECTION / SET_MAX_CURRENT | milliamps |
| `0x0D` | SET_MAIN_CURRENT (CC mode) | microamps |
| `0x1E` | SET_ADC_RESISTOR | micro-ohms |
| `0x28` | SET_UART_ENABLE | 1 / 0 |
| `0x29` | SET_UART_BAUDRATE | baud |
| `0x32` | SET_GPO | encoded `1 << ((pin-1)*3 + (1 if on else 0))` |
| `0x33` | SET_DIGITAL_VOLTAGE (EXP) | microvolts |
| `0x34` | ENABLE_5V | 5_000_000 = on, 0 = off |
| `0x7C` | ENABLE_LEGACY_SINK | 1 / 0 |

GPO encoding verified observationally:

| call | encoded value |
|---|---|
| `set_gpo(1, True)` | 2 (bit 1) |
| `set_gpo(1, False)` | 1 (bit 0) |
| `set_gpo(2, True)` | 16 (bit 4) |
| `set_gpo(2, False)` | 8 (bit 3) |

Each pin reserves 3 bits; bit 2 of the block is unused (possibly
tri-state).

## Channels

| wire id | code | label | subtype | nominal rate | unit | always-on? |
|---|---|---|---|---|---|---|
| `0x00` | mc | Main current | 4 | 4000 | A | |
| `0x01` | mv | Main voltage | 1 | 1000 | V | |
| `0x02` | ac | ADC current | 1 | 1000 | A | |
| `0x03` | av | ADC voltage | 1 | 1000 | V | |
| `0x04` | sn | Sense− voltage | 1 | 1000 | V | |
| `0x05` | sp | Sense+ voltage | 1 | 1000 | V | |
| `0x06` | mp | Main power | 4 | 4000 | W | |
| `0x07` | ap | ADC power | 1 | 1000 | W | |
| `0x10` | vb | VBUS | 1 | 1000 | V | |
| `0x11` | vj | DC jack | 1 | 1000 | V | |
| `0x14` | tp | Temperature | 1 | 1000 | °C | yes |
| `0x16` | i1 / i2 | GPI bitmap | 1 | 1000 | digital | |
| `0x17` | rx | UART log | — | text | text | |

**Important caveat on rates**: the documented rates are the channel's
*capabilities*. Empirically the device's baseline post-init stream is
about **6 Hz** across every channel — the legacy `arc_direct` library
exhibits the same. A higher-rate streaming mode exists (the Otii
desktop client achieves it) but the unlock command has not been
reverse engineered. Tracked in `ROADMAP.md` as v0.2.

## Things we have *not* decoded (and why)

### Decoded but intentionally not implemented

- **Firmware transfer** — payload type `0x18`. Captured shape: a multi-frame
  transfer of up to 4108 B per frame; first frame's body begins with the
  ASCII header `"Qoitech Arc firmware package"` followed by the filename
  (`"arc-fw.bin"`) and binary firmware bytes. benchctrl deliberately does
  not implement firmware upgrade (bricking risk on interrupted /
  malformed transfers).
- **Calibration internals** — calling `Arc.calibrate()` via the documented
  Otii TCP API fires zero wire commands (cap #41). The vendor's actual
  calibration flow lives somewhere else — probably the Desktop GUI's
  service-mode path or a USB control transfer outside the bulk
  endpoint. Out of scope.
- **Device-side battery emulation** — was not observed in captures of
  the workflows accessible to us (cap #40). benchctrl's emulator is a
  host-side ~100 Hz control loop instead; sub-ms ESR tracking would
  need device-side firmware access we don't have.

### Probably-not-wire-commands

- **`set_channel_samplerate`** — fails inside the Otii server's
  JavaScript layer with `"Cannot read properties of undefined"` before
  any bytes reach the device (cap #42). Most plausible interpretation:
  there is no wire command — the device streams at hardware-fixed
  native rates and "sample rate" in the GUI is a post-capture
  downsample.

### Niche / low-impact

- 8-byte payloads with `type=0x68` (6 occurrences across all captures)
  and `type=0x6A` (1 occurrence) — small control frames near response
  bursts. Likely flow-control housekeeping. Documented; not needed for
  measurement.
- UART log (`rx`) channel text decoding — the device emits parsed UART
  text on channel `0x17` in some envelope we haven't focused on. Out of
  scope for v0.2; tracked in ROADMAP.md.
