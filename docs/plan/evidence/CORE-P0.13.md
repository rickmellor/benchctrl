---
id: CORE-P0.13
commit: 38a4eca
date: 2026-10-22
rig: github-actions
operator_role: ai-assistant
measurements:
  ci_run_id: 39853457809
  py39_ubuntu_job_id: 120117593812
  py39_ubuntu_attempt: 2
  py39_ubuntu_passed: 3126
  py39_ubuntu_failed: 0
  py39_ubuntu_skipped: 55
  py313_ubuntu_job_id: 120085429018
  py313_ubuntu_attempt: 1
  py313_ubuntu_passed: 3175
  py313_ubuntu_failed: 0
  py313_ubuntu_skipped: 6
---

GitHub Actions run 39853457809 on 38a4eca (PR #4, `feat/pro-phase0`), `pytest
(hardware-free)` on `ubuntu-latest`:

- **Python 3.13**, attempt 1, job 120085429018: 3175 passed, 6 skipped, 0 failed.
- **Python 3.9**, attempt 2, job 120117593812: 3126 passed, 55 skipped, 0 failed. The
  extra skips are the `mcp` tests: the `mcp` extra is limited to Python ≥ 3.10, because
  no `mcp<2` release supports 3.9.

Attempt 1 of the 3.9 job (120085429047) failed one test. In
`test_sim_loopback.py::test_ohmic_load_closes_the_loop` the measured mean was 2.04 %
under a 2 % tolerance. This branch changes neither that test nor the Otii sim. The
re-run on the same commit passed.

The Python 3.10, 3.11 and 3.13 Ubuntu legs were green on attempt 1. Ubuntu 3.12 failed
one observer-session test on each attempt, and it was a different test each time:
`test_dropping_a_session_removes_its_subscriber`, then
`test_an_observers_queue_is_shallow_and_droppable` with a `KeyError` on the session id.
Both look for the session's event subscriber straight after connecting. The agent sends
the welcome in `_authenticate` before `handle()` calls `register_session`, so a fast
client can look before the subscriber exists. Master has the same ordering. This is
not P0.13's scope; it is recorded here so nobody mistakes it for a 3.12 regression.

## Not covered

- **macOS.** Every macOS leg stays in the test step far longer than Ubuntu's ~35 min.
  On master's run 38615760131 (2026-10-15), four of the five legs ran until GitHub's
  6-hour limit and were cancelled, and macOS 3.9 failed at install. The workflow sets
  no `timeout-minutes`. This evidence covers Ubuntu only.
- **Windows.** Collection fails on `termios`, which is Unix-only. It fails the same
  way on master.
