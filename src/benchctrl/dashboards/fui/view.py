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

Why a dark slot is not one state
--------------------------------

``NO LINK`` was originally the answer to every question a slot could not answer,
which made it the panel's least informative pixel: on the development board all
five slots read it while four instruments sat on the bus, powered and ready. The
reason is that the safety governor creates a device's state lazily, on the first
call that could arm something, so ``safety.devices`` is ``{}`` on a healthy idle
agent.

A dark slot now says *which* dark it is, because the operator's next action
differs for each and the states are learned from different places:

======================  ==================================================
:py:data:`STANDBY`      on the bus, agent serves it, not opened yet — the
                        resting state of a healthy bench
:py:data:`ABSENT`       discovery looked and it is not there
:py:data:`UNSCANNED`    nobody has looked yet; not an assertion of absence
:py:data:`UNDETERMINED` discovery looked but *cannot* decide this one, so its
                        absence is unproven
:py:data:`NOT_SERVED`   the agent's registry does not list it, so nothing
                        will drive it even if it is plugged in
:py:data:`FAULT`        the agent tried to open it and failed
:py:data:`NO_LINK`      no session at all, so none of the above is known
======================  ==================================================

The distinctions between the three "not there" states are the ones that earn
their keep. A measurement of absence, the absence of a measurement, and a
question the instrument cannot answer all look the same on a screen and are not
the same fact: collapsing the first two would have the panel assert an empty
bench for the first seconds after every boot, and collapsing the third would have
it report the QR10x missing on a bench where it is plugged in and working.

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

#: What a slot says when discovery found the instrument on the bus but the agent
#: has not opened it. This is the ordinary resting state of a healthy bench —
#: devices open lazily, on first use — so it must not look like a fault.
STANDBY = "STANDBY"

#: Discovery looked and the instrument is not on the bus.
ABSENT = "NOT FOUND"

#: Discovery *cannot* decide for this instrument, so its absence is unproven.
#: Only reachable for a device key with no VID/PID signature — currently the
#: QR10x, which sits behind a generic CH340 bridge whose ID says nothing about
#: what is on the other end, and which ``discovery.inventory()`` identifies only
#: by an explicit probe it does not perform. Reporting :py:data:`ABSENT` there
#: would be a false negative dressed as a measurement.
UNDETERMINED = "NO ID"

#: No inventory has been taken yet, so presence is genuinely unknown. Distinct
#: from :py:data:`ABSENT` on purpose: one is a measurement, the other is the
#: absence of one, and collapsing them would have the panel assert an empty bench
#: during the first seconds after boot.
UNSCANNED = "SCANNING"

#: The agent's registry does not list this device, so nothing will drive it even
#: if it is plugged in. A configuration fact, not a hardware one.
NOT_SERVED = "NOT SERVED"

#: The agent tried to open it and failed. The loudest of the non-armed states,
#: because it is the only one an operator can usually fix.
FAULT = "OPEN FAILED"

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


def _discoverable_keys() -> frozenset[str]:
    """Device keys a bus scan can actually decide, per discovery's own table.

    Read from :py:data:`benchctrl.discovery.SIGNATURES` rather than listed here,
    so adding a VID/PID for an instrument automatically upgrades its slot from
    :py:data:`UNDETERMINED` to a real present/absent answer. A hardcoded list
    would keep saying "cannot tell" after the thing that could tell arrived.

    Imported lazily and defensively: this module is pure view logic and must
    still render if discovery cannot be imported at all.
    """
    try:
        from benchctrl import discovery
    except Exception:  # noqa: BLE001 - the view must never fail to build
        return frozenset()
    return frozenset(
        s.device_key for s in discovery.SIGNATURES if s.device_key
    )

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


