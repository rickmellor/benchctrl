"""The agent: a threaded TCP server exposing the bench.

One connection per client, one thread per connection, one worker thread per
device. Sessions are authenticated, and exactly one may hold the *writer
claim* per device; the rest are read-only observers. That preserves the
single-writer semantics the MCP layer already assumes while letting a second
client watch a long run without being able to disturb it.

Stdlib only — no asyncio, no framework. The board runs Python 3.13 with
nothing but ``pyserial`` installed, and the MCP stack cannot be installed
there because its dependencies ship compiled wheels for the wrong
architecture. Keeping the agent to the standard library is what makes it
deployable at all.
"""

from __future__ import annotations

import itertools
import json
import logging
import socket
import socketserver
import string
import threading
import time
import traceback
from typing import Any, Optional

from benchctrl.agent import dispatch
from benchctrl.agent.blobs import CHUNK_BYTES, BlobStore
from benchctrl.agent.eventbus import (
    DEFAULT_MAX_QUEUE,
    DISPLAY_MAX_QUEUE,
    EventBus,
    EventSubscriber,
)
from benchctrl.agent.recordings import PREVIEW_HZ, IteratorTable, RecordingTable
from benchctrl.agent.registry import DeviceRegistry
from benchctrl.agent.runs.engine import RunManager
from benchctrl.agent.runs.spec import RunSpec
from benchctrl.agent.safety import SafetyGovernor, TripReason
from benchctrl.agent.worker import PRIORITY_NORMAL, WorkerPool, clamp_blocking
from benchctrl.exceptions import BenchValueError
from benchctrl.net import auth as authmod
from benchctrl.net.codec import TAG as CODEC_TAG
from benchctrl.net.codec import Decoder, Encoder
from benchctrl.net.errors import PolicyError, encode_exception
from benchctrl.net.frames import FrameReader, FrameType, FrameWriter

log = logging.getLogger("benchctrl.agent.server")

AGENT_NAME = "benchctrl-agent"


