"""Declarative run specifications.

A run is data, not code. That is deliberate: the thing executing it is
unattended on a bench for hours, possibly driving power into a DUT, and the
host that submitted it may be asleep in another room. A JSON document can be
validated before anything is energised, hashed so the artifact bundle records
exactly what ran, and re-run months later to reproduce a result.

The safety envelope lives here rather than in the engine, so it is declared
up front, checked before the first setpoint, and impossible for a later
phase — or the LLM advisor — to widen.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Optional

from benchctrl.exceptions import BenchValueError

SCHEMA_VERSION = 1

#: Phase modes. ``emulator`` and any recording are mutually exclusive at the
#: device level — see the note on A-1 in :py:class:`Phase`.
MODES = ("idle", "cv", "cc", "emulator")

OPS = ("<", "<=", ">", ">=", "==", "!=")

SEVERITIES = ("debug", "info", "warn", "alarm", "critical")

#: Dead time after a phase switches mains, before that phase starts measuring.
#: Not tuning — a correctness default. A DUT coming up on mains draws inrush and
#: then boots, so the first samples after an outlet switch describe a supply
#: settling rather than the thing under test. Defaulting to zero would make the
#: opening samples of every power-cycle phase garbage, and garbage that *looks*
#: like data is worse than a gap. Overridable per phase, including to zero.
DEFAULT_OUTLET_SETTLE_S = 3.0


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise BenchValueError(f"invalid run spec: {message}")


def _as_float(obj: Any, *fields: str) -> None:
    """Normalise numeric fields to float on a frozen dataclass.

    Without this, ``chunk_s=60`` and ``chunk_s=60.0`` produce specs that
    compare equal but serialise differently ("60" vs "60.0"), so
    :py:attr:`RunSpec.sha256` would change across a JSON round trip. The
    hash is what ties a result bundle to the spec that produced it, so it
    has to be stable regardless of how the author wrote their numbers.
    """
    for name in fields:
        value = getattr(obj, name)
        if value is not None and not isinstance(value, float):
            object.__setattr__(obj, name, float(value))


def _outlet_key(index: Any) -> int:
    """Coerce one outlet key, rejecting aggregates and bools.

    JSON object keys are always strings, so ``{"3": true}`` from a round-tripped
    spec has to mean outlet 3 — but ``"all"`` must not become anything at all.
    A ``bool`` is refused before ``int`` for the reason in
    :py:func:`_as_outlets`.
    """
    if isinstance(index, bool):
        raise BenchValueError(
            f"invalid run spec: outlet index {index!r} is a bool; True would "
            f"silently become outlet 1"
        )
    if isinstance(index, str):
        try:
            index = int(index)
        except ValueError:
            raise BenchValueError(
                f"invalid run spec: outlet index {index!r} is not a number. "
                f"Aggregates like 'all', 'b1' and 'b2' are inexpressible on "
                f"purpose — `oltctrl index all act off` de-powers everything."
            ) from None
    if not isinstance(index, int):
        raise BenchValueError(
            f"invalid run spec: outlet index {index!r} must be an int"
        )
    _require(index >= 1, f"outlet index {index} is not a positive index")
    return index


def _normalise_outlet_setpoints(setpoints: dict) -> None:
    """Rewrite a phase's ``outlets`` keys to ints, in place.

    JSON object keys are strings, so ``{"outlets": {3: True}}`` serialises as
    ``{"3": true}`` and comes back with a string key. Left alone, the spec's
    ``sha256`` would differ before and after a round trip — and that hash is
    the only thing tying an archived result bundle to the spec that produced
    it. Normalising on construction makes both spellings converge.

    Also the reason the engine can look up ``outlets[idx]`` by int without
    caring where the spec came from.
    """
    outlets = setpoints.get("outlets")
    if not isinstance(outlets, dict) or not outlets:
        return
    rewritten = {_outlet_key(k): v for k, v in outlets.items()}
    _require(
        len(rewritten) == len(outlets),
        f"phase outlets has duplicate indices after normalisation: "
        f"{sorted(outlets)}",
    )
    setpoints["outlets"] = {k: rewritten[k] for k in sorted(rewritten)}


def _as_outlets(value: Any) -> list[int]:
    """Normalise an outlet collection to sorted, unique, plain ints.

    Sorted and de-duplicated so ``(3, 2, 3)`` and ``(2, 3)`` hash identically
    — the same stability requirement :py:func:`_as_float` exists for.

    ``bool`` is rejected **before** ``int``, exactly as the driver does:
    ``True`` is an ``int`` in Python and would silently become outlet 1. A
    spec that meant "yes, outlets" and got "outlet 1" would authorise cutting
    a specific piece of hardware nobody named.
    """
    outlets: set[int] = set()
    for item in value or ():
        if isinstance(item, bool) or not isinstance(item, int):
            raise BenchValueError(
                f"invalid run spec: safety.allowed_outlets must contain plain "
                f"ints, got {item!r}. Aggregates like 'all' are inexpressible "
                f"on purpose — one line can de-power the whole bench."
            )
        _require(
            item >= 1,
            f"safety.allowed_outlets: outlet {item} is not a positive index",
        )
        outlets.add(item)
    return sorted(outlets)


@dataclass(frozen=True)
class Condition:
    """A threshold that must hold for ``for_s`` before it counts.

    The dwell time is not decoration. Measurement noise crosses any
    threshold briefly; an abort triggered by one stray sample would make
    long unattended runs useless.
    """

    channel: Optional[str] = None
    metric: Optional[str] = None
    op: str = "<"
    value: float = 0.0
    for_s: float = 0.0
    reason: str = ""

    def __post_init__(self) -> None:
        _as_float(self, "value", "for_s")
        _require(self.op in OPS, f"unknown operator {self.op!r}; valid: {list(OPS)}")
        _require(
            bool(self.channel) != bool(self.metric),
            "a condition needs exactly one of 'ch' or 'metric'",
        )
        _require(self.for_s >= 0, "for_s must be >= 0")

    @property
    def source(self) -> str:
        return self.channel or self.metric or ""

    def matches(self, sample: float) -> bool:
        if self.op == "<":
            return sample < self.value
        if self.op == "<=":
            return sample <= self.value
        if self.op == ">":
            return sample > self.value
        if self.op == ">=":
            return sample >= self.value
        if self.op == "==":
            return sample == self.value
        return sample != self.value

    def describe(self) -> str:
        dwell = f" for {self.for_s:g}s" if self.for_s else ""
        return f"{self.source} {self.op} {self.value:g}{dwell}"

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"op": self.op, "value": self.value}
        if self.channel:
            d["ch"] = self.channel
        if self.metric:
            d["metric"] = self.metric
        if self.for_s:
            d["for_s"] = self.for_s
        if self.reason:
            d["reason"] = self.reason
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Condition:
        return cls(
            channel=d.get("ch"),
            metric=d.get("metric"),
            op=str(d.get("op", "<")),
            value=float(d.get("value", 0.0)),
            for_s=float(d.get("for_s", 0.0)),
            reason=str(d.get("reason", "")),
        )


@dataclass(frozen=True)
class Safety:
    """The envelope. Deterministic, and nothing may widen it mid-run."""

    max_voltage_V: float = 5.0
    max_current_A: float = 1.0
    max_duration_s: float = 86_400.0
    max_board_temp_C: float = 85.0
    abort_if: tuple[Condition, ...] = ()
    #: Mains outlets a phase in this run may switch. **Empty means none** —
    #: an allowlist, not a limit, so a run that never mentions outlets cannot
    #: acquire the ability to cut power through a typo. This is the run-level
    #: envelope and it is checked *in addition to* the driver's own
    #: ``allowed_outlets``: the driver says what this deployment may ever
    #: touch, the spec says what this experiment is allowed to.
    allowed_outlets: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        _as_float(
            self, "max_voltage_V", "max_current_A", "max_duration_s", "max_board_temp_C"
        )
        _require(self.max_voltage_V > 0, "safety.max_voltage_V must be > 0")
        _require(self.max_current_A > 0, "safety.max_current_A must be > 0")
        _require(self.max_duration_s > 0, "safety.max_duration_s must be > 0")
        object.__setattr__(
            self, "allowed_outlets", tuple(_as_outlets(self.allowed_outlets))
        )

    def check_setpoints(self, setpoints: dict) -> None:
        """Reject a phase whose setpoints breach the envelope."""
        volts = setpoints.get("voltage_V")
        if volts is not None and volts > self.max_voltage_V:
            raise BenchValueError(
                f"phase requests {volts} V but safety.max_voltage_V is "
                f"{self.max_voltage_V} V"
            )
        amps = setpoints.get("current_limit_A", setpoints.get("current_A"))
        if amps is not None and amps > self.max_current_A:
            raise BenchValueError(
                f"phase requests {amps} A but safety.max_current_A is "
                f"{self.max_current_A} A"
            )
        self._check_outlets(setpoints.get("outlets"))

    def _check_outlets(self, outlets: Any) -> None:
        """Reject a phase that switches an outlet outside the envelope.

        Note what is checked: the *keys*, not the values. Switching an outlet
        **off** is as much a state change as switching it on — it de-powers
        whatever is plugged in — so an unlisted outlet is refused in either
        direction. A "you may only turn things off" exemption would be the
        obvious shortcut and it is wrong.
        """
        if not outlets:
            return
        if not isinstance(outlets, dict):
            raise BenchValueError(
                f"invalid run spec: phase setpoint 'outlets' must be a mapping "
                f"of outlet index -> bool, got {type(outlets).__name__}"
            )
        for index, state in outlets.items():
            idx = _outlet_key(index)
            if not isinstance(state, bool):
                raise BenchValueError(
                    f"invalid run spec: phase requests outlet {idx} -> "
                    f"{state!r}; must be a bool. A truthy string would "
                    f"energise an outlet a spec meant to cut."
                )
            if idx not in self.allowed_outlets:
                raise BenchValueError(
                    f"phase switches outlet {idx} but safety.allowed_outlets "
                    f"is {list(self.allowed_outlets)}. Widening this is a "
                    f"deliberate decision about which mains outlets an "
                    f"unattended run may switch — not a typo to paper over."
                )

    def to_dict(self) -> dict:
        d = {
            "max_voltage_V": self.max_voltage_V,
            "max_current_A": self.max_current_A,
            "max_duration_s": self.max_duration_s,
            "max_board_temp_C": self.max_board_temp_C,
            "abort_if": [c.to_dict() for c in self.abort_if],
        }
        # Emitted **only when non-empty**, following the precedent in
        # `RunSpec.to_dict`. `RunSpec.sha256` hashes this dict, and that hash
        # is what ties an archived result bundle to the spec that produced it.
        # An unconditional key would silently re-hash every spec ever archived,
        # breaking that tie for runs that have nothing to do with outlets.
        if self.allowed_outlets:
            d["allowed_outlets"] = list(self.allowed_outlets)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Safety:
        return cls(
            max_voltage_V=float(d.get("max_voltage_V", 5.0)),
            max_current_A=float(d.get("max_current_A", 1.0)),
            max_duration_s=float(d.get("max_duration_s", 86_400.0)),
            max_board_temp_C=float(d.get("max_board_temp_C", 85.0)),
            abort_if=tuple(Condition.from_dict(c) for c in d.get("abort_if", ())),
            allowed_outlets=tuple(d.get("allowed_outlets", ())),
        )


@dataclass(frozen=True)
class Sampling:
    """What to record, how often, and how finely to chunk it."""

    channels: tuple[str, ...] = ("mc", "mv")
    chunk_s: float = 300.0
    metric_period_s: float = 10.0
    preview_hz: float = 20.0
    record: bool = True

    def __post_init__(self) -> None:
        _as_float(self, "chunk_s", "metric_period_s", "preview_hz")
        object.__setattr__(self, "channels", tuple(self.channels))
        _require(self.chunk_s > 0, "sampling.chunk_s must be > 0")
        _require(self.metric_period_s > 0, "sampling.metric_period_s must be > 0")
        # The board holds ~40 bytes per sample in a Python list. Five
        # minutes at full Arc rates is already ~110 MB; an hour would OOM.
        _require(
            self.chunk_s <= 900,
            f"sampling.chunk_s={self.chunk_s} is too large — chunks are held "
            f"in RAM before being written, and the board has a ~500 MB budget "
            f"(roughly 22 minutes at full rate). Use 300 s.",
        )

    def to_dict(self) -> dict:
        return {
            "channels": list(self.channels),
            "chunk_s": self.chunk_s,
            "metric_period_s": self.metric_period_s,
            "preview_hz": self.preview_hz,
            "record": self.record,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Sampling:
        return cls(
            channels=tuple(d.get("channels", ("mc", "mv"))),
            chunk_s=float(d.get("chunk_s", 300.0)),
            metric_period_s=float(d.get("metric_period_s", 10.0)),
            preview_hz=float(d.get("preview_hz", 20.0)),
            record=bool(d.get("record", True)),
        )


@dataclass(frozen=True)
class Phase:
    """One stage of a run."""

    name: str
    mode: str = "idle"
    setpoints: dict = field(default_factory=dict)
    duration_s: float = 0.0
    exit: tuple[Condition, ...] = ()
    emulator: dict = field(default_factory=dict)
    #: Dead time after this phase's mains transition, before it starts
    #: measuring. ``None`` means :py:data:`DEFAULT_OUTLET_SETTLE_S`, which is
    #: **not** zero. ``0.0`` is a distinct, explicit "I know this DUT needs no
    #: settling" — which is why this is ``Optional`` rather than a float
    #: defaulting to 0: absent and zero mean opposite things here, and
    #: conflating them would make the safe default unreachable.
    settle_s: Optional[float] = None

    def __post_init__(self) -> None:
        _as_float(self, "duration_s", "settle_s")
        _require(
            self.settle_s is None or self.settle_s >= 0,
            f"phase {self.name!r}: settle_s must be >= 0",
        )
        _normalise_outlet_setpoints(self.setpoints)
        _require(bool(self.name), "every phase needs a name")
        _require(
            self.mode in MODES, f"phase {self.name!r}: unknown mode {self.mode!r}"
        )
        _require(
            self.duration_s > 0 or self.exit,
            f"phase {self.name!r} has neither a duration nor an exit condition — "
            f"it would run forever",
        )
        # A settle window on a phase that switches nothing would be a setting
        # that silently does nothing — the operator waits for a delay that never
        # happens and reads the first samples as settled. Rejected rather than
        # generalised into an all-purpose dwell: `settle_s` means "let mains
        # come up", and giving it a second meaning would make the power-cycle
        # case impossible to reason about.
        _require(
            self.settle_s is None or bool(self.setpoints.get("outlets")),
            f"phase {self.name!r} sets settle_s but switches no outlet. "
            f"settle_s is the dead time after a mains transition; on a phase "
            f"with no transition it would do nothing at all.",
        )
        if self.mode == "emulator":
            _require(
                bool(self.emulator.get("profile")),
                f"phase {self.name!r}: emulator mode needs emulator.profile",
            )

    @property
    def records(self) -> bool:
        """Whether this phase captures a full recording.

        Emulator phases never do. The emulator's 100 Hz control loop and the
        recording reader thread contend for the same transport and deadlock
        within ~100 ms (KNOWN_LIMITATIONS A-1). Relocating both to the board
        moves the deadlock with them, so the engine forbids the combination
        structurally rather than discovering it at runtime.
        """
        return self.mode != "emulator"

    def to_dict(self) -> dict:
        d = {
            "name": self.name,
            "mode": self.mode,
            "setpoints": dict(self.setpoints),
            "duration_s": self.duration_s,
            "exit": [c.to_dict() for c in self.exit],
        }
        if self.emulator:
            d["emulator"] = dict(self.emulator)
        # Conditional for the same reason as `Safety.allowed_outlets`: an
        # unconditional key re-hashes every spec ever archived. Keyed on
        # `is not None`, not truthiness — an explicit `settle_s=0.0` is a
        # decision about a DUT and has to survive the round trip.
        if self.settle_s is not None:
            d["settle_s"] = self.settle_s
        return d

    @property
    def effective_settle_s(self) -> float:
        """The dead time the engine will actually wait. Never negative."""
        if not self.setpoints.get("outlets"):
            return 0.0
        return DEFAULT_OUTLET_SETTLE_S if self.settle_s is None else self.settle_s

    @classmethod
    def from_dict(cls, d: dict) -> Phase:
        return cls(
            name=str(d.get("name", "")),
            mode=str(d.get("mode", "idle")),
            setpoints=dict(d.get("setpoints", {})),
            duration_s=float(d.get("duration_s", 0.0)),
            exit=tuple(Condition.from_dict(c) for c in d.get("exit", ())),
            emulator=dict(d.get("emulator", {})),
            settle_s=(
                float(d["settle_s"]) if d.get("settle_s") is not None else None
            ),
        )


@dataclass(frozen=True)
class Rule:
    """A watched condition that emits an event when it fires."""

    when: Condition
    kind: str = "rule_fired"
    severity: str = "warn"
    text: str = ""

    def __post_init__(self) -> None:
        _require(
            self.severity in SEVERITIES,
            f"rule severity {self.severity!r}; valid: {list(SEVERITIES)}",
        )

    def to_dict(self) -> dict:
        return {
            "when": self.when.to_dict(),
            "emit": {"kind": self.kind, "severity": self.severity, "text": self.text},
        }

    @classmethod
    def from_dict(cls, d: dict) -> Rule:
        emit = d.get("emit", {})
        return cls(
            when=Condition.from_dict(d.get("when", {})),
            kind=str(emit.get("kind", "rule_fired")),
            severity=str(emit.get("severity", "warn")),
            text=str(emit.get("text", "")),
        )


@dataclass(frozen=True)
class LLMConfig:
    """How much the on-board model may participate.

    The defaults are set by arithmetic, not taste. At ~5.3 tok/s prompt
    processing and ~3.3 tok/s generation, a 400-token prompt costs ~75 s
    before the first output token and 120 output tokens costs another ~36 s.
    A turn is therefore ~2 minutes. Anything that assumes faster is wrong.
    """

    enabled: bool = False
    model: str = ""
    min_interval_s: float = 60.0
    max_call_s: float = 180.0
    max_prompt_tokens: int = 400
    max_output_tokens: int = 120
    call_on: tuple[str, ...] = ("phase_end",)

    def __post_init__(self) -> None:
        _as_float(self, "min_interval_s", "max_call_s")
        object.__setattr__(self, "call_on", tuple(self.call_on))
        _require(self.min_interval_s >= 30, "llm.min_interval_s must be >= 30")
        _require(
            self.max_prompt_tokens <= 2048,
            "llm.max_prompt_tokens above 2048 makes a turn take many minutes "
            "on this hardware",
        )

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "model": self.model,
            "min_interval_s": self.min_interval_s,
            "max_call_s": self.max_call_s,
            "max_prompt_tokens": self.max_prompt_tokens,
            "max_output_tokens": self.max_output_tokens,
            "call_on": list(self.call_on),
        }

    @classmethod
    def from_dict(cls, d: dict) -> LLMConfig:
        return cls(
            enabled=bool(d.get("enabled", False)),
            model=str(d.get("model", "")),
            min_interval_s=float(d.get("min_interval_s", 60.0)),
            max_call_s=float(d.get("max_call_s", 180.0)),
            max_prompt_tokens=int(d.get("max_prompt_tokens", 400)),
            max_output_tokens=int(d.get("max_output_tokens", 120)),
            call_on=tuple(d.get("call_on", ("phase_end",))),
        )


@dataclass(frozen=True)
class RunSpec:
    """A complete, validated, hashable run definition."""

    name: str
    device: str = "otii_arc"
    dut: str = ""
    description: str = ""
    safety: Safety = field(default_factory=Safety)
    sampling: Sampling = field(default_factory=Sampling)
    phases: tuple[Phase, ...] = ()
    rules: tuple[Rule, ...] = ()
    llm: LLMConfig = field(default_factory=LLMConfig)

    def __post_init__(self) -> None:
        object.__setattr__(self, "phases", tuple(self.phases))
        object.__setattr__(self, "rules", tuple(self.rules))
        _require(bool(self.name), "a run needs a name")
        _require(bool(self.phases), "a run needs at least one phase")
        names = [p.name for p in self.phases]
        _require(len(names) == len(set(names)), f"phase names must be unique: {names}")
        for phase in self.phases:
            self.safety.check_setpoints(phase.setpoints)
        # Settle windows included: they are unattended bench time like any
        # other, and a run that power-cycles fifty times spends real minutes
        # there. Excluding them would let a spec exceed its own envelope.
        total = self.total_duration_s
        _require(
            total <= self.safety.max_duration_s,
            f"phases total {total:g}s but safety.max_duration_s is "
            f"{self.safety.max_duration_s:g}s",
        )

    @property
    def total_duration_s(self) -> float:
        """Wall-clock the phase list asks for, settle windows included.

        A settle window is dead time *added* to a phase, not carved out of its
        duration: the point of ``duration_s`` is how long the DUT is measured,
        and shortening that because mains had to come up would quietly change
        the experiment. But it is still time the bench spends unattended, so it
        counts against ``safety.max_duration_s``.
        """
        return sum(p.duration_s + p.effective_settle_s for p in self.phases)

    @property
    def switched_outlets(self) -> frozenset[int]:
        """Every mains outlet any phase in this run switches.

        Derived, never serialised — so it cannot change :py:attr:`sha256`. It
        exists so the agent can decide *before the run starts* whether this
        spec needs a PDU and a second writer claim, rather than discovering it
        at the phase that switches mains.
        """
        return frozenset(
            idx
            for phase in self.phases
            for idx in (phase.setpoints.get("outlets") or {})
        )

    def phase(self, index: int) -> Phase:
        return self.phases[index]

    def to_dict(self) -> dict:
        return {
            "schema": SCHEMA_VERSION,
            "name": self.name,
            "device": self.device,
            "dut": self.dut,
            "description": self.description,
            "safety": self.safety.to_dict(),
            "sampling": self.sampling.to_dict(),
            "phases": [p.to_dict() for p in self.phases],
            "rules": [r.to_dict() for r in self.rules],
            "llm": self.llm.to_dict(),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    @property
    def sha256(self) -> str:
        """Content hash — recorded in the artifact bundle so a result can
        always be traced to the exact spec that produced it."""
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, d: dict) -> RunSpec:
        schema = d.get("schema", SCHEMA_VERSION)
        _require(
            schema == SCHEMA_VERSION,
            f"spec schema {schema} is not supported (this build reads "
            f"{SCHEMA_VERSION})",
        )
        return cls(
            name=str(d.get("name", "")),
            device=str(d.get("device", "otii_arc")),
            dut=str(d.get("dut", "")),
            description=str(d.get("description", "")),
            safety=Safety.from_dict(d.get("safety", {})),
            sampling=Sampling.from_dict(d.get("sampling", {})),
            phases=tuple(Phase.from_dict(p) for p in d.get("phases", ())),
            rules=tuple(Rule.from_dict(r) for r in d.get("rules", ())),
            llm=LLMConfig.from_dict(d.get("llm", {})),
        )

    @classmethod
    def from_json(cls, text: str) -> RunSpec:
        try:
            return cls.from_dict(json.loads(text))
        except json.JSONDecodeError as exc:
            raise BenchValueError(f"run spec is not valid JSON: {exc}") from exc