def _slot_state(
    dev: Optional[dict],
    slot: Optional[dict],
    *,
    trustworthy: bool,
    inventory_taken: bool,
    registry_known: bool,
    connected: bool,
    discoverable: bool = True,
) -> dict:
    """One instrument rail slot.

    Three independent facts arrive here and the slot must not blur them:

    - ``dev`` — the safety governor's entry, present only once the agent has
      tracked arm state for this device. What it is *doing*.
    - ``slot`` — the registry/discovery entry. Whether the agent *serves* it and
      whether it is *on the bus*.
    - ``inventory_taken`` — whether anybody has looked at the bus yet.

    Precedence runs from most actionable to least: a live arm state outranks
    everything, then an open failure (the operator can fix it), then absence,
    then not-yet-scanned, then standby. Getting this order wrong is how a rail
    ends up reporting a configuration detail over an armed output.
    """
    served = bool(slot and slot.get("served"))
    opened = bool(slot and slot.get("opened"))
    open_error = (slot or {}).get("open_error")
    present = (slot or {}).get("present")

    base = {
        "served": served,
        "opened": opened,
        "present": present,
        # Carried on every slot, not just the undecidable ones, so the renderer
        # can explain a NO ID without re-deriving which keys discovery can see.
        "discoverable": discoverable,
        "confidence": (slot or {}).get("confidence"),
        "path": (slot or {}).get("path"),
        "open_error": str(open_error) if open_error else None,
    }

    # The governor knows about it, so it is open and being tracked: report what
    # it is doing, using the label the state model already derived so the FUI
    # cannot invent a status vocabulary that disagrees with it.
    if dev is not None:
        return {
            **base,
            "linked": True,
            "status": dev["label"].upper(),
            "armed": bool(dev["armed"]),
            # An inferred reading is one we worked out from an event rather than
            # being told. The renderer draws it dashed; it must not look
            # confirmed.
            "inferred": bool(dev["inferred"]),
            "stale": not trustworthy,
        }

    # Everything below is "no arm state", which used to be one undifferentiated
    # NO_LINK. None of these are measurements, so none of them are ever marked
    # linked — the renderer's bright-cyan treatment stays reserved for a slot
    # the agent is actually talking to.
    dark = {**base, "linked": False, "armed": False, "inferred": False, "stale": False}

    if not connected:
        # No session, so every one of the facts below is of unknown age. Nothing
        # here can be asserted, including absence.
        return {**dark, "status": NO_LINK}
    if open_error:
        return {**dark, "status": FAULT, "attention": True}
    if not registry_known:
        # No device table has arrived, so "the agent does not serve this" is not
        # something anyone has been told. Falls through to NO_LINK rather than
        # guessing either way — this is the older-agent and first-frame case.
        return {**dark, "status": NO_LINK}
    if not served:
        # Attached but unowned is worth distinguishing from simply unconfigured:
        # the first is a config gap on a bench that has the hardware, and the
        # operator's next move differs.
        return {**dark, "status": NOT_SERVED, "attention": bool(present)}
    if present is False and not discoverable:
        # A scan ran and did not find it, but a scan structurally cannot find
        # this one — no VID/PID signature exists, so "not found" is not a
        # measurement of absence and must not be shown as one.
        return {**dark, "status": UNDETERMINED}
    if present is False:
        return {**dark, "status": ABSENT}
    if present is None or not inventory_taken:
        return {**dark, "status": UNSCANNED}
    # Served, on the bus, no arm state: the resting state of a healthy bench,
    # because the registry opens devices lazily on first use.
    return {**dark, "status": STANDBY, "ready": True}


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
    # .get with a default rather than indexing: build_view is called with
    # snapshots from an AgentFeed, and a feed talking to an older agent — or a
    # test fixture predating these fields — must degrade to "unknown presence"
    # rather than raising and blanking the whole panel.
    dev_slots = snap.get("slots") or {}
    inventory_taken = bool(snap.get("inventory_taken"))
    registry_known = bool(snap.get("registry_known"))
    connected = bool(snap["connected"])

    discoverable = _discoverable_keys()

    slots = []
    for spec in INSTRUMENTS:
        slot = dict(spec)
        slot.update(
            _slot_state(
                devices.get(spec["key"]),
                dev_slots.get(spec["key"]),
                trustworthy=trustworthy,
                inventory_taken=inventory_taken,
                registry_known=registry_known,
                connected=connected,
                discoverable=spec["key"] in discoverable,
            )
        )
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
        # Counts for the rail header, so an operator reads "3 of 5 on the bus"
        # without decoding five slots. Derived from the slots just built rather
        # than recomputed, so the summary cannot disagree with the rail.
        "bench": {
            "linked": sum(1 for s in slots if s["linked"]),
            "present": sum(1 for s in slots if s.get("present") is True),
            "total": len(slots),
            # The denominator a scan can actually speak to. Counting the QR10x in
            # "N/5 ON BUS" would make a fully-populated bench read as incomplete
            # forever, since no scan can ever find it.
            "scannable": sum(1 for s in slots if s["key"] in discoverable),
            "inventory_taken": inventory_taken,
        },
        # Instruments on the bus that no driver claims. A fixed five-slot rail
        # structurally cannot show these, and "something is plugged in that
        # benchctrl cannot drive" is a real bench fact — on the development board
        # it is an SDG1032X and a DS1000Z scope with no drivers yet.
        "unclaimed": list(snap.get("unclaimed") or ()),
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
