"""Snapshot → the view model the FUI renders. Pure, and deliberately dull.

Every judgement the cinematic display makes is made here, in plain data, so it
can be asserted in CI without a browser, a board, or a display. The JavaScript
downstream is a renderer: it decides where the glow goes, never what is true.

The invariant this module enforces
----------------------------------

**A slot describing an instrument shows a real reading or it shows
:py:data:`NO_LINK`.** Not a plausible default, not the last value we saw, not
zero. The FUI's whole visual language says "live telemetry", which makes a
stale-but-pretty number more dangerous here than on a plain panel — it is read
from across a bench by someone deciding whether the rail is hot.

So :py:func:`build_view` only ever *narrows*. It starts from
:py:data:`INSTRUMENTS`, every slot dark and unlinked, and lights up exactly what
the agent reported in this snapshot. A device the agent stopped mentioning goes
back to :py:data:`NO_LINK` on the next frame rather than keeping its last glow.

Trustworthiness is carried per-slot, not just globally: when the view is stale
the readouts are marked ``stale`` so the renderer can strike them through. A
global banner is not enough when the numbers themselves are what gets believed.
"""

from __future__ import annotations

from typing import Optional

from benchctrl.dashboards.state import BenchStatus

#: What a readout says when there is no measurement behind it. A single literal,
#: so the renderer can style "no data" one way and there is no chance of an
#: empty string reading as zero.
NO_LINK = "NO LINK"

#: The bench's instrument slots, in the order the right-hand rail stacks them.
#:
#: ``kind`` drives which detail panel a slot gets (a DMM gets digits, a supply
#: gets a trace). ``label`` is what the panel shows: short, uppercase, and
#: readable at three metres.
#:
#: Slots are declared statically rather than discovered so the display has a
#: fixed geometry — a rail that reflows when a USB cable is nudged is unreadable,
#: and an instrument silently vanishing from a bench display is exactly the kind
#: of absence an operator should notice as a dark slot rather than a missing row.
INSTRUMENTS: tuple[dict, ...] = (
    {"key": "otii_arc", "label": "OTII ARC", "kind": "smu", "role": "SOURCE/MEASURE"},
    {"key": "rigol_dp2031", "label": "PSU", "kind": "psu", "role": "BENCH SUPPLY"},
    {"key": "rigol_dl3031a", "label": "DC LOAD", "kind": "load", "role": "ELEC LOAD"},
    {"key": "siglent_sdm4065a", "label": "DMM", "kind": "dmm", "role": "6.5 DIGIT"},
    {"key": "eastwood_qr10x", "label": "QR10X", "kind": "sensor", "role": "SCANNER"},
)

#: Test-sequence stages for the centre flowchart. Names match the run states the
#: agent emits; ``INIT`` and ``DONE`` bracket them.
STAGES: tuple[str, ...] = ("INIT", "PSU RAMP", "LOAD SET", "ANALYSIS", "DONE")

#: Maps a run state to the stage that should be pulsing. Anything unrecognised
#: leaves no stage active rather than guessing, so an unknown state reads as
#: "not one of ours" instead of lighting the wrong node.
_STAGE_FOR_STATE = {
    "pending": "INIT",
    "starting": "INIT",
    "running": "ANALYSIS",
    "finished": "DONE",
    "passed": "DONE",
    "failed": "DONE",
    "aborted": "DONE",
}


def _slot_state(dev: Optional[dict], *, trustworthy: bool) -> dict:
    """One instrument rail slot.

    ``dev`` is the device's entry from the snapshot, or None when the agent did
    not report it at all. None means dark and unlinked — never a default.
    """
    if dev is None:
        return {
            "linked": False,
            "status": NO_LINK,
            "armed": False,
            "inferred": False,
            "stale": False,
        }
    return {
        "linked": True,
        # The label the state model already derived (ARMED/RECORDING/idle), so
        # the FUI cannot invent a status vocabulary that disagrees with it.
        "status": dev["label"].upper(),
        "armed": bool(dev["armed"]),
        # An inferred reading is one we worked out from an event rather than
        # being told. The renderer draws it dashed; it must not look confirmed.
        "inferred": bool(dev["inferred"]),
        "stale": not trustworthy,
    }


