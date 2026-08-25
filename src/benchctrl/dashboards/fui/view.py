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
:py:data:`IN_RUN`       a live run declared it would drive this instrument,
                        so it is in use for the run's whole duration
:py:data:`OPEN`         the agent holds a live session to it, nothing armed
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

#: The agent holds a live session to the instrument but nothing is armed. Ranks
#: between an arm state and :py:data:`STANDBY`: it is a stronger statement than
#: "on the bus" (the agent has talked to it and is holding it) and a weaker one
#: than any arm state, which is why it cannot displace ARMED.
OPEN = "OPEN"

#: A run in flight declared this instrument as one it would drive.
#:
#: Outranks :py:data:`OPEN` because it is the stronger claim about the same
#: device — an open session says a handle exists, this says something is using it
#: and will keep using it. It holds for the run's full duration, which is the
#: point: a run's calls come in bursts, so a slot that only lit up while a call
#: was in flight read as idle through every dwell of a test that owned it.
IN_RUN = "IN RUN"

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

#: Presentation for each instrument benchctrl has a driver for: how it is named
#: and drawn, *not* which ones this bench has.
#:
#: ``kind`` drives which detail panel a slot gets (a DMM gets digits, a supply
#: gets a trace). ``label`` is what the panel shows: short, uppercase, and
#: readable at three metres.
#:
#: This is a lookup table, not the rail's contents — see :py:func:`_rail_specs`.
#: Using it directly as the rail was a bug of the "works on my bench" kind: it
#: hardcoded the development bench's five instruments, so a user with a DMM and
#: nothing else got three permanently dark slots for hardware they do not own,
#: and anyone whose agent served a device not in this table got no slot at all.
INSTRUMENTS: tuple[dict, ...] = (
    {"key": "otii_arc", "label": "OTII ARC", "kind": "smu", "role": "SOURCE/MEASURE"},
    {"key": "rigol_dp2031", "label": "PSU", "kind": "psu", "role": "BENCH SUPPLY"},
    {"key": "rigol_dl3031a", "label": "DC LOAD", "kind": "load", "role": "ELEC LOAD"},
    {"key": "siglent_sdm4065a", "label": "DMM", "kind": "dmm", "role": "6.5 DIGIT"},
    {"key": "eastwood_qr10x", "label": "QR10X", "kind": "sensor", "role": "SCANNER"},
)

#: Presentation for a device this build has no entry for. Its slot is still
#: drawn, because the agent serving something we cannot name is a fact about the
#: bench and hiding it would make the rail quietly incomplete.
_UNKNOWN_KIND = "generic"
_UNKNOWN_ROLE = "INSTRUMENT"


def _label_for(key: str) -> str:
    """A three-metre-readable name for a device key we have no spec for.

    ``rigol_dp2031`` → ``DP2031``: the vendor prefix is dropped because the model
    is the distinguishing part and the column is narrow. Falls back to the whole
    key when there is nothing to drop, rather than to an empty string — an
    unlabelled slot is worse than an ugly one.
    """
    tail = key.split("_", 1)[-1] if "_" in key else key
    return (tail or key).upper()


def _rail_specs(slots: dict) -> list[dict]:
    """The rail's slots, for the bench described by ``slots``.

    A device earns a row by being **served or present** — the agent will drive it,
    or the hardware is on the bus. Both halves matter and for opposite reasons: a
    served device that is absent needs its row to say ``NOT FOUND``, and hardware
    found on the bus that nothing serves needs one to say ``NOT SERVED``. Those
    are the two mismatches an operator has to fix, and hiding either would make
    the rail agree with itself while disagreeing with the bench.

    A recorded ``open_error`` also earns a row, even when the device is by then
    neither served nor present. The agent demonstrably *tried* to open this
    instrument and failed, which is the most actionable thing the rail can say —
    and the combination is reachable: an agent restarted with a shorter device
    list drops the key from its table while the recorded failure remains. Filtering
    on served-or-present alone would make the panel drop the one row an operator
    could act on, precisely when it appeared.

    Otherwise, neither served nor present means nothing has ever referred to this
    device on this bench, and it gets no row. The state model keeps such a slot on
    purpose (``served`` is cleared, not deleted, so an unserved-but-attached
    instrument can still be shown) — so this is the filter that makes the rail
    describe *a* bench rather than every bench benchctrl has drivers for.

    Membership therefore never turns on cable state alone: unplugging an
    instrument the agent still serves leaves its row in place and dark, which is
    the absence an operator should notice, rather than a row silently vanishing.

    Order follows :py:data:`INSTRUMENTS` so a standard bench always stacks the
    same way regardless of the order the agent lists devices in; anything
    unrecognised is appended, sorted, so the geometry is still stable frame to
    frame.

    An empty result is honest and handled: :py:func:`build_view` falls back to the
    full table before a device table arrives, so the panel is populated during
    startup rather than blank.

    **Core harness is excluded.** :py:data:`PDU_KEYS` never earns a row, however
    served or present it is. This is not cosmetic filtering: the served-or-present
    rule above appends any key it does not recognise as an unknown instrument, so a
    switched PDU landed on the rail with the ``INSTRUMENT`` role and the rail's arm
    vocabulary the moment the agent began serving it — the exact treatment it must
    not get. It has its own panel (:py:func:`_mains_panel`), and a device shown in
    both places is one an operator has to reconcile.
    """
    keys = [
        key
        for key, slot in slots.items()
        if isinstance(slot, dict)
        and key not in PDU_KEYS
        and (slot.get("served") or slot.get("present") or slot.get("open_error"))
    ]
    wanted = set(keys)
    specs = [dict(s) for s in INSTRUMENTS if s["key"] in wanted]

    known = {s["key"] for s in INSTRUMENTS}
    for key in sorted(wanted - known):
        specs.append(
            {
                "key": key,
                "label": _label_for(key),
                "kind": _UNKNOWN_KIND,
                "role": _UNKNOWN_ROLE,
            }
        )
    return specs

