"""The bench's cinematic status display.

A full-screen sci-fi console for the board's HDMI panel: dark, glowing, dense,
animated. It is a *skin* over the observer feed in :py:mod:`benchctrl.dashboards.feed`,
so everything in :py:mod:`benchctrl.dashboards.state` about staleness,
pessimistic arming, and the unsafe latch still governs what it is allowed to
claim. The styling is a rendering choice; none of it is allowed to widen what
the display may assert.

The rule that survives the styling
----------------------------------

Sci-fi consoles in films are decorative — every readout is a prop, and being
wrong costs nothing. This one drives decisions about whether it is safe to touch
a DUT, so the two are kept strictly apart:

- **Instrument readouts** (:py:mod:`benchctrl.dashboards.fui.view`) show a real
  measurement or the literal string :py:data:`~benchctrl.dashboards.fui.view.NO_LINK`.
  There is no third option and no interpolation, smoothing, or last-known value
  quietly held on screen. A believable ``12.01 V`` beside an instrument that is
  not connected is the exact failure this package is styled to avoid.
- **Decoration** (the hex streams, the greebles, the crosshairs) is synthetic
  and clearly non-numeric, because nobody can mistake ``0x7F3A`` drifting past
  in 6pt type for a measurement.

:py:mod:`benchctrl.dashboards.fui.view` is pure: it turns a feed snapshot into
the JSON the browser renders, with no Streamlit, no sockets, and no clock of its
own. That is what makes the honesty rules testable without a board or a display.
"""

from benchctrl.dashboards.fui.view import (
    INSTRUMENTS,
    NO_LINK,
    build_view,
)

__all__ = ["NO_LINK", "INSTRUMENTS", "build_view"]
