# benchctrl user guide

**Draft.** These pages are the external user documentation, written to be
lifted into a wiki. Each file is one wiki page; the directory is the page
tree. Nothing here is API reference — that lives in
[`docs/api_reference.md`](../api_reference.md) and stays where it is.

benchctrl turns a bench of instruments into one programmable surface: a Python
API, a shell command, or a set of tools an AI agent can call. The same code
drives real hardware, hardware on another machine, or simulators with no
instruments attached at all.

## Start here

| Page | Read it when |
|---|---|
| [Overview](overview.md) | You want to know what this is and whether it fits your problem |
| [Theory of operation](theory-of-operation.md) | You want to know *how* it works before trusting it with your hardware |
| [Supported equipment](equipment-matrix.md) | You want to know if your instrument is supported, and what it is good for |
| [Installation](installation.md) | You are ready to install it |

## Using it

| Page | What it covers |
|---|---|
| [Local and remote mode](local-vs-remote.md) | Running the bench on your machine vs. on a dedicated host — and the honest trade-offs |
| [Setting up a bench host](bench-host-setup.md) | Standing up a small Linux board as a permanent bench controller |
| [The bench display](bench-display.md) | The read-only status panel on the bench host's own monitor |
| [Driving it from an AI agent](agent-harness.md) | Wiring the bench into Claude Code or any MCP client |
| [Command line](../cli.md) | Every instrument function as a shell command |

## Worked examples

Each is a complete task, start to finish, with the reasoning behind the
choices — not a code fragment.

| Example | The job |
|---|---|
| [Bringing up a board](examples/board-bringup.md) | Power a new board, hold it in reset, release it, watch what it draws |
| [Power consumption characterization](examples/power-characterization.md) | Turn "how long will it run on a battery" into a measured answer |
| [Battery emulation](examples/battery-emulation.md) | Make a DUT believe it is running on a specific cell, at a specific temperature and state of charge |
| [Sleep and duty-cycle current](examples/sleep-current.md) | Measure the microamps that decide whether a product lasts a year |
| [Power-cycle and cold-boot testing](examples/power-cycling.md) | Automate brownout, cold-start and recovery tests |
| [Unattended runs](examples/unattended-runs.md) | Hand the bench an experiment and walk away |

## Extending it

| Page | What it covers |
|---|---|
| [Adding a driver](adding-a-driver.md) | Bringing a new instrument into benchctrl |

## Reference material

The guide links into these rather than repeating them:

- [`api_reference.md`](../api_reference.md) — every public class and method
- [`drivers.md`](../drivers.md) — per-instrument detail, firmware quirks, wire formats
- [`dashboard.md`](../dashboard.md) — the bench display's design, the hazard it replaced, the e-stop
- [`KNOWN_LIMITATIONS.md`](../../KNOWN_LIMITATIONS.md) — hardware caps and workarounds, kept deliberately honest
- [`CONTRIBUTING.md`](../../CONTRIBUTING.md) — development setup and conventions

## A note on how these pages are written

Two conventions, both deliberate:

**Limitations are in the body, not an appendix.** Where a technique has a
sharp edge — a voltage ceiling, a latency floor, a mode that cannot be
reversed without a power cycle — it is stated where you would hit it. A guide
that reads well and then surprises you at the bench has failed at the only
job that matters.

**Numbers are measured, not nominal.** Where a page quotes a current, a
sample rate or a settling time, it came off this bench with these
instruments. Where a figure is a datasheet claim, it says so.