#: Test-sequence stages for the centre flowchart, in order.
#:
#: These are the stages the *bench* emits (``run_stage`` events from
#: :py:class:`~benchctrl.agent.runs.engine.RunEngine`), so this row reports where
#: a run is rather than deriving it. Duplicated as literals rather than imported
#: from ``benchctrl.agent.runs.spec`` for the reason ``state.py`` gives at length:
#: the dashboard talks to a *remote* agent over a wire protocol and must build
#: where the agent is not importable. :py:func:`_active_stage` therefore treats an
#: arriving name it does not recognise as a name it cannot place, rather than
#: assuming this tuple is authoritative.
#:
#: The previous vocabulary — ``PSU RAMP``, ``LOAD SET``, ``ANALYSIS`` — was the
#: renderer's own invention, mapped from a run's coarse status. Two of those five
#: nodes had nothing that could ever select them, and the one that did lit
#: ``ANALYSIS`` from the first setpoint onwards, so the row was mostly scenery
#: with a pulse that carried one bit: a run exists.
STAGES: tuple[str, ...] = ("INIT", "PREPARE", "EXECUTE", "ANALYZE", "DONE")


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

#: Fallback stage for a run whose bench never sent a ``run_stage`` event, mapped
#: from the coarse run state. Used *only* when nothing was reported.
#:
#: This used to be the only mechanism, and it is why the sequence row was
#: decoration: a status field with three interesting values cannot express five
#: stages, so most of them were unreachable. It survives for the two cases where a
#: reported stage genuinely is not available — an older agent, and a panel that
#: connected mid-run and missed the transition — and it is deliberately coarse
#: there: only the brackets, never a claim about the middle of a run.
#:
#: ``running`` maps to nothing on purpose. "A run is in flight" does not say which
#: stage it is in, and the old mapping's answer (``ANALYSIS``) was wrong for
#: almost the entire time it was lit. No node lit is the honest rendering of "the
#: bench has not told us where it is".
_STAGE_FOR_STATE = {
    "pending": "INIT",
    "starting": "INIT",
    "finished": "DONE",
    "complete": "DONE",
    "passed": "DONE",
    "failed": "DONE",
    "aborted": "DONE",
    "errored": "DONE",
    "error": "DONE",
    "safe_stopped": "DONE",
}


#: How long after an action a slot still shows it. Chosen against the status
#: poll (:py:data:`~benchctrl.dashboards.feed.DEFAULT_POLL_S`, 5 s) so the marker
#: outlives the gap between polls: the whole point is to cover the window where
#: ``busy_with`` samples idle because a ~200 ms call fell between two polls.
#: Long enough to bridge that, short enough that nobody reads it as "now".
RECENT_ACTION_S = 8.0


def _recent_action(
    key: str, ages: object, names: object, *, trustworthy: bool
) -> Optional[dict]:
    """What this device was last seen doing, and how long ago — or None.

    The complement to :py:func:`_busy_method`, and deliberately a different claim.
    ``busy`` says a call is in flight *at this instant* and comes from a 5 s poll;
    this says a call *completed* N seconds ago and comes from action events, which
    arrive as each call finishes. Only the second can see a bench driven by
    ~200 ms calls: the poll almost never lands inside one, which is why a live
    6-setpoint sweep left both instruments' rails looking untouched.

    Carrying the age rather than a boolean is what keeps it honest. "Recently
    active" with no number invites being read as "active", and this marker is
    always about the past — so the renderer is given the seconds and shows them.

    Withheld entirely when the view is not trustworthy, matching ``busy``: on a
    stale view the last action could have been minutes ago with nothing arriving
    to correct it, and an age computed against a frozen feed understates itself.
    """
    if not trustworthy or not isinstance(ages, dict):
        return None
    age = ages.get(key)
    if not isinstance(age, (int, float)) or isinstance(age, bool):
        return None
    if age < 0 or age > RECENT_ACTION_S:
        return None
    name = ""
    if isinstance(names, dict):
        raw = names.get(key)
        if isinstance(raw, str):
            name = _clip(raw, _ACTION_CHARS)
    return {"age_s": round(float(age), 1), "action": name}


def _enrolled_run(key: str, enrolled: object) -> Optional[str]:
    """The id of the in-flight run that declared ``key``, or None.

    Same defensive shape as :py:func:`_busy_method` and for the same reason: this
    crossed a process boundary from an agent that may be a release ahead or
    behind, and a malformed table must cost this one slot its IN RUN marker rather
    than raise inside the renderer.

    An empty or non-string run id yields None. A truthy id is what licenses the
    slot to claim it is in a run, so a blank one must not — "in a run I cannot
    name" is a worse readout than falling through to the open-session rung, which
    is still true and still says the agent is talking to the device.
    """
    if not isinstance(enrolled, dict):
        return None
    raw = enrolled.get(key)
    if not isinstance(raw, str) or not raw:
        return None
    return raw


def _busy_method(key: str, busy: object) -> Optional[str]:
    """What ``key``'s worker thread is executing right now, named by method alone.

    The agent labels a job ``f"{key}.{name}"`` (``agent/server.py``), so the raw
    value hands the device key back to a slot that is already headed by it: on
    the bench board ``busy_with`` reads
    ``siglent_sdm4065a.measure_resistance_4wire`` on a row titled DMM. Only a
    leading ``"{key}."`` is removed, and it is removed *by length*, never by
    splitting on ``"."`` — a driver method reached through a sub-object arrives
    as ``rigol_dp2031.channel.set_current``, and ``rsplit(".")[-1]`` would show
    ``set_current``, naming a call that is not the one running. Naming the wrong
    call is worse than naming a long one.

    Anything unusable yields None rather than a guess: a payload that is not a
    dict, a value that is not a string. This field crossed a process boundary
    from an agent that may be a release ahead or behind, and a malformed
    ``workers`` table must cost this slot its activity marker and nothing else.
    """
    if not isinstance(busy, dict):
        return None
    raw = busy.get(key)
    if not isinstance(raw, str) or not raw:
        return None
    prefix = key + "."
    if raw.startswith(prefix):
        # ``or raw``: a label that is nothing but the prefix still means a call
        # is in flight, and returning "" there would report the device idle —
        # the one thing this field must never do while a thread is inside a
        # driver call.
        return raw[len(prefix) :] or raw
    return raw


