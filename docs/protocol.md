# Wire protocol

What's on the USB cable between the host and an Arc / Arc Pro. This
documents the reverse-engineering result OpenSMU's implementation
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

### START_RECORDING

A variable-length payload that selects channels and rates:

```
[8 B header: 69 83 2a ff 17 02 00 00]
[N channel records]
[8 B sentinel: 17 00 00 00 00 00 00 00]
```

Channel records have two sizes based on subtype:

| Subtype | Size | Layout |
|---|---|---|
| 1 (1 kHz nominal) | 12 B | `[id:u16][1:u16][rate:u32][0:u32]` |
| 4 (4 kHz nominal) | 24 B | `[id:u16][4:u16][rate:u32][16 B zero blob]` |

Enabling `mc` (id `0x00`) auto-includes `mp` (id `0x06`); enabling `ac`
(`0x02`) auto-includes `ap` (`0x07`). The host mirrors this so the
payload matches what the vendor stack sends.

### STOP_RECORDING — type `0x78` (16 B)

```
[seq:u32] [78 00 00 00] [target_wire_id:u32] [0:u32]
```

`target_wire_id` is one of the channels in the currently active
recording (highest in the set, per vendor capture).

### Recording cleanup — type `0x7C` (8 B)

```
[seq:u32] [7C 00 00 00]
```

Sent after `STOP_RECORDING` to release the recording context.

## Inbound (device → host) payloads

### Sample record (12 B, inside a frame payload)

```
[02 00 08 00] [chan:u32 LE] [value:f32 LE]
```

One frame can carry many sample records back-to-back. The `chan` field
is the channel wire id (see table below).

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

## Things we have *not* decoded

- Full-rate (1 kHz / 4 kHz) streaming command
- Battery emulation: profile upload, SoC commands, profiling enable
- Calibration: trigger and completion notification
- Firmware upgrade: bootloader entry + image upload (deferred indefinitely)
- `set_channel_samplerate` wire command (Otii server bug blocked capture)
- UART log channel record format (probably type `0x0003`)
- `set_power_regulation` wire command
- `set_tx` / `get_rx` (treating TX/RX as GPO/GPI)
