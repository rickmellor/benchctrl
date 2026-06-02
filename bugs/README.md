# Vendor firmware bug reports

Bench-discovered firmware bugs and behavioural anomalies observed during
benchctrl driver development against specific units. Each report is a
self-contained document with reproduction steps, expected vs observed
behaviour, firmware versions tested, and any workaround.

Reports here are intended for submission to the relevant vendor's
technical support. Tone is factual and reproducible — these are
engineering bug reports, not commentary.

## Index

| File | Device | Firmware | Severity | Title |
|---|---|---|---|---|
| [`dl3031a-01-func-mode-stuck-requires-power-cycle.md`](dl3031a-01-func-mode-stuck-requires-power-cycle.md) | Rigol DL3031A | 00.01.05.00.01 | High | `:SOURce:FUNCtion:MODE FIXed` silently no-ops once device enters LIST / WAV / BATTery / OCP / OPP mode — only a power cycle restores FIX mode |
| [`dl3031a-02-func-mode-query-returns-incorrect-state.md`](dl3031a-02-func-mode-query-returns-incorrect-state.md) | Rigol DL3031A | 00.01.05.00.01 | High | `:SOURce:FUNCtion:MODE?` returns `WAV` after `*RST` and after power-cycle even though the device is operating in FIX mode |
| [`dl3031a-03-measure-current-returns-zero-cr-mode-low-current.md`](dl3031a-03-measure-current-returns-zero-cr-mode-low-current.md) | Rigol DL3031A | 00.01.05.00.01 | Medium | `:MEASure:CURRent:DC?` returns 0 in CR mode at currents below ~50 mA while the load is actually sinking current |
| [`dp2031-01-outp-pair-query-returns-stale-off-during-transition.md`](dp2031-01-outp-pair-query-returns-stale-off-during-transition.md) | Rigol DP2031 | 01.00.01.00.16 | Medium | `:OUTPut:PAIR?` returns `OFF` for ≥ 1 s after `:OUTPut:PAIR PARallel` write, before the mode transition completes |
| [`dp2031-02-outp-pair-state-survives-rst.md`](dp2031-02-outp-pair-state-survives-rst.md) | Rigol DP2031 | 01.00.01.00.16 | High | `:OUTPut:PAIR` state (SERies / PARallel internal-channel-tie) survives `*RST`, contrary to factory-default expectations |
| [`dp2031-03-ovp-clear-form-divergence.md`](dp2031-03-ovp-clear-form-divergence.md) | Rigol DP2031 | 01.00.01.00.16 | Low | `:OUTPut:OVP:CLEar` and `:SOURce<n>:VOLTage:PROTection:CLEar` are documented as aliases but behave differently regarding output re-enable |

## Reporter context

- **Bench setup**: Pure-Python USB-TMC control via pyvisa, NI-VISA backend on Windows 11.
- **Driver code**: Open-source benchctrl library (`benchctrl.drivers.rigol_dl3031a`, `benchctrl.drivers.rigol_dp2031`). Each report links to the affected driver method for context.
- **Reproduction environment**: All reports include the exact pyvisa one-liners to reproduce on a bench. No special test fixtures required.
- **Unit identification**: Specific serial numbers and firmware versions are listed in each report's metadata so the vendor can correlate against known firmware revisions.