def _queue_depth(key: str, queued: object) -> int:
    """How many calls are waiting behind ``key``'s current one; 0 when unknown.

    0 for anything unusable, bools included: ``isinstance(True, int)`` is True,
    so a flag arriving in this field would otherwise be shown as "1 queued" —
    a count nobody measured, on a panel whose numbers are what get believed.
    """
    if not isinstance(queued, dict):
        return 0
    depth = queued.get(key)
    if isinstance(depth, bool) or not isinstance(depth, int) or depth < 0:
        return 0
    return depth


def _slot_state(
    dev: Optional[dict],
    slot: Optional[dict],
    *,
    trustworthy: bool,
    inventory_taken: bool,
    registry_known: bool,
    connected: bool,
    discoverable: bool = True,
    busy: Optional[str] = None,
    queued: int = 0,
    recent: Optional[dict] = None,
    enrolled_run: Optional[str] = None,
) -> dict:
    """One instrument rail slot.

    Three independent facts arrive here and the slot must not blur them:

    - ``dev`` — the safety governor's entry, present only once the agent has
      tracked arm state for this device. What it is *doing* — but only when it is
      doing something. Its resting label ("idle") is not a status word here and
      never reaches ``status``; a bare entry means a handle was opened, which the
      OPEN rung says without outranking a run.
    - ``slot`` — the registry/discovery entry. Whether the agent *serves* it and
      whether it is *on the bus*.
    - ``inventory_taken`` — whether anybody has looked at the bus yet.

    Precedence runs from most actionable to least: a live arm state outranks
    everything, then an open failure (the operator can fix it), then a measured
    absence, then enrollment in a live run, then an open session, then an open
    session *inferred from activity*, then the two forms of not-yet-known, then
    standby. Getting this order wrong is how a rail ends up reporting a
    configuration detail over an armed output.

    The one adjacent pair worth naming is absence over an open session. They can
    contradict each other — ``opened`` says the agent got a handle at some point,
    not that it has just proved the handle works, so a cable pulled mid-session
    leaves it True against a dead file descriptor. A scan that looked and did not
    find the device is both the newer measurement and the one the operator can
    act on, so it wins. The same reasoning puts an open session *above*
    UNDETERMINED and UNSCANNED, which are not measurements at all: nothing opens
    a session to hardware that is absent. It also keeps IN_RUN below a measured
    absence: a run declaring it will drive an instrument is a statement about
    intent, and a scan that cannot find the instrument outranks intent.

    ``busy`` and ``queued`` — what this device's worker thread is executing at
    this instant — deliberately sit **outside** that ordering, as their own
    fields, and never touch ``status``. The precedence above is a ranking of
    claims competing for one word, and every rung on it is either a hazard or a
    reason to distrust the screen; being busy is neither. It is the bench doing
    its job, and it is also the only one of these facts that is orthogonal to the
    rest: an armed supply can be mid-call, and both halves are true at once.

    Folding it into the status word — even only in the standby case, where
    nothing would visibly compete — was rejected because it makes the safety
    property depend on a branch. ``status`` would then be the one string that is
    sometimes the arm state and sometimes an activity report, and the next person
    to add a rung has to rediscover why. Kept separate, "a routine measurement
    cannot hide a live output" holds structurally rather than by ordering: there
    is no input on which ``busy`` can displace ``ARMED``, because no code path
    writes it there. Same reasoning as :py:attr:`BenchStatus.headline` keeping
    ``ACTIVE`` below ``ARMED``, one level down.

    Both are carried on *every* slot, including the dark ones. A key with no
    entry gets ``busy=None`` and ``queued=0``, which the renderer draws as
    nothing at all — not "idle", which would be a claim.
    """
    if not trustworthy:
        # An activity marker has no stale form. Every other readout here can be
        # struck through and still mean something — a crossed-out ARMED is a
        # warning about a bench nobody can see — but "executing
        # measure_resistance_4wire" is an assertion about this instant, and there
        # is no way to draw it that does not say a call is in flight now.
        #
        # A disconnect is already handled at the source:
        # :py:meth:`BenchStatus.apply_disconnected` drops ``busy_devices`` and
        # ``queued_devices`` with the session, alongside presence and identity, so
        # nothing is duplicated for that case. This covers the one it cannot — a
        # view that is merely *behind*, from dropped events or a silent agent,
        # where the last snapshot's ``busy_with`` is still on the snapshot and
        # would sit on the glass indefinitely with nothing arriving to correct it.
        busy, queued = None, 0
        # Same reasoning: an age measured against a feed that stopped arriving
        # understates itself, so "2 s ago" would sit on the glass while the real
        # answer grew to minutes. _recent_action also refuses on an untrustworthy
        # view; belt and braces, because this function is called directly by tests
        # and by any future caller that does not go through build_view.
        recent = None

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
        # What the hardware says it is, learned from the bus scan: the model
        # string and the serial off the USB descriptors or the VISA resource.
        # Deliberately NOT called "label" — the rail's own short label ("PSU")
        # is on the slot spec under that name, and build_view merges this dict
        # over it, so reusing the key would replace the three-metre-readable
        # name with a sentence.
        #
        # A serial number is the field that makes the rail auditable: two
        # DP2031s on a bench are indistinguishable by model alone, and "which
        # supply did that run drive" is answered here or not at all.
        "hw_label": (slot or {}).get("label"),
        "serial_number": (slot or {}).get("serial_number"),
        "usb_id": (slot or {}).get("usb_id"),
        "open_error": str(open_error) if open_error else None,
        # What this device is doing *right now*, alongside the status rather than
        # inside it — see the docstring. None and 0 mean "nothing said so", which
        # the renderer prints as nothing.
        "busy": busy,
        "queued": queued,
        # What this device was last seen doing, with its age. Sits beside ``busy``
        # rather than inside it, and never displaces it: a slot that is genuinely
        # mid-call shows the call, and this covers the gaps between polls. Like
        # ``busy`` it is carried on every slot, including dark ones, where None
        # draws as nothing rather than as a claim of inactivity.
        "recent": recent,
    }

    # The governor knows about it and it has a real arm state: report what it is
    # doing, using the label the state model already derived so the FUI cannot
    # invent a status vocabulary that disagrees with it. Top of the ladder because
    # every label reachable here is a live output — a hazard, and the one thing
    # nothing below may displace.
    #
    # The governor's *resting* label is excluded, and that exclusion is the point.
    # ``DeviceView.label`` returns "idle" when nothing is armed, and publishing it
    # here put the word IDLE at the top of the precedence ladder — above IN RUN and
    # both forms of OPEN, and not in this module's vocabulary at all. The effect was
    # R1's exact failure mode wearing a different word: measured on hardware, a
    # governor-tracked DMM read IDLE for all 82 in-flight samples of a run that had
    # declared it, with ``run`` attribution absent, because the ladder returned
    # before it ever reached the enrollment rung. STANDBY was fixed and IDLE was
    # not, since it enters two rungs higher.
    #
    # "The governor has a state object for this device" is not an activity report.
    # The governor creates that object lazily on the first call that *could* arm
    # something, so its existence says a handle was opened — which is what the
    # rungs below already know how to say, and they can say it without outranking
    # a run.
    armed_now = bool(dev and (dev["armed"] or dev["label"].upper() != "IDLE"))
    if dev is not None and armed_now:
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
    # Nothing armed, but the governor tracking it is still evidence of an open
    # session: it does not create state for a device it has never called. Carried
    # down the ladder so the OPEN rung can use it, rather than short-circuiting
    # here — the run and hazard rungs in between must get their chance first.
    governor_open = dev is not None

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
    if not registry_known and not governor_open:
        # No device table has arrived, so "the agent does not serve this" is not
        # something anyone has been told. Falls through to NO_LINK rather than
        # guessing either way — this is the older-agent and first-frame case.
        #
        # A governor state object is exempt because it is *not* the absence of a
        # report: the governor only creates one for a device it has called, so the
        # agent has told us about this instrument by a route other than the device
        # table. Both of these gates mean "nobody said", and here somebody did.
        return {**dark, "status": NO_LINK}
    if not served and not governor_open:
        # Attached but unowned is worth distinguishing from simply unconfigured:
        # the first is a config gap on a bench that has the hardware, and the
        # operator's next move differs. Same exemption, same reason — an agent
        # whose governor is driving a device plainly serves it, whatever a missing
        # or lagging device table implies.
        return {**dark, "status": NOT_SERVED, "attention": bool(present)}
    if present is False and discoverable:
        # A scan that *can* see this device ran and did not find it. Ranked above
        # an open session on purpose, and it is the one place the two genuinely
        # contradict each other: the registry reports a session the agent opened
        # at some point, not one it has just proved works, so a cable pulled
        # mid-session leaves ``opened`` True against a handle that is already
        # dead. Between "the agent holds a handle" and "a scan looked and it is
        # gone", the absence is both the newer measurement and the actionable
        # one, so it takes the word.
        return {**dark, "status": ABSENT}
    if enrolled_run:
        # A live run declared it would drive this instrument, so it is in use for
        # the run's whole duration — including the dwells. Ranked above OPEN
        # because it is the stronger claim about the same device: OPEN says the
        # agent holds a handle, IN RUN says something is actively using it and
        # will keep using it. Ranked below the hazard rungs above for the usual
        # reason — being enrolled in a run is not a reason to distrust the screen,
        # and a measured absence still outranks any claim about what should be
        # happening.
        #
        # This exists because enrollment and activity answer different questions.
        # A supply set once at phase entry and held through a ten-minute dwell is
        # in use the whole time while making no calls for all but 200 ms of it, so
        # activity alone leaves it looking idle during a test that is driving it.
        return {
            **base,
            "linked": True,
            "status": IN_RUN,
            "armed": False,
            "inferred": False,
            "stale": not trustworthy,
            "run": enrolled_run,
        }
    if opened or governor_open:
        # The agent holds a live session to this instrument. STANDBY explicitly
        # means "on the bus, *not opened yet*", so reporting it here was false:
        # during a 4-wire sweep the DMM and QR10x both read STANDBY for the whole
        # test while the agent was driving them.
        #
        # Above the two remaining not-known rungs, because an open session is
        # better evidence of presence than either of them has. UNDETERMINED and
        # UNSCANNED both mean "no scan has answered this"; a session that opened
        # answers it — nothing opens a handle to hardware that is not there. This
        # ordering is what the QR10x needs: it sits behind a driverless CH340
        # with no VID/PID, so no non-probing scan will *ever* confirm it, and
        # ranked below UNDETERMINED it would read "cannot tell" for the whole of
        # a test that was driving it.
        #
        # ``linked`` is what earns the renderer's bright-cyan treatment, and an
        # open session is exactly the condition it is meant to mark — the agent is
        # talking to this device. It is not marked ``inferred``: an open session
        # is reported by the registry in the ``agent.devices`` table, not deduced.
        #
        # ``governor_open`` reaches here too, and is marked ``inferred`` when the
        # registry has not also said ``opened``: a governor state object implies a
        # handle rather than reporting one, which is exactly what that flag means
        # everywhere else in this ladder.
        return {
            **base,
            "linked": True,
            "status": OPEN,
            "armed": False,
            "inferred": not opened,
            "stale": not trustworthy,
        }
    if recent is not None or busy:
        # The device is executing a call, or just did. It cannot do that without
        # an open handle, so it is open — we have simply not been *told* yet.
        #
        # This rung closes a seam between two feeds running at different rates.
        # Activity arrives on the event stream, within milliseconds of the call;
        # ``opened`` arrives on the 5 s ``agent.status`` poll. For up to one poll
        # interval after a device's first call, the card therefore had live
        # activity text and a status word of STANDBY — measured on hardware as a
        # 10-sample window at the start of a sweep, and precisely the complaint
        # that a card "still shows standby while the device is in use" with "some
        # text on the card updating".
        #
        # Reported as OPEN and flagged ``inferred``, which is what that flag
        # already means here: worked out from an event rather than reported by the
        # registry. The renderer draws it dashed, so the seam is visible as a
        # slightly-less-confirmed OPEN rather than papered over — and one poll
        # later the rung above supersedes it with the confirmed form.
        #
        # Placed directly below OPEN as the weaker sibling of the same claim, and
        # above the two not-known rungs for OPEN's own reason: a completed call
        # answers the presence question that UNDETERMINED and UNSCANNED say
        # nobody has answered. Left *below* ABSENT so a scan that looked and
        # cannot find the device still takes the word; that pairing is unreachable
        # for the QR10x, which is undiscoverable and so was reading NO ID during
        # the sweep that was driving it.
        #
        # ``busy`` and ``recent`` are both cleared above when the view is not
        # trustworthy, so this rung cannot fire off a stale snapshot.
        return {
            **base,
            "linked": True,
            "status": OPEN,
            "armed": False,
            "inferred": True,
            "stale": not trustworthy,
        }
    if present is False:
        # Not discoverable, so the scan's "not found" is not a measurement of
        # absence and must not be shown as one — no VID/PID signature exists for
        # a scan to match.
        return {**dark, "status": UNDETERMINED}
    if present is None or not inventory_taken:
        return {**dark, "status": UNSCANNED}
    # Served, on the bus, no arm state: the resting state of a healthy bench,
    # because the registry opens devices lazily on first use.
    return {**dark, "status": STANDBY, "ready": True}


