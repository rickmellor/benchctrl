# DP2031: `:OUTPut:PAIR?` returns `OFF` for ≥ 1 s after `:OUTPut:PAIR PARallel` write

## Metadata

| Field | Value |
|---|---|
| Device | Rigol DP2031 triple-output programmable DC power supply |
| Firmware version | 01.00.01.00.16 |
| Serial observed | DP2A243500269 |
| Transport | USB-TMC (USB0::0x1AB1::0xA4A8::DP2A243500269::INSTR) |
| Discovered | 2026-05-30 (bench-verified across multiple sessions) |
| Severity | **Medium** — status query returns misleading value during mode transition |

## Summary

After `:OUTPut:PAIR PARallel` is written, the `:OUTPut:PAIR?` query
returns `OFF` for at least 1 second — sometimes longer — even though
the mode transition is in progress and ultimately takes effect. There
is no SCPI error queued during this window. After the (undocumented)
settling period, the query begins returning `PARALLEL` as expected.

This makes the query indistinguishable from a write that was rejected.
A host that does

```python
write(":OUTPut:PAIR PARallel"); time.sleep(0.3); read(":OUTPut:PAIR?")
```

will see `OFF` and incorrectly conclude the mode change failed.

The same defect does NOT appear with `:OUTPut:PAIR SERies` — the
SERies query reflects the mode within ~300 ms.

## Reproduction

```python
import pyvisa, time
rm = pyvisa.ResourceManager()
psu = rm.open_resource("USB0::0x1AB1::0xA4A8::DP2A243500269::INSTR")
psu.timeout = 5000
psu.read_termination = "\n"
psu.write_termination = "\n"

psu.write("*RST"); psu.write("*CLS")
time.sleep(0.5)
print("Initial PAIR:", psu.query(":OUTPut:PAIR?").strip())  # "OFF"

# Compare SERies vs PARallel behaviour:
print("\nSERies transition:")
psu.write(":OUTPut:PAIR SERies")
for t_ms in (100, 300, 500, 1000, 1500, 2000):
    time.sleep(t_ms / 1000.0 - (sum([100, 300, 500, 1000, 1500, 2000][:[100, 300, 500, 1000, 1500, 2000].index(t_ms)]) / 1000.0))
    print(f"  t+{t_ms} ms: {psu.query(':OUTPut:PAIR?').strip()}")

psu.write(":OUTPut:PAIR OFF")
time.sleep(2.0)

print("\nPARallel transition:")
psu.write(":OUTPut:PAIR PARallel")
for t_ms in (100, 300, 500, 1000, 1500, 2000, 3000):
    time.sleep(0.1)  # poll roughly every 100 ms
    print(f"  ~t+{t_ms} ms: {psu.query(':OUTPut:PAIR?').strip()}")

psu.write(":OUTPut:PAIR OFF")
time.sleep(2.0)

# Observed:
#   Initial PAIR: OFF
#
#   SERies transition:
#     t+100 ms : SERIES
#     t+300 ms : SERIES
#     t+500 ms : SERIES
#     ...
#   (immediate)
#
#   PARallel transition:
#     ~t+100 ms : OFF       ← stale
#     ~t+300 ms : OFF       ← stale
#     ~t+500 ms : OFF       ← stale
#     ~t+1000 ms: OFF       ← stale
#     ~t+1500 ms: PARALLEL  ← finally updates
```

## Expected behaviour

Either:
1. `:OUTPut:PAIR?` should return the target mode (e.g. `PARALLEL`)
   immediately after the write is accepted, with the device internally
   completing the transition. The query reports the requested state,
   not the current physical state of the relays.
2. Or, if the device legitimately cannot report `PARALLEL` until the
   transition completes, the query should return some "in transition"
   indicator (e.g. queue an `-285,"Program currently running"` error)
   rather than the misleading `OFF` value.

Comparable: the `:OUTPut:PAIR SERies` query behaves per (1) — reports
the target mode immediately. The PARallel case should be consistent.

## Actual behaviour

`:OUTPut:PAIR?` returns `OFF` for ≥ 1 second (sometimes up to ~1.5 s)
after a `:OUTPut:PAIR PARallel` write. After the transition completes,
the query returns `PARALLEL` correctly. No SCPI error is queued during
the window.

## Impact

- Hosts can't reliably verify a `PARallel`-mode write took effect via
  the query. We initially concluded — incorrectly — that the PARallel
  mode was silently rejected on this firmware because the query
  returned `OFF` after the write.
- Test code that polls `:OUTPut:PAIR?` immediately after the write to
  decide whether to proceed will mistakenly fall through to "mode
  rejected" error handling.
- Real wall-time impact: the transition takes ≥ 1 s, which is
  significantly longer than the data-sheet's quoted < 10 ms command
  processing time. The query semantics during this window should
  reflect that the device is mid-transition, not that the request was
  rejected.

## Workaround

- Wait ≥ 2 s after writing `:OUTPut:PAIR PARallel` before polling
  `:OUTPut:PAIR?`.
- Or, retry the read with exponential backoff until it returns
  `PARALLEL` (with a long-enough overall timeout to allow for the
  documented (?) transition time).

## Related driver reference

- `benchctrl/drivers/rigol_dp2031/driver.py` — `set_channel_pair()`,
  `get_channel_pair()`
- `KNOWN_LIMITATIONS.md § F-3.5` (DP2031 bench-discovered quirks)

## Notes for the vendor

- Documenting the actual PARallel-mode transition time in the
  programming guide would help host code apply the correct settle.
- A simple fix on the device side would be to have the query reflect
  the requested mode immediately, matching the SERies-write behaviour.
