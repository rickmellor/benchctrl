# Benchctrl Pro: core plan

The benchctrl side of [Benchctrl Pro](https://generacet.atlassian.net/wiki/spaces/AS1/pages/2266825026).
The Pro service's own items (`PRO-…`) live in its repo; the IDs here are the same ones the
Pro implementation plan uses, so a `PRO-` item can name the `CORE-` item it needs.

## How this file is read

- Every deliverable is one ID'd item: `- **CORE-P0.4** text … \`verify: test\``.
  - `verify: test`: a test marked `@pytest.mark.plan("CORE-P0.4")` ran and passed.
  - `verify: evidence`: a bench measurement in `docs/plan/evidence/CORE-P0.4.md`
    (see [evidence/README.md](evidence/README.md)).
  - `verify: test+evidence`: both.
- Each phase has a `plan:phase` marker, `planned | active | closed`. An item belongs to the
  phase its ID names. **Closing a phase is a claim the build checks:** every item in it
  must be `done`.
- **Status is never written here.** `tools/plancheck.py` derives it from this build's test
  results and the evidence files (`todo | in-progress | done | regressed`); the `plan-status`
  CI job publishes it as a job summary, an artifact and a PR comment. A skipped test is not
  a pass. To see it locally after a test run:

  ```
  pytest -m "not hardware" --junitxml=junit.xml
  python tools/plancheck.py --plan docs/plan/pro-core.md --tests tests \
      --junit junit.xml --evidence docs/plan/evidence
  ```

- A change that alters scope edits this file in the same PR.

## Phase 0: core hardening

<!-- plan:phase id=CORE-P0 state=active -->

Useful standalone: these fix real gaps in benchctrl today and come before any Pro code.
With no Pro configuration the agent behaves as before, except that a safety fix may trip
*earlier*, never later; each such change has a CHANGELOG entry.

- **CORE-P0.1** `tools/plancheck.py` (stdlib-only) with fixture tests covering each gate. Each gate has a fixture that only it rejects. `verify: test`
- **CORE-P0.2** Register the `plan` pytest marker, add a conftest hook that writes it to junit `<property name="plan">`, and add a `plan-status` CI job that publishes the artifact and the PR comment. `verify: test`
- **CORE-P0.3** Commit `docs/plan/pro-core.md` and tag existing tests that already prove planned behaviour, so the first report starts from the real baseline. `verify: test`
- **CORE-P0.4** A failing test reproduces the unattended-run `HEARTBEAT_LOST` trip. Then the fix: the run engine touches its own `run:<id>` owner from its loop. `verify: test`
- **CORE-P0.5** Per-owner deadman. A arms, B keeps chatting, A goes silent → only A's device trips. `verify: test`
- **CORE-P0.6** A wedged run engine trips `RUN_WATCHDOG` within `deadman_s` + 1 s (sim, injected clock); `max_duration_s` bounds runs. `verify: test`
- **CORE-P0.7** On benchpi, a run with the client disconnected completes, and the wedged-engine trip time is measured and recorded. `verify: evidence`
- **CORE-P0.8** Claim gating:
  - `agent.open`/`agent.close` are refused across claims;
  - `run.abort` is owner/holder only, or `force` + reason, which raises an `alarm` event (`critical` stays reserved for trips);
  - `blob.release`/`rec.release` are owner-only;
  - structured ERR `code`, which `cli.py` matches on, with the text match kept as a fallback.

  `verify: test`
- **CORE-P0.9** Session ids are `s-<agent6>-<boot6>-N`, unique across restarts. `verify: test`
- **CORE-P0.10** Handshake negotiation with `v_min`/`v_max`/`caps`. Old client ↔ new agent and new client ↔ old agent both interoperate. `verify: test`
- **CORE-P0.11** `DeviceRegistry.describe()` reports serial and binding. `verify: test`
- **CORE-P0.12** Fix the `docs/runs.md` unattended-run promise. KNOWN_LIMITATIONS states that the deadman is best-effort and that a hardware interlock is the guarantee. CHANGELOG entries cover each safety change. `verify: evidence`
- **CORE-P0.13** The full suite passes on Python 3.9 and 3.13. `verify: evidence`: a CI run whose 3.9 and 3.13 jobs are green, cited by run id. A single test can't show that a whole job passed.

## Phase 1a: uplink and enrollment

<!-- plan:phase id=CORE-P1a state=planned -->

- **CORE-P1a.1** `agent/uplink.py`:
  - TLS ≥ 1.2;
  - backoff with jitter;
  - a droppable event-bus subscriber;
  - the fixed `pro.*` allowlist, with unknown methods refused.

  `verify: test`
- **CORE-P1a.2** `benchctrl-agent pro enroll` writes `/etc/benchctrl/pro.json` with mode 0600. `verify: test`
- **CORE-P1a.3** With no `pro.json`, the agent opens no outbound socket. `verify: test`
- **CORE-P1a.4** `up.register` labels tailnet endpoints, and `listen_tailnet_only` binds to loopback + the tailnet address only. `verify: test`
- **CORE-P1a.5** `deploy/install-pro.sh` joins the tailnet and enrolls the bench. `verify: evidence`

## Phase 1b: leases and enforcement

<!-- plan:phase id=CORE-P1b state=planned -->

- **CORE-P1b.1** `agent/leases.py` verifies:
  - the token's Ed25519 signature, against a pinned `kid`;
  - the `cnf` binding;
  - the agent id;
  - nbf/exp, with ±30 s skew;
  - revocation;
  - the generation.

  Revocations persist to disk. `verify: test`
- **CORE-P1b.2** The `lease-ed25519` challenge. A replayed token without the client's private key is refused. `verify: test`
- **CORE-P1b.3** Managed mode: no lease, no claim. Forged and wrong-agent tokens are refused. `verify: test`
- **CORE-P1b.4** At expiry or revoke the order is: abort runs → `trip(LEASE_*)` → drop claims → downgrade sessions → `EVT lease_ended` → report upstream. Warnings go out at T−5 and T−1 min. `verify: test`
- **CORE-P1b.5** Local caps (`pro.max_tiers`, mains). A grant beyond them is refused. `verify: test`
- **CORE-P1b.6** Client side:
  - `RemoteClient(lease=, lease_key=)`, with a per-reservation client key pair;
  - the lease env vars (`BENCHCTRL_LEASE`, `BENCHCTRL_LEASE_KEY`). `benchctrl reserve` never passes `BENCHCTRL_PRO_KEY` to its child (design §8.1);
  - `--record lease.json`;
  - `benchctrl reserve` / `benchctrl lease`;
  - exit codes 120–125;
  - release in `finally` and on SIGTERM.

  `verify: test`
- **CORE-P1b.7** Expiry while armed: the output is safe within 1 s of `exp`. Sim test, plus a real DP2031 measurement (DUT rail stays de-energised unless authorised). `verify: test+evidence`
- **CORE-P1b.8** Agent restart mid-lease: the lease survives and revocations persist. `verify: test`
- **CORE-P1b.9** `benchctrl/_vendor/ed25519.py` (design §7.5, D10):
  - passes the RFC 8032 test vectors;
  - rejects a non-canonical `S`, undecodable points, a wrong `kid` and a retired key;
  - is pinned and hash-checked.

  Verify time on benchpi is measured and recorded. `verify: test+evidence`
- **CORE-P1b.10** benchctrl MCP tools `bench_find`, `bench_reserve`, `bench_release`, `bench_status` and `bench_why_waiting` (design §8.1). They are read-only when no Pro key is in the environment. `verify: test`

## Phase 1c: remote CI

<!-- plan:phase id=CORE-P1c state=planned -->

- **CORE-P1c.1** `--connect auto|lan|tailnet`, with a 1.5 s LAN probe before falling back to the tailnet. `verify: test`

## Phase 2: capability match and fixtures

<!-- plan:phase id=CORE-P2 state=planned -->

- **CORE-P2.1** Device instance keys (`driver#name`; a bare key means `#0`) across config, CLI, MCP and proxies, behind a compatibility shim. `verify: test`
- **CORE-P2.2** Binding and calibration come from agent config into `describe()`. `verify: test`
- **CORE-P2.3** Per-device lease scope and per-outlet PDU claims. `verify: test`
- **CORE-P2.4** Optional TLS on the agent listener. `verify: test`

## Phase 3: dataflow

<!-- plan:phase id=CORE-P3 state=planned -->

- **CORE-P3.1** `stream.export` (recording/blob, and live SAMPLES with agent-side decimation). `verify: test`
- **CORE-P3.2** `stream.import` with an underrun policy. An import counts as an arming owner. `verify: test`