#: Device keys that switch mains, for deciding whether this bench has a mains
#: panel at all.
#:
#: A literal, and deliberately not an import of
#: :py:data:`benchctrl.agent.registry.SWITCHED_PDU_KEYS`, for the reason recorded
#: at the top of :py:mod:`benchctrl.dashboards.state`: this display runs against a
#: *remote* agent over a wire protocol, and a panel that could only be built where
#: ``agent`` is importable would not be a remote panel. Drift here fails safe — an
#: unlisted PDU key yields :py:data:`MAINS_NO_PDU` until a sweep arrives, and the
#: sweep is what actually decides the panel's contents.
PDU_KEYS: frozenset[str] = frozenset({"cyberpower_pdu41002"})

#: The mains panel's own verdict words. Separate from the instrument rail's
#: vocabulary on purpose: a PDU's states are not an instrument's, and reusing
#: ``STANDBY``/``OPEN`` here would invite the renderer to style them alike when
#: what they mean is unrelated. ``NO PDU`` is the honest word for a bench with no
#: switched PDU served — an absence of hardware, not a fault.
MAINS_NO_PDU = "NO PDU"

#: A PDU is served but nothing has reported its state. Reached in two ways that
#: both leave the panel with nothing to show: the bench has not swept yet, and the
#: PDU has not been opened by anything with a reason to open it (a display must
#: never be that reason — see ``agent.server._maybe_start_mains_sweep``). Both are
#: ordinary on a healthy idle bench, so this must not read as a fault.
MAINS_UNREPORTED = "NOT REPORTED"

