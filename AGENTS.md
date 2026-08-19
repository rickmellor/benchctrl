# Working on benchctrl with AI coding agents

This file is for AI agents (and the humans directing them) doing work in
this repo. `CONTRIBUTING.md` says what the code must look like; this says
**how to get there** — specifically, the sub-agent structure to use when
adding a new instrument integration.

Read `CONTRIBUTING.md`'s five conventions first. Nothing here overrides
them.

---

## Why this pattern exists

Instrument drivers fail in a particular way: the code runs, the tests
pass, and the numbers are wrong. Every failure mode below was hit for
real in this repo.

- **Vendor manuals cover families, not models.** The SDM4000A remote
  manual documents the SDM4045A/4055A/4065A together. The SDM4065A has a
  1 MΩ resistance range; the SDM4055A has 2 MΩ. It accepts six NPLC
  values; the SDM4055A accepts three. A constant read from the wrong
  column produces a driver that works, reports plausible numbers, and is
  wrong. Nothing in a test suite catches this — only the manual does.
- **A simulator built from the same misreading agrees with the driver.**
  Sim and driver written together share their author's assumptions, so
  the tests pass and prove nothing. The QR10x sim answered `DEV.PROD`
  with a model code for months; the field is a YYYYMMDD date, and only
  real hardware revealed it.
- **"Accurate enough" is not an opinion.** Cross-validating two
  instruments needs an uncertainty budget from the datasheets, or the
  pass/fail threshold is a number somebody made up. 2-wire resistance
  without a null carries ~0.2 Ω of lead error — 5x larger than the
  38 mΩ offset the QR10x validation was trying to resolve. A test with a
  guessed tolerance would have "passed" while measuring nothing.
- **Idiomatic integration is not guessable from one example.** A driver
  can work perfectly locally and be invisible to `session.resolve()`,
  the remote proxy, and sim mode — five registries, all easy to miss.

The common thread: these are **research failures, not coding failures**.
So the structure below puts research in dedicated agents with narrow
briefs and a hard requirement to cite sources.

---

## The three sub-agents

Spawn all three **before writing driver code**, in parallel. They read;
you write. None of them should edit files — advisory only, so their
output can be judged rather than silently absorbed.

Give each one the extracted manual text (see *Preparing the sources*)
and require **a section or line-number citation for every claim**. An
uncited spec number is a guess wearing a suit.

### 1. The spec agent — "what does this instrument actually do?"

**Brief:** extract an authoritative command reference from the vendor
manuals.

Must cover:
- Every command the driver will send, with its exact parameter set.
- **Model-split values, called out explicitly.** Tell the agent which
  model you have and instruct it to *reject* values belonging to sibling
  models — and to report the sibling's value too, so the driver comment
  can record what it was chosen over.
- The acquisition model: one-shot (`MEAS?`) vs configure-then-trigger
  (`CONF` + `READ?`) vs `INIT` + `FETCh?`. **What each one resets.**
- Response formats: quoting, scientific notation, comma-separated
  lists, short-vs-long mnemonics on readback.
- Error and out-of-range sentinels.
- Interactions and side effects between settings.
- Ambiguities in the manual, flagged as ambiguous rather than resolved
  by guessing.

The last three are where the value is. Two examples from the SDM4065A,
both of which would have shipped as silent wrong-number bugs:

- `MEASure:RES?` is `CONFigure` + `READ?` in one command, and
  `CONFigure` **clears the null and resets the range**. So
  `null_now()` followed by `measure_resistance()` silently discards the
  null — the API's most natural call sequence is the broken one.
- Enabling `NULL:STATe` **arms `NULL:VALue:AUTO`**, which makes the
  instrument overwrite the offset with its own next reading. So
  `set_null_value(x)` then `set_null(True)` nulls by something other
  than `x`. State must go first, value second.

### 2. The accuracy agent — "what can this measurement actually prove?"

**Brief:** build the measurement uncertainty budget. Required whenever
the work involves validating one instrument against another, or any
claim of the form "the reading matches".

Must produce:
- Per-range accuracy specs (% of reading + % of range), converted to
  **absolute units at the actual operating point**. "0.010% + 0.005%"
  is not actionable; "±14 mΩ at 100 Ω on the 200 Ω range" is.
- A direct answer to *can this setup resolve the effect being measured?*
  — with the arithmetic shown. If it cannot, that is the finding, and it
  is more valuable than a threshold that hides it.
- Whether a **differential** protocol (a setpoint ladder, checking
  tracked deltas) resolves what an absolute one cannot. Gain and offset
  errors largely cancel in a delta, so this often rescues a
  cross-validation that absolute accuracy cannot support.
- Noise floor vs accuracy, kept separate. Averaging N readings beats
  down noise and does **nothing** for the accuracy spec. Conflating them
  produces false confidence from a big N.
- Preconditions: warm-up, autozero, temperature range for the stated
  spec, thermal EMF, self-heating, TCR over a plausible excursion.
- **A derived pass/fail criterion**, stated as an inequality with its
  derivation. Never a round number that looks reasonable.

Instruct it to say **"not specified in the manual"** rather than
estimating. Knowing which parts of the budget are documented and which
are assumed is the whole point.

### 3. The integration agent — "is this wired in the way this repo wires things?"

**Brief:** ensure the new driver follows existing repo patterns and
**invents no new modality**. This one is about the codebase, not the
instrument.