def _active_stage(runs: dict) -> Optional[str]:
    """Which flowchart node pulses, if any.

    Only a *running* run drives the flow. A finished one lights ``DONE``; no
    runs at all leaves every node dim, because an idle bench is genuinely not
    part-way through a sequence and pretending otherwise is the cinematic lie
    this module refuses.
    """
    if not runs:
        return None
    # Deterministic order, because dict order here is the agent's iteration
    # order and the runs list beside this flowchart is sorted. With two runs in
    # different states the old fallback took whichever happened to be last, so
    # {finished, unknown-state} lit DONE or nothing depending on nothing
    # meaningful — and the lit node could disagree with the list next to it.
    states = [runs[k] for k in sorted(runs)]
    for state in states:
        if state in ("running", "starting", "pending"):
            return _STAGE_FOR_STATE.get(state)
    # No run is in flight, so light the furthest stage any run actually reached.
    # An unrecognised state contributes nothing rather than being guessed at.
    reached = [_STAGE_FOR_STATE[s] for s in states if s in _STAGE_FOR_STATE]
    if not reached:
        return None
    return max(reached, key=STAGES.index)


def build_view(snap: dict, status: Optional[BenchStatus] = None) -> dict:
    """Turn a :py:meth:`AgentFeed.snapshot` dict into the FUI's view model.

    Takes the flat snapshot rather than the live model so it inherits the
    snapshot's atomicity — the renderer must never see a frame stitched from
    two different instants. ``status`` is optional and used only for the event
    log, which the flat snapshot does not carry.
    """
    trustworthy = bool(snap["trustworthy"])
    devices = snap["devices"]

    slots = []
    for spec in INSTRUMENTS:
        slot = dict(spec)
        slot.update(_slot_state(devices.get(spec["key"]), trustworthy=trustworthy))
        slots.append(slot)

    # Alert count is what an operator must act on, not everything interesting.
    # Deliberately excludes "starting": a booting display has raised nothing.
    alerts = 0
    if snap["unsafe"]:
        alerts += 1
    if snap["armed"]:
        alerts += 1
    if snap["stale_reason"] and not snap.get("starting"):
        alerts += 1
    if snap.get("observer_denied"):
        alerts += 1

    return {
        "headline": snap["headline"],
        "severity": snap["severity"],
        "connected": bool(snap["connected"]),
        "starting": bool(snap.get("starting")),
        "trustworthy": trustworthy,
        "stale_reason": snap["stale_reason"],
        "unsafe": bool(snap["unsafe"]),
        "observer_denied": bool(snap.get("observer_denied")),
        "armed": list(snap["armed"]),
        "alerts": alerts,
        "instruments": slots,
        "stages": [
            {"name": name, "active": name == _active_stage(snap["runs"])}
            for name in STAGES
        ],
        "runs": [{"id": k, "state": v} for k, v in sorted(snap["runs"].items())],
        "dropped_events": int(snap["dropped_events"]),
        "reconnects": int(snap.get("reconnects", 0)),
        "log": _log_lines(status),
        # The banner along the bottom. Says what the bench is doing, or admits
        # it does not know — never "SYSTEM READY" on an unreachable agent.
        "operation": _operation(snap),
    }


def _log_lines(status: Optional[BenchStatus]) -> list[dict]:
    """Recent real events for LOG.MGR, newest last.

    Real events only. The reference art fills this pane with synthetic hex
    chatter; that is fine as *decoration* in the renderer's own background
    layer, but this list is bench truth and is kept clean of it so an operator
    scanning for what actually happened is not reading invented lines.
    """
    if status is None:
        return []
    return [
        {
            "kind": str(e.get("kind", "?")),
            "severity": str(e.get("severity", "info")),
            "device": str(e.get("device", "") or ""),
            "seq": e.get("seq"),
        }
        for e in status.log[-24:]
    ]


def _operation(snap: dict) -> str:
    """The footer banner. Worst-case first, same discipline as the headline."""
    if snap["unsafe"]:
        return "OUTPUT UNCONFIRMED — DO NOT TOUCH THE DUT"
    if snap.get("starting"):
        return "LINKING TO BENCH CONTROL CORE…"
    if not snap["connected"]:
        return "NO LINK TO AGENT — DISPLAY IS NOT LIVE"
    # Above staleness: this one says the display may be endangering the bench,
    # not merely that it is behind. The wording is aimed at the person standing
    # in front of the panel — "observer" is jargon, "the deadman may not fire"
    # is the consequence they need.
    if snap.get("observer_denied"):
        return "AGENT DENIED OBSERVER ROLE — DEADMAN MAY NOT FIRE"
    if snap["stale_reason"]:
        return "VIEW IS STALE — READINGS MAY BE OUT OF DATE"
    if snap["armed"]:
        return f"ARMED: {', '.join(snap['armed']).upper()} — OUTPUT IS LIVE"
    running = [r for r, s in snap["runs"].items() if s == "running"]
    if running:
        return f"RUN IN PROGRESS: {running[0].upper()}"
    return "SYSTEM READY / BENCH IDLE"