#: Mains state arrived and this is what it says. ``LIVE`` describes the *supply*,
#: not any outlet: the PDU's input is energised, which is true whenever the bench
#: can talk to it at all.
MAINS_LIVE = "MAINS LIVE"

#: A run is holding off measurement while a DUT comes back up. Outranks
#: :py:data:`MAINS_LIVE` because it is the more specific statement about the same
#: instant, and because a phase in a settle window otherwise looks stalled.
MAINS_SETTLING = "DUT SETTLING"

#: Per-outlet words. Short enough for a row of eight on a narrow panel.
OUTLET_ON = "ON"
OUTLET_OFF = "OFF"

#: An outlet the PDU did not report in the last sweep. Not ``OFF``: an unreported
#: outlet is one nobody has heard about, and rendering that as de-energised is the
#: one mistake on this panel that could put somebody's hand in an enclosure.
OUTLET_UNKNOWN = "—"

#: How old a mains reading may be before the panel marks it stale, in seconds.
#:
#: Derived from the bench's own sweep cadence rather than picked: the agent sweeps
#: every ``heartbeat_s * MAINS_INTERVAL_HEARTBEATS`` (~10 s on a 5 s heartbeat),
#: so this is three intervals — the same two-may-be-missed-before-crying-wolf
#: margin the silence budget uses. Mains readings also arrive *only* by push, so a
#: panel that failed to notice them stopping would have no other way to find out.
MAINS_STALE_S = 30.0