Must produce:
- A **full trace of the closest existing device**: every file that
  mentions it, in dependency order, with `file:line`. `grep -rn` for the
  device key and class name across *everything* — src, tests, docs,
  deploy. This is what makes the checklist complete instead of
  plausible.
- A file-by-file checklist for the new device, with surrounding code
  quoted so style can be matched, and ordering-sensitive steps flagged.
- Anti-patterns specific to this addition: a Protocol invented for a
  single instance (`CONTRIBUTING.md` rule 3 — wait for the second),
  units diverging from house style, a method-naming scheme that no other
  driver uses, an exception hierarchy the wire codec cannot carry.
- The remote round-trip requirements, concretely: which return types
  survive the codec, whether custom exception `__init__` signatures
  reconstruct, what happens to a `list[float]` or a `tuple`.
- A verification recipe: the commands that prove the integration is
  complete, including any test that enumerates devices and so fails on a
  missed registration.

Its most useful output is usually the **anti-pattern list**, because a
first implementation of anything tends to invent an abstraction the repo
already has a convention for.

---

## Preparing the sources

Vendor PDFs are gitignored (`references/`, `*.pdf`) — they are
copyrighted, large, and binary. Drivers cite the specific facts they
depend on in comments, so the code stands alone without the PDF.

Extract text to `/tmp` and hand the agents the paths:

```bash
pdftotext -layout references/SDM4000A_RemoteManual.pdf /tmp/sdm_remote.txt
pdftotext -layout references/SDM4000A_Datasheet.pdf    /tmp/sdm_ds.txt
pdftotext -layout references/SDM4000A_UserManual.pdf   /tmp/sdm_user.txt
```

`-layout` matters: it preserves table columns, and the model-split
values live in tables.

**Verify the load-bearing constants yourself.** The agents are a wide
net, not an oracle. Grep the extracted text for anything a wrong value
would make silently incorrect — range lists, discrete parameter sets,
sentinels — and read the surrounding paragraph.

---

## Sequence

1. **Clean the branch first.** Merge and push what's outstanding, then
   branch. Instrument work involves hardware runs; a dirty tree makes
   "which code produced this reading?" unanswerable.
2. **Extract the manuals** to `/tmp`.
3. **Spawn all three agents in parallel.** They take a while; don't wait
   idle.
4. **Verify the model-split constants by hand** while they run.
5. **Write the driver**, then the simulator, then the tests. The
   simulator must model the *quirks* — sentinels, side effects, reset
   behaviour — or the tests only prove the sim and driver share
   assumptions.
6. **Wire the integration** per the integration agent's checklist. All
   five registries; verify each loads.
7. **Run the sim-backed suite**, then mutation-test the claims (below).
8. **Validate on hardware** using the accuracy agent's protocol and its
   derived threshold.
9. **Document**: `CHANGELOG.md`, `KNOWN_LIMITATIONS.md` for quirks,
   `docs/`.

If the brief changes mid-flight — new hardware capability, a lifted
constraint — **send the affected agent an update** rather than
re-deriving its work yourself. When sense leads became available for the
SDM4065A validation, 4-wire went from impossible to primary, and the
whole uncertainty budget changed with it.

---

## Judging agent output

Agent reports are evidence, not conclusions.

- **No citation, no claim.** Spot-check citations against the source.
- **Cross-check the agents against each other.** The spec agent's range
  list and the accuracy agent's per-range table must agree.
- **A sim/driver agreement proves nothing on its own** when both came
  from the same reading of the manual. The independent check is
  hardware, or the manual read a second time with fresh eyes.
- **Prefer a reported blocker to a reported success.** "2-wire cannot
  resolve this" is a real finding. Treat a clean bill of health on a
  hard question as unfinished.
- **Watch for confident wrong answers on model splits.** These are the
  highest-cost errors and the easiest to state fluently.

---

## Proving tests bite

A passing test is not evidence; a test that fails when the code breaks
is. For anything load-bearing — a validation threshold, a quirk
workaround, a safety guard — **mutate the source and confirm the test
fails.**

Rules learned the hard way:
- **Assert the test count.** `rc=0` with zero tests collected looks
  exactly like a pass.
- **A surviving mutant means the test does not prove its docstring.**
  Not "the test is weak" — the claim is false and must be rewritten or
  withdrawn. A `_POLL_S` mutation from 5 ms to 100 ms survived here; the
  real threshold was ~200 ms, and the source comment was overclaiming.
- **Check for equivalent mutants** before writing a test to kill one.
- **Hold mutated files outside the module** so a partial revert cannot
  leave the mutation in place.
- **Behavioural assertions may not discriminate constants.** If a test
  cannot tell 5 ms from 100 ms, add a direct bound on the constant and
  say why in a comment.

---

## Hardware validation

- **Never energise an output** without knowing what is attached. Default
  to `mode: idle`.
- **A physical measurement no simulator produced is the strongest
  evidence available.** The QR10x's 38 mΩ offset from its 100.0 Ω
  setpoint was worth more than the whole sim suite: no simulator
  invented it.
- **Repeat the open/close cycle** and check readings match. That is what
  proves `close()` actually released the device.
- **Record the identity of the unit under test** — model, serial,
  firmware — alongside the readings, in the changelog or limitations
  entry. A measurement without a provenance is an anecdote.
- **Privileged operations on a remote board are the operator's to run.**
  Ask; do not find a way around the prompt. A route that bypasses a gate
  is not the same as satisfying it.
