<!--
Thanks for the PR. Walk through the checklist below — most items are
one-liners.
-->

## What this changes

<!-- 1-3 sentences. -->

## Why

<!--
Link the issue / discussion / CHANGELOG context that motivated this.
"Why" matters more than "what" — the diff shows what.
-->

## Checklist

- [ ] Tests added or updated. Hardware-free where possible; hardware-marked if it needs an instrument.
- [ ] `pytest -m "not hardware"` passes locally.
- [ ] `ruff check` is clean.
- [ ] If you added a public SDK method, an MCP tool was added in the same PR (SDK ↔ MCP parity — see CONTRIBUTING.md § 1).
- [ ] If you found a hardware or firmware quirk, it's documented in `KNOWN_LIMITATIONS.md` and the driver rejects the known-bad case with a clear error.
- [ ] `CHANGELOG.md` entry added under the current `Unreleased` / active version header.
- [ ] If user-facing behavior changed, the relevant doc in `docs/` is updated. Likely candidates:
  - `docs/getting_started.md` (tutorial)
  - `docs/api_reference.md` (API surface)
  - `docs/battery.md` / `docs/bench.md` / `docs/mcp.md` (subsystem-specific)
  - `validation/README.md` (harness behavior)

## Hardware tested

<!--
If your change touches anything that talks to a device, say what you
tested it against. "Only mocks" is fine for pure refactors but flag it
explicitly.
-->

- [ ] Arc / Arc Pro
- [ ] Rigol DL3031A
- [ ] Eastwood QR10x
- [ ] Mock only (refactor / documentation)

## Anything reviewers should focus on

<!--
Optional. Specific files, edge cases, alternative approaches you
considered, etc.
-->