def _mains_panel(snap: dict, *, trustworthy: bool) -> dict:
    """The bottom-left harness panel: the bench's mains and its outlets.

    Core harness, and the reason this is a function of its own rather than a row
    in :py:func:`_rail_specs`. A PDU is not an instrument: nothing measures with
    it, no run enrolls it as a source, and it has no arm state. It is what the
    bench and the DUT are plugged into — peer to the board the agent runs on — so
    it gets a fixed panel that is present whenever the bench has one, rather than
    a slot that competes for space with the things a test actually reads.

    Three states this must keep apart, because an operator's next action differs
    for each and two of them look identical from the data:

    ==========================  ==============================================
    :py:data:`MAINS_NO_PDU`     no switched PDU is served; there is no mains
                                readout to have
    :py:data:`MAINS_UNREPORTED` a PDU is served and nothing has reported it —
                                either no sweep yet or nothing has opened it
    :py:data:`MAINS_LIVE`       a reading arrived, and these are the numbers
    ==========================  ==============================================

    The first two both have empty outlet maps, which is why ``known`` is computed
    in the state model rather than inferred from emptiness here.

    Every number is passed through or replaced by :py:data:`NO_LINK`, never
    defaulted. That rule is inherited from this module's header and is sharper for
    this panel than any other: a fabricated ``0.0 A`` reads as "nothing is
    drawing", and a fabricated outlet ``OFF`` reads as "the DUT is de-powered".
    Both are read from across a bench by someone deciding whether it is safe to
    touch something.
    """
    mains = snap.get("mains")
    mains = mains if isinstance(mains, dict) else {}
    # Served-ness comes from the device table, not from the mains payload: the
    # panel must be able to say "this bench has a PDU we have not heard from",
    # which is precisely the case where the payload is empty.
    slots = snap.get("slots")
    slots = slots if isinstance(slots, dict) else {}
    served = any(
        key in PDU_KEYS
        and isinstance(slot, dict)
        and (slot.get("served") or slot.get("present"))
        for key, slot in slots.items()
    )
    known = bool(mains.get("known"))
    # A reading is its own kind of stale, on its own clock. Both routes count: the
    # whole view being untrustworthy, and mains alone having gone quiet while the
    # rest of the panel is current — the second is invisible to the global banner.
    age = _as_optional_float(mains.get("age_s"))
    aged_out = age is not None and age > MAINS_STALE_S
    stale = known and (not trustworthy or aged_out)

    rows = _outlet_rows(mains.get("outlets"))
    settling = _as_optional_float(mains.get("settling_s"))
    if not served and not known:
        status = MAINS_NO_PDU
    elif not known:
        status = MAINS_UNREPORTED
    elif settling is not None and settling > 0:
        status = MAINS_SETTLING
    else:
        status = MAINS_LIVE

    return {
        "status": status,
        "known": known,
        "served": served,
        "stale": stale,
        # Distinguished from ``stale`` so the renderer can say *why* — a reading
        # the bench stopped sending is a different fault from a whole view that
        # has gone untrustworthy, and only the first points at the PDU.
        "aged_out": bool(aged_out),
        "age_s": age,
        "device": str(mains.get("device") or ""),
        "transport": str(mains.get("transport") or ""),
        "voltage": _mains_reading(mains.get("voltage_V"), known, "{:.1f} V"),
        "frequency": _mains_reading(mains.get("frequency_Hz"), known, "{:.1f} Hz"),
        "load_A": _mains_reading(mains.get("load_A"), known, "{:.2f} A"),
        "load_W": _mains_reading(mains.get("load_W"), known, "{:.0f} W"),
        "outlets": rows,
        # ``n of m`` for the panel header, where m is how many outlets were
        # *reported* rather than how many the device has. A PDU that reported six
        # of eight must not read as "2 off" — that would be inventing two
        # measurements out of a short payload.
        #
        # Counted from the rows just built rather than passed through from the
        # payload, so the number and the rows beneath it cannot disagree: a
        # payload whose ``energised`` was computed before an outlet was dropped
        # for an unparseable index would otherwise show "3 on" above two ON rows.
        # Not ``_as_count`` — that floors at 1, which is right for a log row's
        # repeat count and catastrophic here, where zero energised outlets is both
        # possible and the single most important number on the panel to get right.
        "energised": sum(1 for r in rows if r["on"]),
        "reported": len(rows),
        "settling_s": settling,
        "transitions": _as_tally(mains.get("transitions")),
        # The last verified switch a run reported, for the line that says a power
        # cycle happened between two sweeps. None when no run has switched
        # anything this session.
        "last_transition": (
            mains.get("last_transition")
            if isinstance(mains.get("last_transition"), dict)
            else None
        ),
    }


def _mains_reading(value: object, known: bool, fmt: str) -> str:
    """One metering figure, formatted, or :py:data:`NO_LINK`.

    ``known`` is checked as well as the value: a payload that carried a number
    from a previous session must not survive as a reading, and the state model's
    disconnect path already clears the numbers — this is the second half of the
    same guarantee, at the point where the string an operator reads is made.
    """
    if not known:
        return NO_LINK
    number = _as_optional_float(value)
    if number is None:
        return NO_LINK
    return fmt.format(number)


def _outlet_rows(outlets: object) -> list[dict]:
    """One row per outlet the PDU reported, in index order.

    Only reported outlets get a row. The device has eight, and padding to eight
    would mean inventing rows for outlets nobody has heard about — see
    :py:data:`OUTLET_UNKNOWN` for why an invented row is worse than a missing one.

    Keys arrive as strings over the wire and are sorted **numerically**, because a
    lexicographic sort puts outlet 10 between 1 and 2. There is no ten-outlet
    model in tree today, which is exactly why this would be found the hard way.
    """
    if not isinstance(outlets, dict):
        return []
    rows = []
    for key, value in outlets.items():
        try:
            index = int(key)
        except (TypeError, ValueError):
            continue
        rows.append(
            {
                "index": index,
                "on": bool(value),
                "label": OUTLET_ON if value else OUTLET_OFF,
            }
        )
    rows.sort(key=lambda r: r["index"])
    return rows


def _as_tally(value: object) -> int:
    """A running total: an int >= 0, whatever arrived.

    The counterpart to :py:func:`_as_count`, which floors at 1 because a log row
    always stands for at least one action. A tally of things that happened floors
    at 0, because "none yet" is a true and common answer.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if value >= 0 else 0


def _as_optional_float(value: object) -> Optional[float]:
    """A float, or None for anything that is not a number.

    ``bool`` is rejected before ``int`` — ``True`` would otherwise render as
    ``1.0 V``. The same order the drivers' index coercion uses, for the same
    reason: ``bool`` is a subclass of ``int`` and every naive check passes it.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _active_stage(runs: dict, stages: Optional[dict] = None) -> Optional[str]:
    """Which flowchart node pulses, if any.

    Prefers what the bench *reported* — ``stages`` is ``{run_id: stage}``, folded
    from ``run_stage`` events — and falls back to deriving one from the run state
    only for a run that reported nothing. No runs at all leaves every node dim,
    because an idle bench is genuinely not part-way through a sequence and
    pretending otherwise is the cinematic lie this module refuses.

    The preference order is the whole point of the change: a reported stage is a
    measurement of where the run is, and a stage derived from run status is a guess
    that could only ever name the brackets. Where both exist the report wins, so a
    run in EXECUTE is not overwritten by "well, its state is running".
    """
    if not runs:
        return None
    stages = stages if isinstance(stages, dict) else {}
    # Deterministic order, because dict order here is the agent's iteration
    # order and the runs list beside this flowchart is sorted. With two runs in
    # different states the old fallback took whichever happened to be last, so
    # {finished, unknown-state} lit DONE or nothing depending on nothing
    # meaningful — and the lit node could disagree with the list next to it.
    keys = sorted(runs)
    # An in-flight run owns the row: it is the one thing on this panel a person is
    # watching progress. Its *reported* stage first; only if it never reported one
    # does the coarse mapping get a say, and for a plain ``running`` state that
    # mapping deliberately has no answer.
    for key in keys:
        if runs[key] in ("running", "starting", "pending"):
            reported = stages.get(key)
            if isinstance(reported, str) and reported:
                return reported
            return _STAGE_FOR_STATE.get(runs[key])
    # Nothing in flight, so light the furthest stage any run actually reached —
    # reported where reported, derived otherwise. A name this row does not carry
    # contributes nothing rather than being forced onto a node: `stages` comes off
    # the wire from an agent that may know stages this build does not.
    reached = [
        stage
        for key in keys
        for stage in (stages.get(key) or _STAGE_FOR_STATE.get(runs[key]),)
        if stage in STAGES
    ]
    if not reached:
        return None
    return max(reached, key=STAGES.index)


