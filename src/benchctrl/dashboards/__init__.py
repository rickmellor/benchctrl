"""Read-only status displays for a running bench.

Nothing is exported at package level — importing this is a deliberate no-op, so
that the agent never pulls a display's dependencies in by accident. Import the
piece you want:

- :py:mod:`~benchctrl.dashboards.state` — what the panel knows, as pure data
- :py:mod:`~benchctrl.dashboards.feed` — the observer session that fills it
- :py:mod:`~benchctrl.dashboards.fui` — the board's HDMI console

The rule all of them inherit, and the reason ``state`` is a separate pure
module: **a display may never change what it displays, and may never influence
the bench.** See ``docs/dashboard.md``.
"""
