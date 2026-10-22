---
id: CORE-P0.7
commit: c42ba98
date: 2026-10-22
rig: benchpi
operator_role: ai-assistant
measurements:
  disconnected_run_duration_s: 60
  disconnected_run_unattended_s: 80
  disconnected_run_trips: 0
  disconnected_run_mv_min_V: 1.0000
  disconnected_run_mv_max_V: 1.0010
  disconnected_run_mv_samples: 23
  disconnected_run_mc_abs_max_A: 0.00000033
  wedge_trials: 5
  wedge_trips_run_watchdog: 5
  wedged_engine_trip_s_min: 15.482
  wedged_engine_trip_s_max: 15.498
  wedged_engine_trip_s_mean: 15.494
  wedge_bound_s: 16.0
---

Both measurements use `tools/p07_measure.py` at c42ba98. benchpi's agent was running
the `src/` of a70a28d, which is the same tree: `deploy/sync-board.sh --target benchpi
--check` from c42ba98 reported `IN SYNC — 126 files identical on both ends`. Agent
config: `deadman_s` 15, `heartbeat_s` 5, `rigol_dp2031` in `safe_stop_exempt`. Before
the runs the bench was idle: nothing armed, no trips, no workers, no recordings, no
device open.

## A run with its client gone completes

The Otii Arc was used with the DUT **unplugged** (project owner's choice), at 1.0 V,
a 10 mA limit, for 60 s, inside a 1.5 V / 50 mA / 120 s safety envelope. Run from the
WSL laptop against the real agent:

```
python tools/p07_measure.py disconnected-run --host 192.168.1.66 --port 9737 \
    --token <agent token> --duration-s 60 --voltage 1.0 --current-limit 0.01
```

The script claims the Arc, submits the run, and closes its socket. It then waits 80 s
(60 s run + 20 s settle) and reconnects as an observer. Result, exit 0:

- run `20261022T205257Z-p07-measure-8c6ce2`: `complete`, "all phases complete".
  The phase ran 20:53:03–20:54:03Z and ended on "duration elapsed".
- trips after submit: none. The observer's `output_enabled` read-back was `false`.

The report's `pass` is categorical, so the margin was checked separately in the run's
own `run.db` on benchpi:

- `mv`: 23 one-second metrics over 56.7 s, every one 1.0000–1.0010 V. The output was
  really sourcing the whole time with nobody connected.
- `mc`: between −0.27 µA and +0.33 µA, i.e. noise. Nothing was on the output.
- The chunk `data/chunk000.opensmu` is 2 188 824 B.

Odd: the Arc's `board_temp_C` read 72.2–80.4 °C during the run. That is the
instrument's own sensor with no load, so it isn't caused by this test. It's recorded
here in case it means something on a later run.

## Wedged-engine trip time

The script was copied to benchpi and run there with the agent's venv:

```
/home/rick/benchctrl/.venv/bin/python /tmp/p07_measure.py wedge \
    --trials 5 --deadman-s 15 --heartbeat-s 5 --duration-s 120
```

Each trial starts an agent **in the script's own process**, on 127.0.0.1, with a
**simulated** Arc. It blocks `RunEngine._check_rules`, so the engine stops touching its
`run:<id>` owner while its thread stays alive, and times the governor's trip from the
moment the wedge was entered. The real Arc and the live agent are not touched: wedging
the production agent would mean patching it in place. What this measures is the agent
code's deadman and watchdog timing on benchpi's CPU and Python (3.13.5).

| trial | latency (s) | reason | final state |
|---|---|---|---|
| 1 | 15.482 | run_watchdog | safe_stopped |
| 2 | 15.497 | run_watchdog | safe_stopped |
| 3 | 15.496 | run_watchdog | safe_stopped |
| 4 | 15.498 | run_watchdog | safe_stopped |
| 5 | 15.496 | run_watchdog | safe_stopped |

The bound is `deadman_s` to `deadman_s` + 1 s, i.e. 15–16 s. Every trial landed about
0.5 s past the deadman, which is the deadman thread's 0.5 s poll. The report's
`benchctrl_commit` is `null` only because the copy in `/tmp` is outside a git tree.

## Negative controls

`tests/test_p07_measure.py` covers these against the sim. A run aborted while
unattended, and a run whose device trips while unattended, both report `pass: false`.
`--interrupt-after` / `--interrupt abort|close` reproduce them on a rig. They were not
run on benchpi.