def _headline_run(runs: dict) -> Optional[str]:
    """Which run the centre of the panel is about, if any.

    The same choice :py:func:`_active_stage` makes — an in-flight run first, then
    the lowest-keyed of what remains — factored out because the DUT label and the
    sequence row must agree. A panel naming one run's DUT beside another run's
    stage would be worse than naming neither.
    """
    if not runs:
        return None
    keys = sorted(runs)
    for key in keys:
        if runs[key] in ("running", "starting", "pending"):
            return key
    return keys[0]


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
    # Passed through untyped and validated per-slot in _busy_method /
    # _queue_depth, for the same reason `slots` is fetched with .get: these are
    # newer fields on a payload that arrives from another process, and an agent
    # without them must cost the rail its activity markers rather than the panel.
    busy_devices = snap.get("busy_devices")
    queued_devices = snap.get("queued_devices")
    # Last-observed activity per device, from action events rather than the status
    # poll. Same defensive .get treatment for the same reason.
    action_age = snap.get("action_age")
    last_action = snap.get("last_action")
    # {device_key: run_id} for instruments a live run declared. Passed through
    # unvalidated and checked per-slot in _enrolled_run, exactly as busy_devices
    # and queued_devices are: an older agent declares no devices, and the rail must
    # lose the IN RUN marker rather than the whole slot.
    #
    # Deliberately NOT normalised to {} here. That guard was written and then
    # removed: _enrolled_run's own isinstance check already rejects every non-dict,
    # so a second one upstream is unreachable — a mutation deleting it killed no
    # test, because no input exists that reaches one guard and not the other. An
    # untestable line that looks like a safety check is worse than no line, since
    # the next reader trusts it.
    enrolled = snap.get("enrolled")

    discoverable = _discoverable_keys()

    # The rail shows this bench, not the development bench. Membership is every
    # key the state model has a slot for, which is the union of what the agent
    # serves and what the last scan found — and both halves earn their place: a
    # served device that is absent needs its row to say NOT FOUND, and a device
    # found on the bus that nothing serves needs one to say NOT SERVED. Dropping
    # either would silently hide the exact mismatch an operator must fix.
    #
    # Until a device table arrives there is nothing to filter on, so fall back to
    # the full table rather than an empty rail: a blank column during startup
    # reads as "this bench has no instruments", which is a claim nobody has
    # checked. registry_known is precisely the "has a table arrived" flag.
    specs = _rail_specs(dev_slots) if registry_known and dev_slots else INSTRUMENTS

    slots = []
    for spec in specs:
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
                busy=_busy_method(spec["key"], busy_devices),
                queued=_queue_depth(spec["key"], queued_devices),
                recent=_recent_action(
                    spec["key"], action_age, last_action, trustworthy=trustworthy
                ),
                # Not gated on trustworthiness here: unlike an activity marker,
                # enrollment is not a claim about this instant. _slot_state marks
                # the slot stale and the renderer strikes it through, which is a
                # readable "a run owned this, and we have lost sight of it" —
                # the state most worth showing on a panel that has gone quiet.
                enrolled_run=_enrolled_run(spec["key"], enrolled),
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

    # Once, not once per node: the row below asks about it five times, and this is
    # rebuilt on every frame.
    active_stage = _active_stage(snap["runs"], snap.get("run_stages"))
    # The run the centre of the panel is about, and the two maps keyed by run id.
    # Guarded with isinstance for the reason every other map off the wire is: a
    # snapshot from an older agent carries neither key, and one from a newer or
    # confused one could carry something that is not a dict at all.
    headline_run = _headline_run(snap["runs"])
    dut_map = snap.get("run_dut")
    dut_map = dut_map if isinstance(dut_map, dict) else {}
    name_map = snap.get("run_names")
    name_map = name_map if isinstance(name_map, dict) else {}

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
        # Core harness, and its own top-level block rather than a sixth entry in
        # ``instruments``. A PDU is not something a test reads: it has no arm
        # state, no run enrolls it as a source, and the rail's whole vocabulary
        # (STANDBY, OPEN, IN RUN) says nothing true about a mains contactor. It is
        # what the bench is plugged into — peer to the board the agent runs on —
        # so it gets a fixed panel instead of competing for a rail slot with the
        # instruments a run actually measures with.
        "mains": _mains_panel(snap, trustworthy=trustworthy),
        "stages": [
            {
                "name": name,
                "active": name == active_stage,
                # Whether the run has already been through this node, so the row
                # reads as a progress track rather than a single lit box. Derived
                # from position, which is why STAGES is ordered.
                "done": (
                    active_stage in STAGES
                    and STAGES.index(name) < STAGES.index(active_stage)
                ),
            }
            for name in STAGES
        ],
        # A name the bench reported that this build's STAGES does not carry. Passed
        # through rather than dropped: a newer agent naming a stage we cannot place
        # on the row is worth saying out loud, and the alternative is a row that
        # silently shows nothing while a run is plainly in progress.
        "stage_unknown": (
            active_stage if active_stage and active_stage not in STAGES else None
        ),
        # What the headline run is testing on, and what it is called. Both empty
        # strings when unknown, with ``dut_known`` carrying the distinction the
        # label turns on: a run that declared no DUT reads UNSPECIFIED, no run at
        # all reads NO RUN, and neither may be shown as a DUT that exists.
        "dut": str(dut_map.get(headline_run) or "") if headline_run else "",
        "dut_known": headline_run in dut_map,
        "run_name": str(name_map.get(headline_run) or "") if headline_run else "",
        "runs": [{"id": k, "state": v} for k, v in sorted(snap["runs"].items())],
        "dropped_events": int(snap["dropped_events"]),
        "reconnects": int(snap.get("reconnects", 0)),
        "log": _log_lines(status),
        # The pane shows 24 rows of a stream that can run at thousands of actions
        # a second, so it must be able to say it is a summary. `actions` is how
        # many happened, `actions_folded` how many the agent deliberately
        # collapsed into a repeat count. Both are cumulative. Kept out of
        # `dropped_events`: folding is a declared summary, dropping is a loss.
        "actions": int(snap.get("actions_seen") or 0),
        "actions_folded": int(snap.get("actions_folded") or 0),
        # Link heartbeats. The only positive evidence on an idle bench that the
        # agent is still there: with nothing armed and no run, every other
        # counter here stays put whether the link is quiet or dead. Never a log
        # row — see `BenchStatus.apply_event`.
        "link_beats": int(snap.get("link_beats") or 0),
        # The banner along the bottom. Says what the bench is doing, or admits
        # it does not know — never "SYSTEM READY" on an unreachable agent.
        "operation": _operation(snap),
    }


