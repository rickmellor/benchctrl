# `benchctrl.dashboards`

Read-only status displays for a running bench.

| module | what it is |
|---|---|
| `state.py` | `BenchStatus` — what the panel knows, folded from an event stream. Pure: no sockets, no clock of its own. |
| `feed.py` | The **observer** session that fills it, on a background thread. |
| `fui/` | The board's HDMI console: stdlib `http.server` + three static files. |

`state.py` is deliberately separate and dependency-free because it decides
everything the display is *allowed to claim* — staleness, pessimistic arming,
the unsafe latch — and that has to be unit-testable without a bench or a
browser. A renderer is a rendering choice; it never widens what may be asserted.

The governing rule, in full in `docs/dashboard.md`: **a display may never change
what it displays, and may never influence bench operation.** Two consequences
that are easy to undo by accident:

- The feed connects as an **observer**, because an ordinary session's traffic
  calls `Governor.touch()` and would hold the deadman open forever.
- A readout shows a real measurement or says `NO LINK`. Never a default, never a
  last-known value held on screen.

One-shot plots are elsewhere: `Recording.plot()` and the `plot_recording` MCP
tool.
