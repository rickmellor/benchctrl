"""Exception round-tripping across three separate hierarchies.

benchctrl raises from four unrelated exception trees — ``BenchError``,
``QR10xError``, ``RigolDLError``, ``RigolDP2031Error`` — and callers catch
the specific ones. If a rejected ``set_voltage`` came back as a generic
``RuntimeError`` because it crossed a socket, every ``except
BenchCommandError`` in user code would silently stop working. So the class,
its MRO, and its attributes all travel.

Reconstruction is deliberately **constructor-signature-agnostic**.
``BenchCommandError.__init__`` takes ``(error_code, last_good_value,
command_code, message)``; the Rigol ones take something else; the next one
will differ again. Calling ``cls(msg)`` would raise ``TypeError`` inside the
error path, which is the worst possible place for a second failure. Instead
we allocate with ``__new__`` and restore ``__dict__``.

An unknown class degrades along its MRO: an agent raising something this
client has never heard of still arrives as the nearest known ancestor, so
``except BenchError`` keeps working across version skew.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from benchctrl.exceptions import (
    BenchCommandError,
    BenchConnectionError,
    BenchError,
    BenchNotImplementedError,
    BenchProtocolError,
    BenchTimeoutError,
    BenchValueError,
)

log = logging.getLogger("benchctrl.net.errors")


class RemoteBenchError(BenchError):
    """An agent-side exception with no local counterpart.

    Carries the original class name and traceback so the operator can still
    see what actually happened on the bench.
    """

    def __init__(self, message: str, *, remote_class: str = "", remote_traceback: str = ""):
        super().__init__(message)
        self.remote_class = remote_class
        self.remote_traceback = remote_traceback


class PolicyError(BenchValueError):
    """The agent refused an operation on policy grounds.

    Raised for a method that is not on the allowlist, a private name, a
    denied operation (``close``, ``calibrate``, ``firmware_upgrade``), or a
    mutation attempted without the writer claim. A subclass of
    ``BenchValueError`` because it is, fundamentally, the caller asking for
    something invalid.
    """


def _registry() -> dict[str, type]:
    """Class name -> class, for every exception that can cross the wire.

    Built lazily: the Rigol driver modules import cleanly without pyvisa
    (the import is inside ``open()``), but there is no reason to pay for
    them until an error actually needs mapping.
    """
    global _REGISTRY
    if _REGISTRY is not None:
        return _REGISTRY

    reg: dict[str, type] = {}
    for cls in (
        BenchError,
        BenchConnectionError,
        BenchValueError,
        BenchTimeoutError,
        BenchCommandError,
        BenchProtocolError,
        BenchNotImplementedError,
        RemoteBenchError,
        PolicyError,
        ValueError,
        TypeError,
        TimeoutError,
        OSError,
        RuntimeError,
        KeyError,
        AttributeError,
        NotImplementedError,
    ):
        reg[cls.__name__] = cls

    for module_path, names in (
        (
            "benchctrl.drivers.eastwood_qr10x.driver",
            ("QR10xError", "QR10xConnectionError", "QR10xProtocolError",
             "QR10xTimeoutError", "QR10xValueError"),
        ),
        (
            "benchctrl.drivers.rigol_dl3031a.driver",
            ("RigolDLError", "RigolDLConnectionError", "RigolDLCommandError",
             "RigolDLTimeoutError", "RigolDLValueError"),
        ),
        (
            "benchctrl.drivers.rigol_dp2031.driver",
            ("RigolDP2031Error", "RigolDP2031ConnectionError",
             "RigolDP2031CommandError", "RigolDP2031TimeoutError",
             "RigolDP2031ValueError"),
        ),
        (
            "benchctrl.drivers.siglent_sdm4065a.driver",
            # SDM4065AOverloadError is the odd one out: every other driver
            # has exactly Connection/Command/Timeout/Value. A DMM needs one
            # more, because "the input exceeded the range" is a distinct
            # recoverable condition (widen the range) rather than a bad
            # command or a dead link — and it must survive the wire, or a
            # remote caller sees a bare RuntimeError it cannot act on.
            ("SDM4065AError", "SDM4065AConnectionError", "SDM4065ACommandError",
             "SDM4065AOverloadError", "SDM4065ATimeoutError",
             "SDM4065AValueError"),
        ),
        (
            "benchctrl.drivers.cyberpower_pdu41002.driver",
            # Three extra beyond the usual Connection/Command/Timeout/Value,
            # and every one of them has to survive the wire to be actionable:
            #
            # - PolicyError: the outlet was outside allowed_outlets. Degrading
            #   it to RuntimeError would make a *refused mains switch* look
            #   like a device fault, and the fix (widen the allowlist) is a
            #   deliberate human decision, not a retry.
            # - SessionError: the CLI is single-session, so the remote caller
            #   must be able to tell "another session holds the device" from
            #   "your password is wrong" — the device produces both *after*
            #   accepting the password, so only the type distinguishes them.
            # - AuthError: likewise must not blur into ConnectionError; the
            #   remedy is BENCHCTRL_PDU_PASSWORD on the agent, not a reconnect.
            ("PDU41002Error", "PDU41002ConnectionError", "PDU41002CommandError",
             "PDU41002ProtocolError", "PDU41002TimeoutError",
             "PDU41002ValueError", "PDU41002AuthError", "PDU41002PolicyError",
             "PDU41002SessionError"),
        ),
        (
            "benchctrl.drivers.ontrak_adu218.driver",
            # No CommandError: this device has no error *reply* to carry. An
            # unknown command, a bad argument and a write-only command are all
            # answered with silence, byte-identical, so what would have been a
            # command error surfaces as ADU218TimeoutError instead — and the
            # ambiguity is documented on that class rather than hidden by a
            # type that implies the device said something.
            #
            # ADU218PolicyError must survive the wire for the same reason as
            # the PDU's: a relay refused by allowed_relays is a deliberate
            # configuration decision, and degrading it to RuntimeError would
            # make it read as a device fault a retry might clear.
            ("ADU218Error", "ADU218ConnectionError", "ADU218ProtocolError",
             "ADU218TimeoutError", "ADU218ValueError", "ADU218PolicyError"),
        ),
    ):
        try:
            module = __import__(module_path, fromlist=["*"])
        except ImportError as exc:  # pragma: no cover - driver always present
            log.debug("error registry: %s unavailable (%s)", module_path, exc)
            continue
        for name in names:
            cls = getattr(module, name, None)
            if isinstance(cls, type) and issubclass(cls, BaseException):
                reg[name] = cls

    _REGISTRY = reg
    return reg


_REGISTRY: Optional[dict[str, type]] = None


#: Attributes worth carrying. Restricted so a rogue agent cannot set
#: arbitrary attributes on a client-side object.
_SAFE_ATTR_TYPES = (str, int, float, bool, type(None))


def encode_exception(
    exc: BaseException,
    *,
    device: Optional[str] = None,
    method: Optional[str] = None,
    traceback_text: str = "",
) -> dict:
    """Serialise ``exc`` for an ERR frame."""
    mro = [c.__name__ for c in type(exc).__mro__ if c is not object]
    attrs = {
        k: v
        for k, v in vars(exc).items()
        if not k.startswith("_") and isinstance(v, _SAFE_ATTR_TYPES)
    }
    out = {
        "c": type(exc).__name__,
        "mro": mro,
        "msg": str(exc),
        "attrs": attrs,
    }
    if traceback_text:
        out["tb"] = traceback_text
    if device:
        out["device"] = device
    if method:
        out["method"] = method
    return out


def decode_exception(payload: dict) -> BaseException:
    """Rebuild the closest local exception for an ERR frame."""
    reg = _registry()
    name = payload.get("c", "")
    message = payload.get("msg", "")
    mro = payload.get("mro") or []

    cls = reg.get(name)
    degraded_from = None
    if cls is None:
        for ancestor in mro:
            if ancestor in reg:
                cls = reg[ancestor]
                degraded_from = name
                break
    if cls is None:
        cls = RemoteBenchError

    if cls is RemoteBenchError:
        exc: BaseException = RemoteBenchError(
            message,
            remote_class=name,
            remote_traceback=payload.get("tb", ""),
        )
    else:
        built = _instantiate(cls, message)
        if built is None:
            exc = RemoteBenchError(
                message, remote_class=name, remote_traceback=payload.get("tb", "")
            )
        else:
            exc = built
            for key, value in (payload.get("attrs") or {}).items():
                if isinstance(value, _SAFE_ATTR_TYPES) and not key.startswith("_"):
                    try:
                        setattr(exc, key, value)
                    except AttributeError:  # pragma: no cover - slotted classes
                        pass

    exc.remote_class = name  # type: ignore[attr-defined]
    exc.remote_traceback = payload.get("tb", "")  # type: ignore[attr-defined]
    exc.remote_device = payload.get("device")  # type: ignore[attr-defined]
    exc.remote_method = payload.get("method")  # type: ignore[attr-defined]
    if degraded_from:
        log.debug(
            "remote raised unknown %s; degraded to nearest known ancestor %s",
            degraded_from,
            cls.__name__,
        )
    return exc


def _instantiate(cls: type, message: str) -> Optional[BaseException]:
    """Build ``cls`` without knowing its constructor signature.

    Three strategies, in order, because the four hierarchies disagree:

    1. ``cls(message)`` — the common case.
    2. ``cls()`` — for classes that reject a positional message.
    3. ``cls.__new__(cls)`` — for classes like ``BenchCommandError`` whose
       ``__init__`` demands ``(error_code, last_good_value, command_code,
       message)``; calling it would raise a ``TypeError`` inside the error
       path, which is the worst place for a second failure.

    Strategy 3 is not universal: ``QR10xTimeoutError`` inherits both
    ``RuntimeError`` and ``TimeoutError`` (an ``OSError``), and CPython
    refuses ``RuntimeError.__new__`` for an ``OSError``-layout class. That is
    exactly why this is a cascade and not a single clever call.

    Returns None if every strategy fails, so the caller can degrade to
    ``RemoteBenchError`` rather than raise while reporting an error.
    """
    try:
        return cls(message)
    except Exception:  # noqa: BLE001
        pass
    try:
        exc = cls()
        BaseException.__init__(exc, message)
        return exc
    except Exception:  # noqa: BLE001
        pass
    try:
        exc = cls.__new__(cls)
        BaseException.__init__(exc, message)
        return exc
    except Exception as exc_info:  # noqa: BLE001
        log.debug("could not instantiate %s: %r", cls.__name__, exc_info)
        return None


def known_class_names() -> list[str]:
    """Every exception name that survives the round trip intact."""
    return sorted(_registry())