def _json(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


#: Methods an observer session may call. Read-only by construction: every one
#: of these only reports state, so an observer cannot change what it observes.
#:
#: Deliberately a strict allowlist rather than a denylist of mutating verbs. A
#: new method added later defaults to *forbidden* for observers, which is the
#: safe direction — the alternative silently grants access to every verb someone
#: forgets to add to a blocklist. Same reasoning as the value-codec allowlist in
#: ``net.codec``.
OBSERVER_METHODS = frozenset(
    {
        "agent.hello",
        "agent.devices",
        "agent.status",
        "agent.time",
        "agent.discover",
        "run.list",
        "run.status",
        "run.events",
    }
)


#: Kind for an action that completed, and for one that raised. Two kinds rather
#: than one with a flag, because the log pane shows ``kind`` in its own column:
#: a reader scanning for what went wrong should not have to decode a boolean.
ACTION_KIND = "action"
ACTION_FAILED_KIND = "action_failed"

#: Severity for reading a value — a property read, a status poll, a getprops.
#: The lowest grade in :py:data:`benchctrl.agent.runs.spec.SEVERITIES`, and
#: deliberately so: a sample loop's reads are the highest-volume thing the agent
#: does, and they must be the first thing the event bus sheds.
ACTION_SEVERITY_READ = "debug"

#: Severity for a command that changed something but cannot make an output live
#: (``set_voltage``, ``rec.stats``, a run submission's bookkeeping).
ACTION_SEVERITY_COMMAND = "info"

#: Severity for an action that arms, disarms, opens, or closes. Above ordinary
#: chatter because these are the lines an operator reconstructing an incident
#: needs, and they are rare enough that keeping them costs no queue depth.
ACTION_SEVERITY_ARM = "warn"

#: Severity for an action that raised. A failure outranks the success of the same
#: verb: "we tried and could not" is the actionable half of the log.
ACTION_SEVERITY_FAILED = "warn"

#: Severity for a *failed* arm or disarm. The only action grade that reaches
#: ``alarm``, because a disarm that raised is how an output stays live.
ACTION_SEVERITY_FAILED_ARM = "alarm"

#: The highest severity an action event may ever carry.
#:
#: This is a safety property, not a style choice. The event bus sheds the least
#: important queued event first (:py:mod:`benchctrl.agent.eventbus`), and it
#: refuses an incoming event outright when nothing queued ranks *below* it. So if
#: ordinary bench traffic were graded ``critical``, a queue full of "read
#: voltage" would refuse the ``safety_trip`` that ``Governor.trip()`` publishes
#: before it disarms anything — the action log would have crowded out the one
#: event it exists to make visible. Everything here must stay strictly below
#: ``critical`` so a trip can always evict its way in.
ACTION_SEVERITY_CEILING = "alarm"

#: Severities whose events may be folded together by :py:class:`ActionCoalescer`.
#: Only sheddable grades: anything worth a ``warn`` is worth its own line.
COALESCED_SEVERITIES = frozenset({ACTION_SEVERITY_READ, ACTION_SEVERITY_COMMAND})

#: Kind for the periodic link heartbeat. The agent emits this so a *quiet* bench
#: still proves it is alive: on an idle bench with nothing armed and no run, no
#: other event is produced at all, and a consumer cannot tell "nothing is
#: happening" from "the link died three minutes ago". Both look like silence.
#:
#: A consumer must treat this kind as liveness *only* and never append it to a
#: visible log — see the note on :py:data:`LINK_SEVERITY`.
LINK_KIND = "link"

#: Severity for the link heartbeat: the lowest grade there is, for the same
#: reason reads are ``debug``. This is bookkeeping about the connection rather
#: than a fact about the bench, so it must be the *first* thing the event bus
#: sheds under pressure. A heartbeat that displaced a real bench event would
#: have inverted its own purpose.
LINK_SEVERITY = ACTION_SEVERITY_READ

#: How often the link heartbeat is emitted, as a multiple of the agent's
#: advertised ``heartbeat_s``.
#:
#: Deliberately faster than the consumer's silence budget, which is
#: ``heartbeat_s * SILENCE_HEARTBEATS`` (3.0) in
#: :py:mod:`benchctrl.dashboards.state`. At 1.0 the two rates would be equal and
#: ordinary scheduling jitter on a small board would produce false STALE
#: flapping; at 3.0 or above every heartbeat would arrive exactly as the budget
#: expired, which is the same bug with extra steps. One beat per interval against
#: a three-beat budget means two may be lost or shed before the panel warns.
LINK_INTERVAL_HEARTBEATS = 1.0

#: Kind for the periodic presence sweep: which configured devices the bench can
#: actually see on the bus right now.
#:
#: The bench pushes this rather than the dashboard polling for it. A display must
#: never be the reason an instrument gets scanned — that is the same rule that
#: keeps ``agent.discover`` off the panel's fast path — and presence is a fact
#: about the *bench*, so the bench is what should notice it changing. A dashboard
#: that opens mid-session gets the current picture from its own first inventory
#: and every change after that arrives unbidden.
PRESENCE_KIND = "presence"

#: Severity for a presence sweep that found no change: bookkeeping, shed first,
#: never logged. A sweep whose result *differs* from the last one is emitted at
#: :py:data:`PRESENCE_CHANGE_SEVERITY` instead — an instrument appearing or
#: vanishing is a real bench event and belongs in the log.
PRESENCE_SEVERITY = ACTION_SEVERITY_READ

#: Severity for a presence sweep whose result changed. ``warn`` rather than
#: ``info`` because an instrument leaving the bus mid-session is the kind of thing
#: that explains a failed run twenty minutes later, and because it must outrank
#: the ordinary read traffic it competes with for space in the log.
PRESENCE_CHANGE_SEVERITY = "warn"

#: How often the bench re-checks which instruments are on the bus, as a multiple
#: of ``heartbeat_s``. Much slower than the heartbeat: a scan enumerates USB and
#: costs ~1.4-1.65 s on the bench board against ~24 us for a liveness check, so
#: this is the one periodic task on the agent that is genuinely expensive.
#:
#: 6.0 against a 5 s heartbeat is a sweep every ~30 s, matching the cadence the
#: dashboard's own inventory poll already used — so this replaces that work
#: rather than adding to it.
PRESENCE_INTERVAL_HEARTBEATS = 6.0

#: How long one identical action signature keeps its line. A repeated read
#: inside this window increments a count instead of emitting.
ACTION_COALESCE_S = 0.5

#: How many distinct action signatures the coalescer tracks before it forgets
#: them all and starts again. Bounds the memory a pathological client can make
#: the agent hold; the counters it keeps are cumulative and survive the reset.
ACTION_MAX_SIGNATURES = 64

#: Longest rendering of one value inside a log line.
ACTION_VALUE_CHARS = 40

#: Longest ``detail`` string on an action event. The log pane is 24 rows of
#: monospace on a 1080p panel — anything past this is not read, and putting a
#: waveform on the event bus would cost the bench queue depth to deliver
#: something nobody can see.
ACTION_DETAIL_CHARS = 120

#: Longest error text. Longer than ``detail``: an exception message is the whole
#: content of a failure line, where ``detail`` is a summary beside a verb.
ACTION_ERROR_CHARS = 160

#: How many items a small container may render inline before it is reduced to
#: its shape.
_INLINE_ITEMS = 4

#: kwarg names never rendered into a log line. The agent's own token cannot
#: reach here (authentication happens in ``_authenticate``, before any request
#: is routed), so this is about a *device* method that takes a credential —
#: nothing in tree does today, and a log line is a bad place to discover the
#: first one.
_REDACT_KWARGS = frozenset({"token", "password", "passwd", "secret", "key", "api_key"})

#: Name segments that make a *method* one whose arguments are credentials, whoever
#: they are passed as. Matched against the underscore-separated segments of the
#: device method name, so ``device_login`` and ``set_api_key`` both hit and
#: ``set_keyboard_lock`` does not.
#:
#: This exists because :py:data:`_REDACT_KWARGS` is the wrong shape on its own: it
#: can only match a *name*, and a positional argument does not have one.
#: ``login(tok)`` is how that call is written in every Python calling convention,
#: and the observed failure was the live agent token rendering 39 of its 43
#: characters into ``detail``. The clip made that look bounded, which is the trap —
#: truncation is not redaction, and a test asserting the field's length would have
#: passed against it.
_CREDENTIAL_SEGMENTS = frozenset(
    {
        "login",
        "logon",
        "auth",
        "authenticate",
        "authorise",
        "authorize",
        "token",
        "password",
        "passwd",
        "passphrase",
        "secret",
        "credential",
        "credentials",
        "apikey",
        "pin",
    }
)

#: Shortest string a shape check will call a credential. Every string literal a
#: driver signature actually passes in this tree is at most 10 characters
#: (``CONTinuous``, ``RESistance``), and ``secrets.token_urlsafe(32)`` is 43, so
#: this sits in a wide gap rather than on a boundary.
_SECRET_MIN_CHARS = 20

#: Characters a base64url/hex credential is built from. A path or an SCPI string
#: contains something outside this set, which is what keeps them readable.
_SECRET_ALPHABET = frozenset(string.ascii_letters + string.digits + "-_=")

#: How much of a long string the shape check looks at. Bounded on purpose: this
#: runs on the request path and the strings reaching it include an encoded
#: recording's base64 payload.
_SECRET_SAMPLE = 64


def _is_credential_method(name: str) -> bool:
    """True when ``name`` is a method whose arguments must not be rendered.

    Graded by the *method*, because argument position carries no name to match on.
    Verified against all 389 public driver methods in tree: none matches, so this
    costs no real log line today.
    """
    return bool(set(name.lower().split("_")) & _CREDENTIAL_SEGMENTS)


def _looks_secret(text: str) -> bool:
    """True when a string has the shape of a token, whatever it was called.

    The backstop for a credential passed positionally to a method whose *name*
    gives no hint — the case :py:func:`_is_credential_method` cannot see. Keyed on
    shape: long, unbroken, drawn only from the base64url alphabet, and mixing
    upper, lower and digits. A ``token_urlsafe`` value matches essentially always;
    an SCPI string, a device key, a path and an all-caps serial number do not.

    Deliberately accepted false positive: a long mixed-case alphanumeric serial
    would render as its length instead of its value. Losing a serial from one log
    line is the cheaper error.
    """
    if len(text) < _SECRET_MIN_CHARS:
        return False
    head = text[:_SECRET_SAMPLE]
    if not set(head) <= _SECRET_ALPHABET:
        return False
    return (
        any(c.islower() for c in head)
        and any(c.isupper() for c in head)
        and any(c.isdigit() for c in head)
    )

#: Wire verbs that arm, disarm, open, or close something.
_ARM_GRADE_METHODS = frozenset(
    {
        "agent.open",
        "agent.close",
        "rec.start",
        "rec.stop",
        "run.submit",
        "run.abort",
        "iter.open",
    }
)


def _arming_call_names() -> frozenset[str]:
    """Device methods the safety governor treats as changing arm state.

    Read from the governor's own table rather than restated here. The log's idea
    of "this one could make an output live" must not be able to drift from the
    governor's, because the drift is invisible: the panel would keep grading
    ``set_output`` as ordinary chatter while the bench armed on it, and the line
    an incident review needs would be the first thing shed.
    """
    try:
        from benchctrl.agent.safety import _ARMING_CALLS

        return frozenset(_ARMING_CALLS)
    except Exception:  # pragma: no cover - the action log must never break routing
        return frozenset()


def action_severity(method: str, device_method: str = "", *, ok: bool = True) -> str:
    """Grade one dispatched action.

    Volume decides the floor and consequence decides the ceiling: a value read is
    ``debug`` because a run emits thousands of them, an arm is ``warn`` because
    there is one and it matters, and nothing is ``critical`` — see
    :py:data:`ACTION_SEVERITY_CEILING`.
    """
    arming = method in _ARM_GRADE_METHODS or (
        method == "device.call" and device_method in _arming_call_names()
    )
    if not ok:
        return ACTION_SEVERITY_FAILED_ARM if arming else ACTION_SEVERITY_FAILED
    if arming:
        return ACTION_SEVERITY_ARM
    if method == "device.call":
        # A property read and a getter both come through here; only a mutator
        # changed anything.
        return (
            ACTION_SEVERITY_COMMAND
            if dispatch.is_mutator(device_method)
            else ACTION_SEVERITY_READ
        )
    if method.startswith(("device.", "blob.", "rec.stats", "run.", "agent.", "iter.")):
        return ACTION_SEVERITY_READ if _is_read_verb(method) else ACTION_SEVERITY_COMMAND
    return ACTION_SEVERITY_COMMAND


#: Verbs that only report. Everything else defaults to ``info``, which is the
#: safe direction for a verb added later: it is one grade louder than it may
#: deserve rather than one grade quieter than it needs.
_READ_VERBS = frozenset(
    {
        "agent.hello",
        "agent.devices",
        "agent.status",
        "agent.time",
        "agent.discover",
        "device.getprops",
        "device.read_window",
        "blob.fetch",
        "rec.stats",
        "run.status",
        "run.list",
        "run.events",
        "run.artifacts",
        "run.fetch_chunk",
        "iter.next",
    }
)


def _is_read_verb(method: str) -> bool:
    return method in _READ_VERBS


def _clip(text: str, limit: int) -> str:
    """One-line, length-bounded rendering of ``text``.

    Newlines are collapsed rather than escaped: an exception message with a
    newline in it would otherwise break one log row into two, and the pane's rows
    are how a reader counts what happened.

    **Slices before it normalises.** ``" ".join(s.split())`` on the whole input
    would be a full scan and a full copy, and the inputs here include an encoded
    recording's base64 payload — megabytes, per read, in a sample loop, on the
    board. Slicing to a few times the limit first bounds the work at a constant
    regardless of what arrived; the slack absorbs whatever whitespace collapsing
    removes, so a value that fits still renders whole.
    """
    raw = text if isinstance(text, str) else str(text)
    head = raw[: limit * 4 + 8]
    flat = " ".join(head.split())
    if len(raw) <= len(head) and len(flat) <= limit:
        return flat
    return flat[: max(limit - 1, 1)] + "…"


def _summarise(value: object, *, limit: int = ACTION_VALUE_CHARS, depth: int = 0) -> str:
    """A short, bounded description of one value.

    Truncation happens **before** formatting, not after. ``_clip(str(value))``
    would be a bug with a measurable cost: a recorded window is hundreds of
    thousands of samples, and rendering it to a string only to throw all but 40
    characters away would allocate megabytes on the request path — per read, in a
    loop, on a board with 2 GB of RAM. So a container reports its *shape*
    (``list[262144]``) and only small ones are rendered inline.

    Anything that is not a JSON-ish scalar or container reports its type name.
    ``str()`` is never called on an unknown object: a driver's ``__repr__`` is
    free to query the instrument, and a log line must not put traffic on the wire
    to the DUT.
    """
    if value is None:
        return "none"
    if isinstance(value, bool):
        # Before int, which bool subclasses.
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _clip(repr(value), limit)
    if isinstance(value, str):
        if not value:
            return "''"
        # Shape check before rendering, not after: a credential that reached here
        # positionally has no name to match on, and the clip that used to bound it
        # was only ever bounding it, not hiding it.
        if _looks_secret(value):
            return f"«redacted:{len(value)}c»"
        return _clip(value, limit)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"bytes[{len(value)}]"
    if isinstance(value, (list, tuple, set, frozenset)):
        if depth or len(value) > _INLINE_ITEMS:
            return f"{type(value).__name__}[{len(value)}]"
        inner = ",".join(_summarise(v, limit=limit, depth=depth + 1) for v in value)
        return f"[{inner}]"
    if isinstance(value, dict):
        tagged = _summarise_envelope(value)
        if tagged is not None:
            return tagged
        if depth or len(value) > _INLINE_ITEMS:
            return f"dict[{len(value)}]"
        inner = ",".join(
            f"{_clip(str(k), 12)}={_summarise(v, limit=limit, depth=depth + 1)}"
            for k, v in value.items()
        )
        return "{" + inner + "}"
    return type(value).__name__


def _summarise_envelope(value: dict) -> Optional[str]:
    """Describe a :py:mod:`benchctrl.net.codec` envelope, or None if not one.

    The result reaching :py:func:`build_action_event` is the *encoded* value, so a
    recording arrives as ``{"__t": "b64", "v": "<megabytes of base64>"}`` and a
    measurement as ``{"__t": "rec", ...}``. Rendering those generically put 40
    characters of base64 on the glass — observed, not hypothesised — which is
    exactly the "waveform blob in a log line" this log must not produce, and it is
    worse than useless: it looks like data.

    So the tag is read and the *shape* reported (``b64[1048576]``). Unknown tags
    fall back to the tag name rather than to the payload, because a tag added
    later must degrade to a short label, never to its contents.
    """
    tag = value.get(CODEC_TAG)
    if not isinstance(tag, str):
        return None
    payload = value.get("v")
    if tag == "b64" and isinstance(payload, str):
        # Characters of base64, not decoded bytes: this is a log line, and
        # decoding to get a byte count would allocate the whole blob to render a
        # number nobody is measuring anything against.
        return f"b64[{len(payload)}c]"
    if tag == "f" and isinstance(payload, str):
        return payload
    if tag == "blob":
        return f"blob[{_summarise(value.get('size'), depth=1)}b]"
    if tag == "enum":
        return _clip(f"{value.get('c')}.{payload}", ACTION_VALUE_CHARS)
    if tag == "dc":
        # Observed on the board: ``info`` logged the two characters "dc". The
        # envelope has exactly three keys, so the generic dict branch rendered it
        # inline and the tag sorted first. Every dataclass-returning method on the
        # bench had the same useless line. The class name is the fact worth having,
        # and one field makes it recognisable rather than merely typed.
        return _clip(f"{value.get('c')}({_summarise_fields(value.get('f'))})", 44)
    if tag == "rec":
        # A live recording handle. ``running`` is the part an operator acts on.
        state = "running" if value.get("running") else "stopped"
        return _clip(f"rec {value.get('name')} {state}", ACTION_VALUE_CHARS)
    if tag == "map":
        items = value.get("items")
        return f"map[{len(items) if isinstance(items, (list, tuple)) else '?'}]"
    if tag == "iter":
        return f"iter#{_summarise(value.get('id'), depth=1)}"
    return _clip(tag, 16)


def _summarise_fields(fields: object) -> str:
    """One or two fields of an encoded dataclass, enough to recognise it by.

    Not all of them: ``SDM4065AInfo`` has eight, and a log row is one line beside a
    verb. Fields whose own value is another envelope or a container are skipped
    rather than descended into, because their rendering is longer than the space
    left and the class name has already carried the meaning.
    """
    if not isinstance(fields, dict):
        return ""
    shown = []
    for name, value in fields.items():
        if isinstance(value, (dict, list, tuple)) or value is None:
            continue
        shown.append(f"{_clip(str(name), 12)}={_summarise(value, limit=16, depth=1)}")
        if len(shown) == 2:
            break
    return ",".join(shown)


def _device_action(method: str, p: dict) -> str:
    """The device-level method a wire verb is carrying, if any.

    ``device.call`` is the interesting one: every driver method on the bench
    arrives under that single verb, so without this the log would read
    ``device.call`` forty times and say nothing about what the bench did.
    """
    if method == "device.call":
        name = p.get("method")
        return _clip(name, ACTION_VALUE_CHARS) if isinstance(name, str) else ""
    if method == "device.getprops":
        names = p.get("names")
        if isinstance(names, (list, tuple)) and names:
            return _clip(",".join(str(n) for n in names[:3]), ACTION_VALUE_CHARS)
        return "snapshot"
    if method == "device.read_window":
        return "read_window"
    return ""


def _summarise_args(method: str, p: dict) -> str:
    """The arguments of a ``device.call``, bounded and redacted.

    Only ``device.call``: every other verb's interesting parameter is the device
    key, which the event carries as its own field, and their parameter dicts are
    handles (blob ids, rec ids) that mean nothing on a wall panel.

    Redaction happens in three layers, because each catches what the others cannot:
    a credential *method* renders arity only (positional arguments have no name to
    match on); a credential *kwarg name* is redacted by name; and any remaining
    string is shape-checked by :py:func:`_looks_secret`. The first version had only
    the middle layer, and a token passed as ``args[0]`` went straight to the glass.
    """
    if method != "device.call":
        return ""
    args = p.get("args") or ()
    kwargs = p.get("kwargs") or {}
    if not isinstance(args, (list, tuple)):
        args = ()
    if not isinstance(kwargs, dict):
        kwargs = {}
    name = p.get("method")
    if isinstance(name, str) and _is_credential_method(name):
        # Arity, not values. That a login was attempted is the fact worth logging;
        # what it was attempted with is never worth logging.
        return f"«redacted:{len(args) + len(kwargs)} args»"
    parts = [_summarise(a) for a in args]
    for key, value in kwargs.items():
        kwname = str(key)
        shown = "«redacted»" if kwname.lower() in _REDACT_KWARGS else _summarise(value)
        parts.append(f"{_clip(kwname, 16)}={shown}")
    return _clip(",".join(parts), ACTION_DETAIL_CHARS)


def _summarise_result(method: str, device_method: str, result: object) -> str:
    """The result half of a log line.

    A credential-shaped *method* has its result withheld the same way its arguments
    are. ``get_token()`` returning a short key would otherwise render whole, and a
    length clip is no protection at all against a bounded-length secret — an 8-char
    PIN fits inside every limit this module has. Grading by the method is what makes
    that a decision rather than an accident of the value's size.

    Everything else goes through :py:func:`_summarise`, whose string branch
    shape-checks anyway, so a token returned by a method with an innocent name is
    still caught. Residual risk accepted: a short, non-random secret returned by a
    method whose name gives no hint would render. Nothing in tree does that, and no
    length or shape rule can distinguish that value from a reading.
    """
    if method == "device.call" and _is_credential_method(device_method):
        return "«redacted»"
    if method == "agent.open" and isinstance(result, dict):
        # ``dict[8]`` was the honest rendering of the surface descriptor and told
        # an operator nothing. The interesting fact about an open is WHICH driver
        # class took the device, because that is what a wrong-instrument mistake
        # looks like on the glass.
        cls = result.get("cls")
        if isinstance(cls, str) and cls:
            return _clip(cls, ACTION_VALUE_CHARS)
    return _summarise(result)


def build_action_event(
    session_id: str,
    method: str,
    p: dict,
    *,
    ok: bool,
    result: object = None,
    error: Optional[BaseException] = None,
    elapsed_ms: Optional[float] = None,
) -> dict:
    """One action, as the event bus will carry it.

    Pure and separate from the handler so the grading and the truncation can be
    asserted without a socket, a device, or a session.
    """
    action = _device_action(method, p)
    device = p.get("device")
    event = {
        "kind": ACTION_KIND if ok else ACTION_FAILED_KIND,
        "severity": action_severity(method, action, ok=ok),
        "method": method,
        "action": action,
        "device": device if isinstance(device, str) else "",
        "ok": bool(ok),
        # How many actions this line stands for. Always present, always at least
        # 1, so a consumer never has to distinguish "one action" from "a line
        # whose count was left off".
        "count": 1,
        "session": session_id,
    }
    if elapsed_ms is not None:
        event["ms"] = round(float(elapsed_ms), 1)
    if ok:
        parts = []
        args = _summarise_args(method, p)
        if args:
            parts.append(args)
        parts.append(f"→ {_summarise_result(method, action, result)}")
        event["detail"] = _clip(" ".join(parts), ACTION_DETAIL_CHARS)
    else:
        event["detail"] = ""
        event["error"] = _clip(
            f"{type(error).__name__}: {error}" if error is not None else "failed",
            ACTION_ERROR_CHARS,
        )
    return event


class ActionCoalescer:
    """Folds repeated identical actions into one line per window.

    Why this is not "emit everything and let the bus shed it"
    --------------------------------------------------------

    Shedding is the bus's answer to a *slow consumer*, and it is the right one.
    It is not an answer to a fast producer. A run polling an instrument does
    thousands of transactions a second, and an event each would make every one of
    them pay for a dict, a lock, and — inside
    :py:meth:`~benchctrl.agent.eventbus.EventSubscriber.offer` — a linear scan of
    a queue up to 256 deep looking for something to evict. That cost lands on the
    request path of the bench, per SCPI transaction, on the board. The bus would
    also then be delivering a display 24 rows can never show.

    So repetition is collapsed here, at the producer, where it is nearly free: a
    dict lookup and an integer. The first action of a burst always emits, so no
    action is ever invisible; a repeat inside the window increments a count that
    rides out on the next line of that signature as ``count`` ("read voltage
    ×47").

    What this loses, stated plainly
    -------------------------------

    If a burst simply stops, the last window's repeats never reach a ``count``.
    That tail is not silent: every action event carries ``folded``, the
    cumulative number of actions that did not get their own line, so a consumer
    can always say the log is a summary rather than a transcript. Silent
    truncation is the thing this codebase refuses; a *declared* summary is fine.
    """

    def __init__(
        self,
        *,
        window_s: float = ACTION_COALESCE_S,
        max_signatures: int = ACTION_MAX_SIGNATURES,
    ) -> None:
        self._window_s = window_s
        self._max_signatures = max_signatures
        self._lock = threading.Lock()
        self._last_emit: dict[tuple, float] = {}
        self._suppressed: dict[tuple, int] = {}
        self._folded = 0
        self._emitted = 0

    def offer(self, event: dict, *, now: Optional[float] = None) -> Optional[dict]:
        """Return the event to publish, or None when it was folded.

        Never blocks on I/O and never raises: this runs on the request path of
        every remote call.
        """
        severity = event.get("severity")
        coalescible = bool(event.get("ok")) and severity in COALESCED_SEVERITIES
        stamp = time.monotonic() if now is None else now
        signature = (
            event.get("method", ""),
            event.get("device", ""),
            event.get("action", ""),
            bool(event.get("ok")),
        )
        with self._lock:
            if not coalescible:
                event["folded"] = self._folded
                self._emitted += 1
                return event
            last = self._last_emit.get(signature)
            if last is not None and stamp - last < self._window_s:
                self._suppressed[signature] = self._suppressed.get(signature, 0) + 1
                self._folded += 1
                return None
            if signature not in self._last_emit and (
                len(self._last_emit) >= self._max_signatures
            ):
                # Forget the table rather than grow it. The cumulative counters
                # survive, so the honesty of `folded` does not depend on the size
                # of this dict.
                self._last_emit.clear()
                self._suppressed.clear()
            self._last_emit[signature] = stamp
            event["count"] = 1 + self._suppressed.pop(signature, 0)
            event["folded"] = self._folded
            self._emitted += 1
            return event

    def stats(self) -> dict:
        with self._lock:
            return {
                "emitted": self._emitted,
                "folded": self._folded,
                "signatures": len(self._last_emit),
                "window_s": self._window_s,
            }


class Session:
    """Per-connection state.

    ``observer`` sessions are read-only status consumers — the HDMI dashboard is
    the motivating case. Two things make them different from a normal client,
    and both are safety properties rather than conveniences:

    1. **Their traffic does not count as operator contact.** ``_serve`` calls
       ``governor.touch()`` on every inbound frame from a normal session, so a
       polling observer would pin ``seconds_since_contact`` near zero and the
       deadman could never trip. A status display must not be able to keep an
       armed bench alive.
    2. **They may only call** :py:data:`OBSERVER_METHODS`. No opening, no
       claiming, no device calls.
    """

    _ids = itertools.count(1)

    def __init__(
        self,
        agent: "BenchAgent",
        sock: socket.socket,
        address,
        *,
        observer: bool = False,
    ) -> None:
        self.agent = agent
        self.sock = sock
        self.address = address
        self.session_id = f"s-{next(self._ids)}"
        self.reader = FrameReader(sock)
        self.writer = FrameWriter(sock)
        self.authenticated = False
        self.observer = observer
        self.claims: set[str] = set()
        self.opened_at = time.monotonic()
        self._event_seq = itertools.count(1)

    @property
    def peer(self) -> str:
        try:
            return self.address[0]
        except (TypeError, IndexError):  # pragma: no cover
            return str(self.address)

    def holds(self, device_key: str) -> bool:
        return device_key in self.claims

    def send_event(self, event: dict) -> None:
        """Write one event frame. **Blocks** — only the sender thread calls it.

        Called from this session's :py:class:`~benchctrl.agent.eventbus.EventSubscriber`
        thread, never from a producer. ``sendall`` has no timeout, so calling
        this from the governor's trip path is what used to let a stalled client
        delay a disarm.
        """
        payload = dict(event)
        payload.setdefault("seq", next(self._event_seq))
        payload.setdefault("ts_mono", round(time.monotonic(), 6))
        payload.setdefault("ts_utc", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        payload.setdefault("severity", "info")
        self.writer.send(FrameType.EVT, _json(payload))


class BenchAgent:
    """Owns the devices, the workers, and the safety governor."""

    def __init__(
        self,
        registry: DeviceRegistry,
        *,
        token: Optional[str] = None,
        deadman_s: float = 15.0,
        heartbeat_s: float = 5.0,
        max_blocking_s: float = 5.0,
        max_recording_s: float = 300.0,
        blob_store: Optional[BlobStore] = None,
        runs_dir=None,
        llm_base_url: str = "",
    ) -> None:
        self.registry = registry
        self.token = token
        self.deadman_s = deadman_s
        self.heartbeat_s = heartbeat_s
        self.max_blocking_s = max_blocking_s
        self.workers = WorkerPool(max_blocking_s=max_blocking_s)
        self.blobs = blob_store or BlobStore()
        self.recordings = RecordingTable(max_recording_s=max_recording_s)
        self.iterators = IteratorTable()
        self.runs = RunManager(runs_dir)
        self.llm_base_url = llm_base_url
        self.failures = authmod.FailureTracker()
        # Built before the governor: the governor's event sink publishes onto it.
        self.events = EventBus()
        # Folds a sample loop's repeated reads into one line per window, at the
        # producer, before the bus ever sees them. See ActionCoalescer.
        self.actions = ActionCoalescer()
        self.governor = SafetyGovernor(
            deadman_s=deadman_s, on_event=self._broadcast_event
        )
        self._sessions: dict[str, Session] = {}
        self._claims: dict[str, str] = {}  # device -> session id
        self._lock = threading.RLock()
        self._deadman_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # The agent's one VISA ResourceManager, built lazily by
        # :py:meth:`resource_manager`. Its own lock, not ``self._lock``: see
        # there for why.
        self._rm: Optional[Any] = None
        self._rm_tried = False
        self._rm_lock = threading.Lock()
        # When the last link heartbeat went out. None means "never", so the
        # first due check on the deadman thread emits immediately rather than
        # making a freshly-connected dashboard wait out a full interval.
        self._last_link_mono: Optional[float] = None
        # Presence sweeps. ``_last_presence_keys`` is None until the first sweep
        # completes, which is what distinguishes "the bus changed" from "this is
        # the first time anybody looked" — the latter is not a change and must not
        # be reported as one.
        self._last_presence_mono: Optional[float] = None
        self._last_presence_keys: Optional[list[str]] = None
        self._presence_running = False
        # Guards ``_presence_running`` only. A separate lock from ``self._lock``
        # for the same reason ``_rm_lock`` is separate: the session table is taken
        # on every connect and disconnect, and a bus scan must not be able to
        # stall those.
        self._presence_lock = threading.Lock()

    # --- lifecycle ------------------------------------------------------

    def start_deadman(self) -> None:
        if self._deadman_thread is not None:
            return
        self._stop.clear()
        self._deadman_thread = threading.Thread(
            target=self._deadman_loop, name="deadman", daemon=True
        )
        self._deadman_thread.start()

    def _deadman_loop(self) -> None:
        while not self._stop.wait(0.5):
            # The trip check comes first and keeps its own handler, so nothing
            # added below can delay or skip it. This thread's first duty is
            # driving an abandoned bench safe; the heartbeat is a courtesy to a
            # display and is strictly subordinate to that.
            try:
                if self.governor.should_trip():
                    self.trip(TripReason.HEARTBEAT_LOST)
            except Exception:  # pragma: no cover
                log.exception("deadman loop raised")
            # Reuses this thread rather than starting a second one: it already
            # ticks at 0.5 s, it is already guaranteed to be running whenever the
            # agent is, and publishing is a non-blocking enqueue onto the event
            # bus. A dedicated thread would cost a thread on a 2 GB board and add
            # a lifecycle to get wrong, to do the same work on the same period.
            try:
                self._maybe_emit_link()
            except Exception:  # pragma: no cover
                log.exception("agent: link heartbeat raised")
            # Kicked off from here but deliberately NOT run here: a bus scan
            # enumerates USB and takes ~1.4-1.65 s on the bench board, and this
            # thread's first duty is tripping an abandoned bench within its
            # deadman window. Running the sweep inline would stall trip detection
            # for the length of a scan, every sweep, forever — the one thing this
            # loop must never do. ``_maybe_start_presence_sweep`` only checks a
            # clock and hands off to a short-lived worker.
            try:
                self._maybe_start_presence_sweep()
            except Exception:  # pragma: no cover
                log.exception("agent: presence sweep raised")

    def _maybe_emit_link(self) -> None:
        """Emit a link heartbeat if one is due and anyone is listening.

        Gated on an observer actually being attached. A bench with no dashboard
        connected produces no heartbeats at all — there is nobody to reassure,
        and a bus that carries an event every few seconds forever is a cost paid
        by every consumer, including the run engine's own subscribers.

        The interval is derived from the agent's advertised ``heartbeat_s``
        rather than being a constant of its own, because that is the figure the
        agent puts in its WELCOME and the figure a consumer builds its silence
        budget from. Two independent numbers that must stay in a ratio is a
        drift waiting to happen: change the config and the panel starts crying
        wolf, with nothing in either file to say why.
        """
        interval = self.heartbeat_s * LINK_INTERVAL_HEARTBEATS
        if interval <= 0:
            return  # heartbeats disabled by configuration
        now = time.monotonic()
        if self._last_link_mono is not None and now - self._last_link_mono < interval:
            return
        # Checked after the clock, not before: an idle bench with no dashboard
        # would otherwise leave ``_last_link_mono`` stale, and the first frame
        # after one connects would be judged overdue by however long the agent
        # had been sitting there alone.
        if self.observer_count() <= 0:
            self._last_link_mono = now
            return
        self._last_link_mono = now
        self.events.publish(
            {
                "kind": LINK_KIND,
                "severity": LINK_SEVERITY,
                # What the heartbeat is *for*: a consumer can compare this to the
                # interval it was promised and decide the link is late without
                # having to know the agent's configuration.
                "heartbeat_s": self.heartbeat_s,
                # Included so a quiet beat still carries the two facts a status
                # panel would otherwise have to poll for. Cheap and already in
                # memory: neither touches an instrument.
                "armed": sorted(self.governor.armed_devices),
                "observers": self.observer_count(),
            }
        )

    def _maybe_start_presence_sweep(self) -> None:
        """Start a bus presence sweep if one is due, on its own thread.

        Cheap by construction: checks two clocks and a flag, then returns. The
        scan itself runs in :py:meth:`_presence_sweep` on a short-lived worker so
        the deadman loop is never held up by USB enumeration.

        Gated on an observer being attached, exactly like the heartbeat. With no
        dashboard connected there is nobody to inform, and a ~1.5 s USB scan every
        30 s on a 2 GB board is not a cost to pay for nobody — it also competes
        with instrument I/O on the same bus. ``_last_presence_mono`` is stamped
        even when the sweep is skipped, so a dashboard connecting does not
        immediately trigger a scan that was "overdue" from an unwatched hour.

        ``_presence_running`` prevents overlap: on a bench where a scan takes
        longer than the interval (a slow hub, a device that is slow to answer),
        an ungated timer would pile scans onto each other until the bus was
        saturated by presence checks — the failure mode where the monitoring
        becomes the outage.
        """
        interval = self.heartbeat_s * PRESENCE_INTERVAL_HEARTBEATS
        if interval <= 0:
            return  # presence sweeps disabled by configuration
        now = time.monotonic()
        if (
            self._last_presence_mono is not None
            and now - self._last_presence_mono < interval
        ):
            return
        if self.observer_count() <= 0:
            self._last_presence_mono = now
            return
        with self._presence_lock:
            if self._presence_running:
                # Still working through the previous sweep. Not stamped: the next
                # tick should reconsider promptly rather than wait out another
                # full interval on top of an already-slow scan.
                return
            self._presence_running = True
        self._last_presence_mono = now
        threading.Thread(
            target=self._presence_sweep, name="presence-sweep", daemon=True
        ).start()

    def _presence_sweep(self) -> None:
        """Scan the bus and publish which configured devices are on it.

        Runs on its own thread. Never raises: this is a background courtesy, and
        a scan that failed must not take down the agent that was only trying to
        be helpful about it.

        Publishes on *every* sweep, not only on change, because a consumer needs
        to know the answer is current — a panel that only hears about changes
        cannot distinguish "still four instruments" from "nobody has looked since
        boot". The severity is what differs: an unchanged sweep is read-grade
        bookkeeping that gets shed first and never logged, while a change is
        ``warn`` and belongs on the screen.

        ``probe=False``. A presence sweep repeats forever, and probing writes
        ``AT+DEV.TYPE?`` at whatever is behind a generic bridge; doing that on a
        timer is how a periodic health check becomes a periodic stray command at
        an instrument that may be mid-measurement. The consequence is honest and
        deliberate: the QR10x sits behind a driverless CH340 with no VID/PID
        identity, so a non-probing scan can confirm *the bridge* is present but
        cannot confirm the QR10x itself. It is reported as undetermined rather
        than present, which is the same distinction the panel already draws — an
        absence of evidence must not render as evidence of absence.
        """
        try:
            from benchctrl import discovery

            found = discovery.inventory(
                probe=False, resource_manager=self.resource_manager()
            )
            by_key = found.get("by_device_key") or {}
            served = list(self.registry.keys)
            # Only the keys this agent serves: a sweep is a statement about the
            # configured bench, and reporting every stray tty as "present" would
            # bury the four lines anybody cares about.
            present = sorted(k for k in served if k in by_key)
            changed = self._last_presence_keys is not None and (
                self._last_presence_keys != present
            )
            first = self._last_presence_keys is None
            self._last_presence_keys = present
            self.events.publish(
                {
                    "kind": PRESENCE_KIND,
                    "severity": (
                        PRESENCE_CHANGE_SEVERITY if changed else PRESENCE_SEVERITY
                    ),
                    "present": present,
                    "served": served,
                    # So a consumer can tell a real change from its first frame,
                    # where everything is "new" and none of it is news.
                    "changed": bool(changed),
                    "first": bool(first),
                }
            )
            if changed:
                log.warning(
                    "agent: bus presence changed — now present: %s (of %s)",
                    present,
                    served,
                )
        except Exception:  # noqa: BLE001 - a background courtesy must not raise
            log.warning("agent: presence sweep failed", exc_info=True)
        finally:
            with self._presence_lock:
                self._presence_running = False

    def trip(self, reason: TripReason) -> dict:
        return self.governor.trip(
            reason,
            self.registry.open_devices(),
            {k: self.workers.get(k) for k in self.registry.open_devices()},
        )

    def resource_manager(self) -> Optional[Any]:
        """The agent's single VISA ``ResourceManager``, or None if there can't be one.

        Exists because ``pyvisa.ResourceManager()`` is a **singleton** — a second
        call returns the same underlying object — so any code that makes "its
        own" manager and closes it afterwards closes *everyone's*, and every
        already-open instrument's next ``query()`` fails with ``InvalidSession:
        Invalid session handle``. That is not hypothetical: on the bench board, a
        read-only HDMI status dashboard calling ``agent.discover`` on its 30 s
        inventory timer killed a live 4-wire resistance sweep mid-run (Eastwood
        QR10x + Siglent SDM4065A), two setpoints in, because the scan created and
        closed a manager the DMM's session depended on. **The dashboard must
        never be able to disrupt bench function** — that is a project invariant,
        and this is the object that upholds it: one long-lived manager, owned by
        the agent, handed to every scan so no scan has cause to make one.

        Lazy, and never fatal. pyvisa stays an *optional* dependency (a bench of
        serial-only instruments is a valid bench), so importing it at module
        scope would break agents that have no VISA hardware. A missing pyvisa or
        an unusable backend returns None, which every discovery entry point
        already treats as "make your own if you need one" — a VISA problem must
        not take down request routing.

        Uses a dedicated ``self._rm_lock`` rather than ``self._lock``.
        ``self._lock`` guards the session and claim tables and is taken by
        ``register_session`` / ``drop_session`` / ``claim`` / ``release`` on
        every connect and disconnect; ``ResourceManager()`` enumerates USB and
        can take ~1.6 s, so holding the session lock across construction would
        stall unrelated connects behind a bus scan. One lock for one field is
        also deadlock-free by construction: nothing is called while it is held
        except pyvisa's constructor, which knows nothing of this agent.
        """
        with self._rm_lock:
            # ``_rm_tried`` and not ``_rm is None``: a bench without a working
            # VISA backend would otherwise retry the ~1.6 s construction on
            # every 30 s inventory poll, forever.
            if self._rm_tried:
                if self._rm is None or self._rm_is_live(self._rm):
                    return self._rm
                # The manager is open-but-dead: something closed the singleton
                # out from under us. Rebuild rather than hand back a handle
                # whose every use raises. Without this the agent never
                # recovers, because ``_rm_tried`` stays latched for the life of
                # the process — on the bench board that showed up as the supply,
                # load and DMM all reading NOT FOUND until a service restart.
                log.warning(
                    "agent: the shared VISA manager was closed by something "
                    "else; rebuilding it"
                )
                self._rm = None
            self._rm_tried = True
            try:
                import pyvisa

                self._rm = pyvisa.ResourceManager()
                log.info("agent: opened the shared VISA resource manager")
            except Exception as exc:  # noqa: BLE001 - optional dependency
                log.info("agent: no shared VISA resource manager (%s)", exc)
                self._rm = None
            return self._rm

    @staticmethod
    def _rm_is_live(rm: Any) -> bool:
        """Whether a ResourceManager still has a valid session.

        Reads ``rm.session``, which pyvisa turns into an ``InvalidSession`` raise
        once the manager is closed. Measured at ~24 us on the bench board — cheap
        enough to check on the path of every inventory poll, unlike
        ``list_resources()``, which enumerates USB and takes ~1.4 s.

        A manager with no ``session`` attribute at all (a fake, or a backend that
        does not expose one) counts as live: the point is to catch the specific
        closed-singleton failure, not to reject anything unfamiliar.
        """
        try:
            # Assigned rather than read bare: the *raise* is the signal, but a
            # bare attribute read reads as dead code (and ruff flags it as one).
            # The value itself is deliberately not judged — only whether getting
            # it raised.
            _ = rm.session
        except AttributeError:
            return True
        except Exception:  # noqa: BLE001 - InvalidSession and friends
            return False
        return True

    def shutdown(self) -> None:
        self._stop.set()
        if self._deadman_thread is not None:
            self._deadman_thread.join(timeout=2.0)
            self._deadman_thread = None
        # Anything still armed must not be left that way.
        if self.governor.any_armed:
            log.warning("agent: devices armed at shutdown — driving to safe state")
            self.trip(TripReason.SHUTDOWN)
        self.runs.abort_all("agent shutdown")
        self.iterators.close_all()
        self.workers.stop_all()
        self.registry.close_all()
        # Only now, and only here. Closing the shared manager invalidates every
        # VISA session made from it (that is the whole hazard this object
        # exists to avoid), so it must come after the trip that drives the
        # bench safe and after ``close_all`` has released the instruments —
        # never on a scan. Idempotent: the drivers' own ``close`` may have
        # closed the same singleton already, and pyvisa swallows the second
        # close as ``InvalidSession``.
        self._close_resource_manager()
        self.blobs.close()
        # Last: the trip above emits events, and closing the bus first would
        # discard the record of the agent driving the bench safe on the way out.
        self.events.close(timeout=1.0)

    def _close_resource_manager(self) -> None:
        """Release the shared VISA manager, at shutdown only. Never raises.

        Also latches ``_rm_tried``, so a scan arriving on a request thread
        during teardown gets None and makes nothing new, rather than resurrecting
        a manager the agent has finished with.
        """
        with self._rm_lock:
            rm, self._rm = self._rm, None
            self._rm_tried = True
        if rm is None:
            return
        try:
            rm.close()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            log.debug("agent: error closing the VISA resource manager", exc_info=True)

    # --- sessions -------------------------------------------------------

    def observer_count(self) -> int:
        """How many observer sessions are currently attached.

        The agent already knows this — an observer registers itself like any
        other session — so "is a dashboard listening?" needs no new handshake or
        registration verb. Using the session table rather than a separate
        subscribe call also means the answer cannot drift from reality: a
        dashboard whose socket died is out of ``_sessions`` by the time
        ``drop_session`` returns, so it stops being counted without having to
        remember to say goodbye.
        """
        with self._lock:
            return sum(1 for s in self._sessions.values() if s.observer)

    def register_session(self, session: Session) -> None:
        with self._lock:
            self._sessions[session.session_id] = session
        # Each session gets its own bounded queue and sender thread, so its
        # socket is never on a producer's critical path. An observer (the HDMI
        # dashboard) gets a shallow, droppable queue: it wants current state,
        # and falling behind must cost it freshness, never cost the bench.
        self.events.subscribe(
            EventSubscriber(
                session.session_id,
                session.send_event,
                max_queue=DISPLAY_MAX_QUEUE if session.observer else DEFAULT_MAX_QUEUE,
                droppable=session.observer,
            )
        )

    def drop_session(self, session: Session) -> None:
        # Stop the sender thread first so it cannot write to a closing socket.
        self.events.unsubscribe(session.session_id, timeout=0.5)
        with self._lock:
            self._sessions.pop(session.session_id, None)
            released = [d for d, s in self._claims.items() if s == session.session_id]
            for device in released:
                self._claims.pop(device, None)
        for device in released:
            log.info("agent: %s released claim on %s", session.session_id, device)
        # If that was the last client and something is armed, the deadman
        # thread will trip after the grace period. Shorten the clock so the
        # grace actually applies from the disconnect, not from last contact.
        if released and self.governor.any_armed:
            log.warning(
                "agent: client holding an armed device disconnected; "
                "safe state in <= %.0fs unless it reconnects",
                self.governor.grace_for_disconnect(),
            )

    def _broadcast_event(self, event: dict) -> None:
        """Fan out to every session without ever blocking the caller.

        This is the governor's event sink, and ``Governor.trip()`` calls it
        **before** driving instruments to a safe state, on the deadman thread.
        It therefore must not touch a socket: it enqueues and returns. See
        :py:mod:`benchctrl.agent.eventbus`.
        """
        self.events.publish(event)

    def broadcast_action(
        self,
        session_id: str,
        method: str,
        p: dict,
        *,
        ok: bool,
        result: object = None,
        error: Optional[BaseException] = None,
        elapsed_ms: Optional[float] = None,
    ) -> Optional[dict]:
        """Record one dispatched action. Returns the event published, or None.

        On the request path of every remote call, so it must cost nothing the
        bench can feel: it builds a dict, folds repeats
        (:py:class:`ActionCoalescer`), and hands the result to the bus, which
        enqueues and returns. No socket, no lock held across I/O, nothing
        unbounded.

        Wrapped in ``except Exception`` because the log is strictly less important
        than the call it describes. A bug in the summarising must not turn a
        successful ``set_voltage`` into an error the client sees — and worse, must
        not turn a *disarm* into one.
        """
        try:
            event = build_action_event(
                session_id,
                method,
                p,
                ok=ok,
                result=result,
                error=error,
                elapsed_ms=elapsed_ms,
            )
            # Separate name: `offer` returns None when it folded the event into a
            # previous line's count, which is not the same type as the event that
            # went in. Reusing `event` made this a dict-vs-Optional[dict] error.
            published = self.actions.offer(event)
            if published is None:
                return None
            self.events.publish(published)
            return published
        except Exception:  # noqa: BLE001 - never fail a bench action over its log
            log.exception("agent: action log failed for %s", method)
            return None

    def claim(self, session: Session, device_key: str) -> dict:
        self.registry.entry(device_key)
        with self._lock:
            holder = self._claims.get(device_key)
            if holder is not None and holder != session.session_id:
                raise PolicyError(
                    f"{device_key} is claimed by session {holder}; this session "
                    f"is a read-only observer until it is released"
                )
            self._claims[device_key] = session.session_id
            session.claims.add(device_key)
        return {"device": device_key, "claimed": True, "session": session.session_id}

    def release(self, session: Session, device_key: str) -> dict:
        with self._lock:
            if self._claims.get(device_key) == session.session_id:
                self._claims.pop(device_key, None)
            session.claims.discard(device_key)
        return {"device": device_key, "claimed": False}

    # --- codecs ---------------------------------------------------------

    def encoder(self, session: Session) -> Encoder:
        return Encoder(
            store_blob=lambda data: self.blobs.put(data).blob_id,
            register_iterator=lambda gen: self.iterators.register(
                "unknown", gen, session_id=session.session_id
            ).iter_id,
            register_recording=lambda rec: self.recordings.start(
                "unknown", rec, session_id=session.session_id
            ).rec_id,
        )

    @staticmethod
    def decoder() -> Decoder:
        return Decoder()


class _Handler(socketserver.BaseRequestHandler):
    """One client connection."""

    agent: BenchAgent  # injected by the server factory

    def handle(self) -> None:  # noqa: C901 - protocol dispatch is inherently branchy
        session = Session(self.agent, self.request, self.client_address)
        log.info("agent: connection from %s (%s)", session.peer, session.session_id)
        try:
            if not self._authenticate(session):
                return
            self.agent.register_session(session)
            self._serve(session)
        except Exception as exc:  # noqa: BLE001
            log.info("agent: session %s ended: %r", session.session_id, exc)
        finally:
            self.agent.drop_session(session)
            session.writer.close()
            try:
                self.request.close()
            except OSError:
                pass
            log.info("agent: %s disconnected", session.session_id)

    # --- handshake ------------------------------------------------------

    def _authenticate(self, session: Session) -> bool:
        agent = self.agent
        peer = session.peer

        if agent.failures.is_blocked(peer):
            remaining = agent.failures.seconds_remaining(peer)
            log.warning("agent: rejecting tarpitted %s (%.0fs left)", peer, remaining)
            self._send_error(
                session, 0, PolicyError(f"too many failures; retry in {remaining:.0f}s")
            )
            return False

        frame = session.reader.read_frame(timeout=authmod.HANDSHAKE_TIMEOUT)
        if frame is None or frame[0] != FrameType.HELLO:
            log.warning("agent: %s did not send HELLO", peer)
            return False
        hello = json.loads(frame[1] or b"{}")

        version_error = authmod.check_version(hello)
        if version_error:
            self._send_error(session, 0, PolicyError(version_error))
            return False

        # A client may ask for the read-only observer role. Self-declared on
        # purpose: it only ever *removes* capability, so there is nothing to gain
        # by lying about it. Still token-authenticated below — an observer can
        # read run history and the device inventory, which is not public.
        if bool(hello.get("observer", False)):
            session.observer = True
            log.info("agent: %s is a read-only observer session", session.peer)

        if not agent.token:
            # No token configured: accept, but be loud about it. This is a
            # bench that anyone on the subnet can drive.
            log.warning(
                "agent: no token configured — %s authenticated without proof. "
                "Anyone on this network can drive the instruments.",
                peer,
            )
            session.authenticated = True
            self._send_welcome(session)
            return True

        challenge, nonce_s = authmod.build_challenge(AGENT_NAME)
        session.writer.send(FrameType.CHALLENGE, _json(challenge))

        frame = session.reader.read_frame(timeout=authmod.HANDSHAKE_TIMEOUT)
        if frame is None or frame[0] != FrameType.AUTH:
            agent.failures.record_failure(peer)
            return False
        presented = json.loads(frame[1] or b"{}").get("mac", "")

        if not authmod.verify_mac(agent.token, hello.get("nonce_c", ""), nonce_s, presented):
            agent.failures.record_failure(peer)
            log.warning("agent: bad token from %s", peer)
            self._send_error(session, 0, PolicyError("authentication failed"))
            return False

        agent.failures.record_success(peer)
        session.authenticated = True
        self._send_welcome(session)
        return True

    def _send_welcome(self, session: Session) -> None:
        agent = self.agent
        session.writer.send(
            FrameType.WELCOME,
            _json(
                {
                    "session": session.session_id,
                    "agent": AGENT_NAME,
                    # Echoed so a client can assert it got the role it asked
                    # for rather than discovering it via a PolicyError later.
                    "observer": session.observer,
                    "heartbeat_s": agent.heartbeat_s,
                    "deadman_s": agent.deadman_s,
                    "clock_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "devices": agent.registry.describe(),
                    "limits": {
                        "max_blocking_s": agent.max_blocking_s,
                        "max_recording_s": agent.recordings.max_recording_s,
                        "chunk_bytes": CHUNK_BYTES,
                    },
                }
            ),
        )

    # --- main loop ------------------------------------------------------

    def _serve(self, session: Session) -> None:
        agent = self.agent
        while True:
            frame = session.reader.read_frame(timeout=max(agent.deadman_s * 2, 30.0))
            if frame is None:
                return
            frame_type, payload = frame
            # An observer's traffic must NOT count as operator contact. A status
            # display polling every second would otherwise pin
            # seconds_since_contact near zero, and should_trip() — which is
            # `any_armed and seconds_since_contact > deadman_s` — could never
            # fire. The bench would stay armed after the real client died,
            # because the thing reporting on it kept it alive.
            if not session.observer:
                agent.governor.touch()

            if frame_type == FrameType.PING:
                session.writer.send(FrameType.PONG)
                continue
            if frame_type == FrameType.REQ:
                self._handle_request(session, json.loads(payload or b"{}"))
                continue
            if frame_type == FrameType.CANCEL:
                continue  # best-effort; the worker checks job.cancelled
            log.debug("agent: ignoring frame type 0x%02X", frame_type)

    def _handle_request(self, session: Session, request: dict) -> None:
        """Dispatch one request, and record that it happened.

        Every remote action funnels through :py:meth:`_route`, which is why the
        action log is taken here rather than in each driver: one place, and a verb
        added later is logged without anybody remembering to log it.

        The event is emitted *after* the reply is on the wire. Building it is
        cheap but not free, and the client's latency is the bench's latency — a
        run's inner loop should not wait on the summarising of the previous
        response. Nothing depends on the ordering: the bus is a separate
        transport with its own per-subscriber threads.
        """
        req_id = request.get("id", 0)
        method = request.get("m", "")
        params = request.get("p") or {}
        started = time.monotonic()
        try:
            result, props = self._route(session, method, params)
            body: dict = {"id": req_id, "r": result}
            if props is not None:
                body["props"] = props
            session.writer.send(FrameType.RSP, _json(body))
        except Exception as exc:  # noqa: BLE001 - relayed to the client
            self._send_error(
                session,
                req_id,
                exc,
                device=params.get("device"),
                method=params.get("method") or method,
            )
            # A *failed* call is logged even from an observer: a read-only session
            # attempting a write is exactly the kind of thing the pane should show.
            self.agent.broadcast_action(
                session.session_id,
                method,
                params,
                ok=False,
                error=exc,
                elapsed_ms=(time.monotonic() - started) * 1000.0,
            )
            return
        if session.observer:
            # A successful observer call is not a bench action — the allowlist
            # makes that structural — and logging it is a feedback loop with the
            # pane it feeds: the dashboard polls agent.status, agent.devices and
            # agent.discover on its own timer, which at 24 visible rows would fill
            # the entire log with the display describing itself and push every
            # real bench action off the glass within seconds. Excluded as a whole
            # category rather than rate-limited, so there is no partial silence to
            # declare: nothing an observer succeeds at is ever logged.
            return
        self.agent.broadcast_action(
            session.session_id,
            method,
            params,
            ok=True,
            result=result,
            elapsed_ms=(time.monotonic() - started) * 1000.0,
        )

    def _send_error(self, session, req_id, exc, device=None, method=None) -> None:
        payload = encode_exception(
            exc,
            device=device,
            method=method,
            traceback_text="".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )[-4000:],
        )
        try:
            session.writer.send(FrameType.ERR, _json({"id": req_id, "e": payload}))
        except Exception:  # noqa: BLE001
            pass

    # --- routing --------------------------------------------------------

    def _route(self, session: Session, method: str, p: dict):
        agent = self.agent

        if session.observer and method not in OBSERVER_METHODS:
            raise PolicyError(
                f"{method!r} is not available to an observer session; observers "
                f"are read-only status consumers and may call only: "
                f"{', '.join(sorted(OBSERVER_METHODS))}"
            )

        if method == "agent.hello":
            return {"agent": AGENT_NAME, "devices": agent.registry.describe()}, None
        if method == "agent.devices":
            return agent.registry.describe(), None
        if method == "agent.discover":
            from benchctrl import discovery

            # The agent's own manager, never a fresh one: a scan that built and
            # closed its own would close this singleton and invalidate every
            # instrument session on the bench. See
            # :py:meth:`BenchAgent.resource_manager` for the sweep it killed.
            return discovery.inventory(
                resource_manager=agent.resource_manager()
            ), None
        if method == "agent.status":
            return {
                "safety": agent.governor.status(),
                "workers": agent.workers.stats(),
                "blobs": agent.blobs.stats(),
                "recordings": agent.recordings.describe(),
                # Which devices are open, per poll. The registry's full
                # ``describe()`` rides in WELCOME once, but open state is the one
                # part of it that changes while a session runs, and a consumer
                # that only ever saw the WELCOME copy believed nothing was ever
                # open — an instrument under active use reported as merely
                # configured. Deliberately ``sessions()`` and not ``describe()``:
                # see that method for why the surface does not belong here.
                "devices": agent.registry.sessions(),
            }, None
        if method == "agent.time":
            return {"utc": time.time(), "mono": time.monotonic()}, None
        if method == "agent.open":
            entry = agent.registry.open(p["device"], **(p.get("open") or {}))
            return entry.to_dict(), None
        if method == "agent.close":
            key = p["device"]
            agent.governor.trip(
                TripReason.OPERATOR,
                {key: agent.registry.open_devices().get(key)},
                {key: agent.workers.get(key)},
            )
            return {"device": key, "closed": agent.registry.close(key)}, None
        if method == "agent.claim":
            return agent.claim(session, p["device"]), None
        if method == "agent.release":
            return agent.release(session, p["device"]), None

        if method == "device.call":
            return self._device_call(session, p)
        if method == "device.read_window":
            return self._read_window(p), None
        if method == "device.getprops":
            key = p["device"]
            obj = agent.registry.get(key)
            surface = agent.registry.surface_of(key)
            names = tuple(p.get("names") or surface.snapshot_props)
            raw = dispatch.snapshot_properties(obj, surface, names)
            return agent.encoder(session).encode(raw), None

        if method == "blob.fetch":
            return self._blob_fetch(session, p), None
        if method == "blob.release":
            return {"released": agent.blobs.release(p["blob"])}, None

        if method == "rec.start":
            return self._rec_start(session, p), None
        if method == "rec.stop":
            return self._rec_stop(session, p), None
        if method == "rec.stats":
            return self._rec_stats(p), None
        if method == "rec.release":
            return {"released": agent.recordings.release(p["rec_id"])}, None

        if method == "run.submit":
            return self._run_submit(session, p), None
        if method == "run.status":
            return self.agent.runs.get(p["run_id"]).status_dict(), None
        if method == "run.list":
            return agent.runs.list(), None
        if method == "run.events":
            store = agent.runs.store_for(p["run_id"])
            return store.events_since(int(p.get("since_seq", 0)),
                                      limit=int(p.get("limit", 500))), None
        if method == "run.abort":
            engine = agent.runs.get(p["run_id"])
            engine.abort(p.get("reason", "operator"))
            return {"run_id": engine.run_id, "aborting": True}, None
        if method == "run.artifacts":
            store = agent.runs.store_for(p["run_id"])
            return {
                "run_id": p["run_id"],
                "chunks": store.chunks(),
                "phases": store.phases(),
                "info": store.info(),
            }, None
        if method == "run.fetch_chunk":
            return self._run_fetch_chunk(session, p), None

        if method == "iter.open":
            return self._iter_open(session, p), None
        if method == "iter.next":
            return self._iter_next(p), None
        if method == "iter.close":
            return {"closed": agent.iterators.close(int(p["iter_id"]))}, None

        raise PolicyError(f"unknown method {method!r}")

    # --- device calls ---------------------------------------------------

    def _device_call(self, session: Session, p: dict):
        agent = self.agent
        key = p["device"]
        name = p.get("method", "")
        obj = agent.registry.get(key)
        surface = agent.registry.surface_of(key)

        # Property read — a distinct path on purpose. If unknown names fell
        # through to a callable, `enable_output`'s `current_limit is None`
        # gate would always see a bound method and arm with no limit set.
        if name in surface.properties:
            raw = dispatch.snapshot_properties(obj, surface, (name,))[name]
            return agent.encoder(session).encode(raw), None

        dispatch.check_callable(surface, name)
        dispatch.check_writer(surface, name, session.holds(key))

        decoder = agent.decoder()
        args = [decoder.decode(a) for a in (p.get("args") or [])]
        kwargs = {k: decoder.decode(v) for k, v in (p.get("kwargs") or {}).items()}

        # Clamp caller-supplied blocking durations so the safety lane is
        # never starved for longer than max_blocking_s.
        args, kwargs = _clamp_durations(name, args, kwargs, agent.max_blocking_s)

        fn = getattr(obj, name)
        worker = agent.workers.get(key)
        result = worker.submit(
            lambda: fn(*args, **kwargs),
            priority=PRIORITY_NORMAL,
            timeout=agent.max_blocking_s * 4,
            label=f"{key}.{name}",
        )
        agent.governor.observe_call(
            key, name, tuple(args), kwargs, session_id=session.session_id
        )

        encoder = agent.encoder(session)
        encoded = encoder.encode(result)
        props = None
        if p.get("want_props", True) and surface.snapshot_props:
            props = encoder.encode(dispatch.snapshot_properties(obj, surface))
        return encoded, props

    # --- runs -----------------------------------------------------------

    def _run_submit(self, session: Session, p: dict) -> dict:
        """Hand the bench a long experiment and let go of it."""
        agent = self.agent
        spec = RunSpec.from_dict(p["spec"])
        if not session.holds(spec.device):
            raise PolicyError(
                f"submitting a run for {spec.device} requires the writer claim "
                f"— call agent.claim first"
            )
        device = agent.registry.get(spec.device)
        pdu = pdu_worker = None
        if spec.switched_outlets:
            pdu_key = self._resolve_pdu_key(spec)
            # A **second** claim, on the PDU's own key. Without this a session
            # holding only the Arc could submit a spec that switches mains, and
            # `run.submit` would become the one path into a mains contactor that
            # skips the writer gate every other route enforces. Checked before
            # the device is opened, so a refusal costs nothing.
            if not session.holds(pdu_key):
                raise PolicyError(
                    f"this run switches mains outlets "
                    f"{sorted(spec.switched_outlets)}, which requires the "
                    f"writer claim on {pdu_key} as well as on {spec.device} — "
                    f"call agent.claim for both"
                )
            pdu = agent.registry.get(pdu_key)
            pdu_worker = agent.workers.get(pdu_key)
        engine = agent.runs.submit(
            spec,
            device,
            worker=agent.workers.get(spec.device),
            governor=agent.governor,
            on_event=agent._broadcast_event,
            clock_scale=float(p.get("clock_scale", 1.0)),
            pdu=pdu,
            pdu_worker=pdu_worker,
        )
        if spec.llm.enabled:
            from benchctrl.agent.llm.supervisor import build_supervisor

            supervisor = build_supervisor(engine, base_url=agent.llm_base_url)
            if supervisor is not None:
                engine.attach_llm(supervisor.start())
        engine.start()
        return {"run_id": engine.run_id, "spec_sha256": spec.sha256}

    def _resolve_pdu_key(self, spec: RunSpec) -> str:
        """Which registered device this run's outlet setpoints refer to.

        Refuses ambiguity rather than picking. On a bench with two switched PDUs
        "outlet 3" names two different physical contactors, and guessing means
        cutting power to the wrong DUT — a failure the operator would read as a
        flaky device, not as a wiring question.
        """
        from benchctrl.agent.registry import SWITCHED_PDU_KEYS

        served = [k for k in self.agent.registry.keys if k in SWITCHED_PDU_KEYS]
        if not served:
            raise BenchValueError(
                f"this run switches mains outlets "
                f"{sorted(spec.switched_outlets)} but this agent serves no "
                f"switched PDU (it serves: {self.agent.registry.keys})"
            )
        if len(served) > 1:
            raise BenchValueError(
                f"this agent serves more than one switched PDU ({served}), so "
                f"'outlet 3' is ambiguous. The spec must name the PDU."
            )
        return served[0]

    def _run_fetch_chunk(self, session: Session, p: dict) -> dict:
        """Expose one recorded chunk as a blob for the host to pull."""
        from pathlib import Path as _Path

        agent = self.agent
        store = agent.runs.store_for(p["run_id"])
        idx = int(p["idx"])
        match = next((c for c in store.chunks() if c["idx"] == idx), None)
        if match is None:
            raise BenchValueError(f"run {p['run_id']} has no chunk {idx}")
        data = _Path(match["path"]).read_bytes()
        info = agent.blobs.put(data, content_type="application/x-opensmu")
        return {"idx": idx, **info.to_dict()}

    def _read_window(self, p: dict) -> dict:
        """Drain a measurement window, returning a code-keyed dict.

        A dedicated verb because the local method keys its result by *the
        caller's own channel objects*, and object identity cannot cross a
        wire. Codes travel; the proxy rebuilds the caller's mapping.
        """
        agent = self.agent
        key = p["device"]
        obj = agent.registry.get(key)
        codes = list(p.get("channels") or ())
        duration = clamp_blocking(float(p.get("duration_s", 0.0)), agent.max_blocking_s)
        worker = agent.workers.get(key)
        result = worker.submit(
            lambda: obj.read_window(codes, duration),
            timeout=duration + agent.max_blocking_s * 2,
            label=f"{key}.read_window",
        )
        return {
            _code_of(channel): list(values) for channel, values in (result or {}).items()
        }

    # --- recordings -----------------------------------------------------

    def _rec_start(self, session: Session, p: dict) -> dict:
        agent = self.agent
        key = p["device"]
        if not session.holds(key):
            raise PolicyError(
                f"recording {key} mutates device state — call agent.claim first"
            )
        agent.recordings.check_duration(p.get("expected_s"))
        obj = agent.registry.get(key)
        channels = tuple(p.get("channels") or ())
        name = p.get("name", "recording")
        worker = agent.workers.get(key)
        rec = worker.submit(
            lambda: obj.start_recording(name=name, channels=channels or None),
            label=f"{key}.start_recording",
        )
        handle = agent.recordings.start(
            key, rec, name=name, session_id=session.session_id
        )
        agent.governor.set_recording(key, True)
        return handle.to_dict()

    def _rec_stop(self, session: Session, p: dict) -> dict:
        agent = self.agent
        handle = agent.recordings.get(p["rec_id"])
        obj = agent.registry.get(handle.device_key)
        worker = agent.workers.get(handle.device_key)
        worker.submit(obj.stop_recording, label=f"{handle.device_key}.stop_recording")
        agent.governor.set_recording(handle.device_key, False)

        data = handle.recording.to_bytes()
        info = agent.blobs.put(data, content_type="application/x-opensmu")
        agent.recordings.finish(
            handle.rec_id, blob_id=info.blob_id, sha256=info.sha256, size=info.size
        )
        out = handle.to_dict()
        out["summary"] = {
            ch.code: _stats_dict(handle.recording.statistics(ch))
            for ch in handle.recording.channels
        }
        return out

    def _rec_stats(self, p: dict) -> dict:
        handle = self.agent.recordings.get(p["rec_id"])
        out: dict = {"rec_id": handle.rec_id, "counts": handle.counts()}
        code = p.get("channel")
        if code:
            out["statistics"] = _stats_dict(handle.recording.statistics(code))
        if p.get("preview"):
            out["preview"] = handle.preview(code)
        return out

    # --- blobs ----------------------------------------------------------

    def _blob_fetch(self, session: Session, p: dict) -> dict:
        agent = self.agent
        blob_id = p["blob"]
        info = agent.blobs.info(blob_id)
        session.writer.send(FrameType.BLOB_HDR, _json({"id": p.get("id", 0), **info.to_dict()}))
        try:
            for chunk in agent.blobs.iter_chunks(blob_id):
                session.writer.send(FrameType.BLOB_CHUNK, chunk)
        except Exception as exc:  # noqa: BLE001
            session.writer.send(
                FrameType.BLOB_END,
                _json({"blob": blob_id, "ok": False, "error": repr(exc)}),
            )
            raise
        session.writer.send(FrameType.BLOB_END, _json({"blob": blob_id, "ok": True}))
        return info.to_dict()

    # --- iterators ------------------------------------------------------

    def _iter_open(self, session: Session, p: dict) -> dict:
        agent = self.agent
        key = p["device"]
        if not session.holds(key):
            raise PolicyError(f"streaming {key} requires the writer claim")
        obj = agent.registry.get(key)
        kwargs = {k: v for k, v in (p.get("kwargs") or {}).items()}
        gen = obj.stream(**kwargs)
        handle = agent.iterators.register(key, gen, session_id=session.session_id)
        return {"iter_id": handle.iter_id}

    def _iter_next(self, p: dict) -> dict:
        handle = self.agent.iterators.get(int(p["iter_id"]))
        items = handle.take(int(p.get("max", 64)))
        return {
            "items": [_sample_dict(s) for s in items],
            "exhausted": handle.exhausted,
            "error": handle.error,
        }


def _clamp_durations(name: str, args: list, kwargs: dict, limit: float):
    """Bound the blocking duration of calls that take one."""
    if name in ("read_raw", "read_window", "read_for") and args:
        first = args[0]
        if isinstance(first, (int, float)):
            args = [clamp_blocking(float(first), limit)] + list(args[1:])
    for key in ("seconds", "duration_s", "timeout"):
        if key in kwargs and isinstance(kwargs[key], (int, float)):
            kwargs[key] = clamp_blocking(float(kwargs[key]), limit * 4)
    return args, kwargs


def _code_of(channel) -> str:
    """Reduce a channel object (or string) to its two-letter code."""
    code = getattr(channel, "code", None)
    return code if isinstance(code, str) else str(channel)


def _stats_dict(stats) -> dict:
    return {
        "sample_count": stats.sample_count,
        "duration": stats.duration,
        "min": stats.min,
        "max": stats.max,
        "average": stats.average,
        "rms": stats.rms,
        "energy": stats.energy,
        "charge": stats.charge,
    }


def _sample_dict(sample) -> dict:
    return {
        "channel": getattr(getattr(sample, "channel", None), "code", None),
        "timestamp": getattr(sample, "timestamp", None),
        "value": getattr(sample, "value", None),
    }


class _ThreadedTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class AgentServer:
    """Binds a socket and serves an agent on it."""

    def __init__(
        self,
        agent: BenchAgent,
        *,
        host: str = "0.0.0.0",
        port: int = 9737,
    ) -> None:
        self.agent = agent
        handler = type("_BoundHandler", (_Handler,), {"agent": agent})
        self._server = _ThreadedTCPServer((host, port), handler)
        self._thread: Optional[threading.Thread] = None

    @property
    def address(self) -> tuple[str, int]:
        return self._server.server_address[:2]

    @property
    def port(self) -> int:
        return self.address[1]

    def start(self) -> AgentServer:
        self.agent.start_deadman()
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="agent-server", daemon=True
        )
        self._thread.start()
        log.info("agent: listening on %s:%d", *self.address)
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        self.agent.shutdown()

    def __enter__(self) -> AgentServer:
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()
