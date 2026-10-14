# Plan evidence

One file per plan item whose `verify:` includes `evidence`: a measurement on real
hardware that CI can't take. The file is named after the item, `CORE-P0.7.md`, and starts
with front matter that `tools/plancheck.py` checks (a malformed file fails the build):

```markdown
---
id: CORE-P0.7
commit: 1a2b3c4            # the commit that was measured; must be an ancestor of HEAD
date: 2026-10-14
rig: benchpi               # benchpi | unoq | ...
operator_role: bench-engineer   # a ROLE label, never a name or an email address
measurements:
  wedged_engine_trip_s: 15.6    # numbers, not prose, so a later run can be compared
  trips: 1
---

How it was measured, the exact commands, and anything odd.
```

Record the margin, not just the verdict: if the item says "within 16 s", record the
seconds it actually took.
