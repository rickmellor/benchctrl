# OpenSMU build progress

Live status log so anyone (human or AI) picking this up mid-flight knows
exactly where it is. Updated after every milestone; latest entry on top.

## Status snapshot

- **Phase**: scaffolding
- **Last commit**: (none yet)
- **Last touched**: design doc + scaffold
- **Hardware**: Arc Pro on COM6, output off, nothing connected

## Where to look first if you're resuming

1. `docs/official_api_inventory.md` — full catalog of every method in
   the official `otii_tcp_client` we are replicating
2. `docs/design.md` — architecture + API style decisions
3. `ROADMAP.md` — features explicitly deferred + why
4. `TEST_PLAN.md` — what should be exercised
5. `VALIDATION_REPORT.md` — what was actually validated against hardware
6. `src/opensmu/` — the implementation

## Phase ledger

| # | Phase | Status | Notes |
|---|---|---|---|
| 1 | Survey official API | done | inventory in `docs/official_api_inventory.md` |
| 2 | Design doc | in progress | `docs/design.md` |
| 3 | Scaffold package | in progress | pyproject + src layout + git init |
| 4 | Transport + protocol | pending | |
| 5 | Device class | pending | |
| 6 | Recording + samples | pending | |
| 7 | Test plan + suite | pending | |
| 8 | Validation | pending | |
| 9 | Documentation | pending | |
| 10 | Polish + handoff | pending | |

## Known blockers / open questions

(none yet)

## Quick resume commands

```powershell
cd C:\Users\rickm\Desktop\opensmu
git log --oneline -20
python -m pytest tests/ -m "not hardware"     # hardware-free
python -m pytest tests/ -m hardware            # requires Arc Pro on COM6
```