#: How many log rows the pane shows. The pane is a fixed-height column on a
#: 1080p panel; past this the rows are off the glass and building them is work
#: nobody sees.
LOG_ROWS = 24


def _log_lines(status: Optional[BenchStatus]) -> list[dict]:
    """Recent real events for LOG.MGR, newest last.

    Real events only. The reference art fills this pane with synthetic hex
    chatter; that is fine as *decoration* in the renderer's own background
    layer, but this list is bench truth and is kept clean of it so an operator
    scanning for what actually happened is not reading invented lines.

    Most rows are now ``action`` / ``action_failed``: the agent emits one for
    every method it dispatches, so this is a live record of what the bench did
    rather than the almost-always-empty pane it used to be (before that, the only
    event kinds anything produced were ``safety_trip``, ``safety_failed`` and
    ``events_dropped`` — three things that on a healthy bench never happen).

    ``action`` and ``detail`` are re-truncated here even though the agent already
    bounded them. Not redundancy for its own sake: this function is what stands
    between the pane and a *field-length contract enforced in another process*,
    across a version boundary, on a payload that arrives from the network. An
    agent one release behind — or a future one whose limit is generous — must cost
    this pane a shortened line, not a row that runs off the panel and pushes the
    columns beside it out of alignment.
    """
    if status is None:
        return []
    return [
        {
            "kind": str(e.get("kind", "?")),
            "severity": str(e.get("severity", "info")),
            "device": str(e.get("device", "") or ""),
            "seq": e.get("seq"),
            # The device-level verb ("set_voltage"), which is the useful half of
            # a `device.call`: without it every driver method on the bench reads
            # as the same line. Falls back to the wire method so a row is never
            # blank — `agent.open` has no device verb and is still worth showing.
            "action": _action_text(e),
            # Arguments and result, already summarised and bounded by the agent.
            "detail": _clip(e.get("detail"), _DETAIL_CHARS),
            # The exception, for a failure. Separate from `detail` so the renderer
            # can colour it without parsing anything.
            "error": _clip(e.get("error"), _ERROR_CHARS),
            # How many identical actions this row stands for; >1 means the agent
            # folded a burst. Shown as "×47" rather than dropped, so a summarised
            # log reads as summarised.
            "count": _as_count(e.get("count")),
            "ok": str(e.get("kind", "")) != "action_failed",
        }
        for e in status.log[-LOG_ROWS:]
    ]


#: Bounds for the two free-text columns. Deliberately shorter than the agent's
#: own limits: this is what fits a row, not what fits an event.
_DETAIL_CHARS = 96
_ERROR_CHARS = 96
_ACTION_CHARS = 28


def _action_text(event: dict) -> str:
    """The verb a row names: the device method, else the wire method."""
    action = event.get("action")
    if isinstance(action, str) and action:
        return _clip(action, _ACTION_CHARS)
    method = event.get("method")
    return _clip(method, _ACTION_CHARS) if isinstance(method, str) else ""


def _clip(value: object, limit: int) -> str:
    """A single-line, length-bounded string, or ``""``.

    Collapses whitespace as well as truncating. A newline inside an exception
    message would otherwise make one event occupy two rows in a pane whose row
    count is how a reader knows how much happened.
    """
    if value is None:
        return ""
    flat = " ".join(str(value).split())
    if len(flat) <= limit:
        return flat
    return flat[: max(limit - 1, 1)] + "…"


def _as_count(value: object) -> int:
    """A row's repeat count: an int >= 1, whatever arrived.

    Defaults to 1 rather than 0. A row on screen always represents at least the
    one action that produced it, and a ``×0`` would read as "this did not happen"
    beside a line saying it did.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return 1
    return value if value >= 1 else 1


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
    # Reads the same ``busy`` the headline's ACTIVE rung reads, immediately below
    # the run branch that mirrors RECORDING. Without this rung the footer had no
    # notion of activity outside a *run*, so a bench driven by direct device calls
    # — which is how the bench is used interactively, and how every demo runs —
    # showed "BENCH IDLE" underneath a headline reading "ACTIVE". Two banners
    # disagreeing about whether the bench is doing something is worse than either
    # being wrong alone: it tells the operator the panel cannot be trusted.
    if snap.get("busy"):
        detail = snap.get("busy_summary") or ""
        return f"BENCH ACTIVE: {detail.upper()}" if detail else "BENCH ACTIVE"
    return "SYSTEM READY / BENCH IDLE"
