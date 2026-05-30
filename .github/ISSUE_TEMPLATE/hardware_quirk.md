---
name: Hardware / firmware quirk
about: You found a device behavior that contradicts the manufacturer's documentation
labels: hardware-quirk
---

## What the manual says

<!--
Quote from the manufacturer's docs. Include the document name, version,
and page / section if you have it.
-->

## What actually happens

<!--
Bench-verified behavior. Include the SCPI command / driver call you
used and what the device did.
-->

## Repro

```python
# Minimal Python snippet that exercises the quirk against real hardware.
```

## Where the workaround should live

- [ ] Driver should reject the bad input at the SDK boundary
- [ ] Driver should silently translate to a workaround behavior
- [ ] User should be warned but allowed to proceed
- [ ] Document only — no behavior change

## Environment

- Device: <!-- e.g. Rigol DL3031A -->
- Firmware: <!-- e.g. 00.01.05.00.01 -->
- pyvisa backend (if applicable):
- OpenSMU version:

<!--
If you want to send a PR documenting this:
1. Add a section under the appropriate header in KNOWN_LIMITATIONS.md
2. If the driver should reject: implement the check + a clear error
   message that points to the workaround
3. Add a test that exercises the workaround path
4. CHANGELOG entry under "Discovered"

Examples of this pattern: KNOWN_LIMITATIONS § F-1 (DL3031A STEP=4),
§ F-4 (manual misreads compensated by the driver).
-->
