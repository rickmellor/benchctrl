"""The PDU41002 driver against the real device, over **both** transports.

Everything in :py:mod:`tests.test_bench_cyberpower_pdu41002` runs against the
simulator. That covers the parsers and every policy check, but three of this
driver's load-bearing facts are properties of the *device* and cannot be
simulated into existence — the simulator models them because the hardware was
measured, so asserting them there proves only that the model is
self-consistent:

1. **The CLI is single-session across all transports**, and the failure arrives
   *after* the password is accepted. A driver that misreads it as an auth
   failure sends the operator hunting for a wrong password.
2. **``close()`` must send ``exit``.** Closing the port does not release the
   device's session, so a driver that merely closes leaves the PDU unreachable
   from the other transport. This is the only method in the repo whose
   correctness depends on a side effect *on the device*.
3. **Command output is transport-independent.** The whole one-CLI-engine
   architecture rests on it.

So these tests are not a hardware smoke test of the read surface. They are the
evidence for the three claims the architecture was built on, plus a real
mains switch verified from the *other* wire.

Running it
----------

Both transports are opt-in by address, and each skips if unreachable:

    BENCHCTRL_PDU41002_PORT   serial device      default /dev/benchctrl/pdu41002
    BENCHCTRL_PDU41002_HOST   ssh host           default pdu-benchctrl
    BENCHCTRL_PDU_PASSWORD    the CLI password   (no default; all tests skip)
    BENCHCTRL_PDU41002_OUTLET the outlet to switch, default 8

The password comes from the environment because that is the only supported
route — never a config file, never an argv. Export it from a 0600 file; do not
type it where a shell history or process listing can see it.

**Mains warning.** Unlike every other hardware suite here, these tests switch
mains power, on one outlet, chosen by ``BENCHCTRL_PDU41002_OUTLET``. They
restore its prior state in a ``finally``, but a test that dies hard can leave
it cut. Point it at an outlet with nothing important on it.

The agent conflict is a skip, not a failure
-------------------------------------------

A benchctrl agent serving the PDU holds the device's one session continuously
(the mains sweep). Every test here would then fail *after* authenticating,
which looks exactly like a broken driver. The fixtures therefore translate
:py:class:`PDU41002SessionError` at open into a skip naming the cause, because
"something else is logged in" is a fact about the bench, not a defect. Stop the
agent first:

    sudo systemctl stop benchctrl-agent
"""

from __future__ import annotations

import os

import pytest

from benchctrl.drivers.cyberpower_pdu41002 import (
    CyberPowerPDU41002,
    PDU41002ConnectionError,
    PDU41002Error,
    PDU41002PolicyError,
    PDU41002SessionError,
)

pytestmark = pytest.mark.hardware


HW_PORT = os.environ.get("BENCHCTRL_PDU41002_PORT", "/dev/benchctrl/pdu41002")
HW_HOST = os.environ.get("BENCHCTRL_PDU41002_HOST", "pdu-benchctrl")

#: The one outlet these tests may switch. Deliberately a single int and
#: deliberately also the whole allowlist below, so a bug in this file cannot
#: reach an outlet the operator did not nominate.
TEST_OUTLET = int(os.environ.get("BENCHCTRL_PDU41002_OUTLET", "8"))

#: Mains is 120 V / 60 Hz here. Wide enough not to flake on a sagging supply,
#: narrow enough that a mis-scaled parse (12.35, 1235) still fails.
V_RANGE = (100.0, 135.0)
HZ_RANGE = (57.0, 63.0)


def _open(transport: str) -> CyberPowerPDU41002:
    """Open the real PDU over one transport, or skip with the reason.

    Skips rather than fails on *every* connection-shaped outcome: no cable, no
    route, and — importantly — the session being held. None of those are
    driver defects, and a red test for any of them trains people to ignore
    this file.
    """
    kwargs = {"port": HW_PORT} if transport == "serial" else {"host": HW_HOST}
    try:
        return CyberPowerPDU41002.open(
            allowed_outlets=(TEST_OUTLET,), **kwargs
        )
    except PDU41002SessionError as exc:
        pytest.skip(
            f"the PDU's single CLI session is held by something else "
            f"({exc}); stop the agent with "
            f"`sudo systemctl stop benchctrl-agent` and re-run"
        )
    except PDU41002Error as exc:
        pytest.skip(f"PDU not reachable over {transport} ({kwargs}): {exc}")


@pytest.fixture(params=["serial", "ssh"])
def pdu(request):
    """A live session, one test at a time, over each transport in turn.

    Function-scoped on purpose, even though an SSH login costs ~7.5 s on
    firmware 1.3.4. A session-scoped fixture would hold the device's only
    session for the whole run, which would make the single-session and
    ``close()``-releases-it tests untestable — the two things this file exists
    to prove.
    """
    if not os.environ.get("BENCHCTRL_PDU_PASSWORD"):
        pytest.skip("BENCHCTRL_PDU_PASSWORD is not set; see this module's docstring")
    dev = _open(request.param)
    try:
        yield dev
    finally:
        dev.close()


# ---------------------------------------------------------------------------
# The reads, over both transports
# ---------------------------------------------------------------------------


def test_identity_reports_this_model(pdu):
    info = pdu.read_identity()
    assert info.model == "PDU41002"
    # A MAC is the field most likely to be mis-sliced by a column-offset bug,
    # and unlike the name/location fields the operator cannot have blanked it.
    assert info.mac_address and info.mac_address.count("-") == 5
    assert info.firmware_version


def test_metering_is_physically_plausible(pdu):
    """Ranges, not exact values — this catches a mis-scaled or swapped parse.

    The metering fields are parsed out of one line, so a column error shows up
    as frequency landing in voltage rather than as a parse failure.
    """
    status = pdu.read_device_status()
    assert V_RANGE[0] < status.voltage_V < V_RANGE[1]
    assert HZ_RANGE[0] < status.frequency_Hz < HZ_RANGE[1]
    assert status.load_A >= 0.0
    assert status.load_W >= 0.0
    # The convenience readers must agree with the struct; they are separate
    # round trips, so allow for a real load changing between them.
    assert abs(pdu.measure_voltage_V() - status.voltage_V) < 5.0
    assert abs(pdu.measure_frequency_Hz() - status.frequency_Hz) < 1.0


def test_every_outlet_reports_a_state(pdu):
    states = pdu.outlet_states()
    assert set(states) == set(range(1, pdu.outlet_count + 1))
    assert all(isinstance(v, bool) for v in states.values())
    # The per-outlet read is a different command with a different layout, so
    # the two must be cross-checked rather than assumed equivalent.
    assert pdu.outlet_state(TEST_OUTLET) == states[TEST_OUTLET]


def test_outlet_config_covers_every_outlet(pdu):
    """The read-back budget is derived from these, so a miss is a flake source."""
    cfg = pdu.read_outlet_config()
    assert set(cfg) == set(range(1, pdu.outlet_count + 1))
    for entry in cfg.values():
        assert entry.on_delay_s >= 0
        assert entry.off_delay_s >= 0
        assert entry.reboot_duration_s > 0


def test_the_allowlist_refuses_a_real_outlet(pdu):
    """The policy check must fire before any bytes reach a live device.

    Also covered against the simulator, and worth repeating here: this is the
    one guard whose failure mode is switching mains on the wrong outlet, and
    the simulator cannot demonstrate that no *device* moved.
    """
    other = 1 if TEST_OUTLET != 1 else 2
    before = pdu.outlet_state(other)
    with pytest.raises(PDU41002PolicyError):
        pdu.set_outlet_state(other, not before)
    assert pdu.outlet_state(other) is before


# ---------------------------------------------------------------------------
# Real mains switching
# ---------------------------------------------------------------------------


def test_switching_round_trips_and_reads_back(pdu):
    """Cut the nominated outlet, verify, restore, verify.

    ``set_outlet_state`` returns the read-back state, and against hardware that
    is the only assertion worth making: ``oltctrl`` acknowledges nothing, so a
    driver that returned the *requested* state would pass every simulator test
    and still be unable to tell a moved contactor from a dead one.
    """
    original = pdu.outlet_state(TEST_OUTLET)
    try:
        assert pdu.set_outlet_state(TEST_OUTLET, not original) is (not original)
        assert pdu.outlet_state(TEST_OUTLET) is (not original)
        assert pdu.set_outlet_state(TEST_OUTLET, original) is original
    finally:
        if pdu.outlet_state(TEST_OUTLET) is not original:
            pdu.set_outlet_state(TEST_OUTLET, original)
    assert pdu.outlet_state(TEST_OUTLET) is original


def test_reset_outlet_ends_where_it_started(pdu):
    """``reset_outlet`` makes no claim about the transient, so assert the end.

    The device holds the outlet off for its configured reboot duration and
    restores it unprompted, so the observable contract is "energised again
    afterwards" — which is exactly why this method returns ``None`` rather than
    a read-back.

    **Do not poll for ``True`` straight after the command.** Measured on
    firmware 1.3.4: the contactor is still reading ``True`` for ~0.5 s after
    ``oltctrl … act reboot`` returns (3 of 6 trials), so an early read cannot be
    told apart from the restored state — and a poll loop that stops on the first
    ``True`` stops on the *pre-cut* one, then asserts mid-cut. That is what this
    test did, and it failed in a full run roughly half the time while passing
    every time in isolation.

    Waiting out the reboot duration first removes the ambiguity without needing
    to catch the transient: once the cut has definitely both started and
    finished, any ``True`` is the restored state. Note this hazard is unique to
    ``reset_outlet`` — it is the only call whose wanted end state equals its
    start state, so ``set_outlet_state``'s verify (which polls for the
    *opposite* of what it read) can never be satisfied by a stale value.
    """
    if not pdu.outlet_state(TEST_OUTLET):
        pdu.set_outlet_state(TEST_OUTLET, True)

    reboot_s = pdu.read_outlet_config()[TEST_OUTLET].reboot_duration_s

    import time

    t0 = time.monotonic()
    pdu.reset_outlet(TEST_OUTLET)

    # Past this instant the cut is over, so a True cannot be the pre-cut read.
    settled_at = t0 + reboot_s + 3.0
    while time.monotonic() < settled_at:
        time.sleep(0.5)

    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if pdu.outlet_state(TEST_OUTLET):
            break
        time.sleep(1.0)
    assert pdu.outlet_state(TEST_OUTLET), (
        f"outlet {TEST_OUTLET} did not come back within "
        f"{reboot_s}s + margin after reset_outlet"
    )


# ---------------------------------------------------------------------------
# The three device facts the architecture rests on
#
# These need two sessions' worth of control, so they open links directly rather
# than through the parametrised fixture.
# ---------------------------------------------------------------------------


def _require_both_transports():
    if not os.environ.get("BENCHCTRL_PDU_PASSWORD"):
        pytest.skip("BENCHCTRL_PDU_PASSWORD is not set")


def test_a_second_session_is_refused_as_a_session_error_not_an_auth_error():
    """The single-session limit, and the distinction that makes it diagnosable.

    The device accepts the password, prints its whole banner, and *then* hangs
    up. Anything that surfaces as ``PDU41002AuthError`` sends the operator
    checking a password that was in fact correct, so the assertion here is on
    the exception *type* as much as on the refusal.

    Note ``_open``'s session-error skip is deliberately bypassed: here the
    refusal is the result.
    """
    _require_both_transports()
    held = _open("serial")
    try:
        with pytest.raises(PDU41002SessionError):
            CyberPowerPDU41002.open(
                host=HW_HOST, allowed_outlets=(TEST_OUTLET,)
            )
    finally:
        held.close()


def test_close_releases_the_session_for_the_other_transport():
    """``close()`` sends ``exit``, and this is the only proof of it.

    Closing the port leaves the device's session held — the CLI keeps session
    state across port open/close — so without the ``exit`` the SSH open below
    fails *after* a successful password exchange. That is precisely the failure
    this test exists to make impossible to reintroduce, and precisely the one a
    simulator cannot show.
    """
    _require_both_transports()
    first = _open("serial")
    first.close()
    assert not first.is_open

    second = _open("ssh")
    try:
        assert second.read_identity().model == "PDU41002"
    finally:
        second.close()


def test_the_two_transports_agree_on_command_output():
    """One CLI engine over two pipes — asserted, not assumed.

    Sequential rather than concurrent, because the device permits one session;
    that is weaker evidence than a simultaneous diff would be, and it is the
    strongest available. Compares parsed output rather than raw bytes: the raw
    streams differ by design (serial echoes the command, SSH does not), and it
    is the *parsed* agreement that licenses one parser.

    Live metering is excluded — mains voltage genuinely differs between two
    reads seconds apart, and a test that treats that as a defect will be
    deleted rather than debugged.
    """
    _require_both_transports()
    readings = {}
    for transport in ("serial", "ssh"):
        dev = _open(transport)
        try:
            readings[transport] = {
                "identity": dev.read_identity().to_dict(),
                "config": {
                    i: c.to_dict() for i, c in dev.read_outlet_config().items()
                },
                "states": dev.outlet_states(),
                "count": dev.outlet_count,
            }
            assert dev.transport == transport
        finally:
            dev.close()

    assert readings["serial"] == readings["ssh"]


def test_a_switch_on_one_transport_is_visible_from_the_other():
    """Switch over SSH, read back over serial.

    The strongest evidence available that the network path really drives the
    same contactors, rather than being a second plausible-looking guess. It
    matters because everything else in this file exercises one transport at a
    time: two independently-wrong implementations would both pass.
    """
    _require_both_transports()
    over_ssh = _open("ssh")
    try:
        original = over_ssh.outlet_state(TEST_OUTLET)
        assert over_ssh.set_outlet_state(TEST_OUTLET, not original) is (not original)
    finally:
        over_ssh.close()

    over_serial = _open("serial")
    try:
        assert over_serial.outlet_state(TEST_OUTLET) is (not original), (
            "a switch commanded over SSH was not visible over serial; the two "
            "transports are not driving the same device state"
        )
        # Restore over the *serial* link, which also proves the reverse
        # direction rather than only SSH-writes/serial-reads.
        assert over_serial.set_outlet_state(TEST_OUTLET, original) is original
    finally:
        over_serial.close()

    confirm = _open("ssh")
    try:
        assert confirm.outlet_state(TEST_OUTLET) is original
    finally:
        confirm.close()


def test_the_password_does_not_appear_in_a_repr_of_a_live_session(pdu):
    """The credential design, checked against a session that really holds one.

    The simulator equivalent passes with a dummy password; this one holds the
    real secret, which is the case that matters — a driver that special-cased
    something about the sim path would pass there and leak here.

    What is *not* asserted: that the password is absent from the instance
    ``__dict__``. It is present, under the mangled ``_CyberPowerPDU41002__``
    name, and it has to be — the session re-authenticates after an idle
    logout, so the driver must keep it. The reachable claim is that nothing
    which gets *printed* or *returned* carries it, which is what the three
    assertions below cover: the object's own repr, the link's, and a reading.
    """
    secret = os.environ["BENCHCTRL_PDU_PASSWORD"]
    assert secret not in repr(pdu)
    assert secret not in repr(pdu._link)
    assert secret not in repr(pdu.read_identity().to_dict())


#: The device's own ``devcfg show`` reports ``Idle Time : 5 Minutes`` on
#: firmware 1.3.4 — not the 3 minutes the vendor manual's default implies. The
#: SSH session is dropped earlier than that, at ~180 s, which is measured rather
#: than documented anywhere.
IDLE_LOGOUT_S = 200.0


@pytest.mark.skipif(
    not os.environ.get("BENCHCTRL_PDU41002_SLOW"),
    reason="waits out the device's real idle timeout; set BENCHCTRL_PDU41002_SLOW=1",
)
def test_an_idle_logout_is_recovered_on_serial_and_fatal_over_ssh(pdu):
    """Idle out for real — and the two transports need opposite treatment.

    Off by default because it costs minutes of wall clock. Worth having anyway,
    for the hazard and for the asymmetry:

    - **Serial** keeps the port and drops to ``Login Name :``, so the session is
      recoverable in place — and *must* be recovered, because a timed-out CLI
      consumes ``oltctrl index N act off`` as a **username**: the switch is
      silently swallowed while the operator believes the outlet is off.
    - **SSH** does not survive it. The device closes the connection at ~180 s
      and the ssh client exits, so there is nothing left to re-authenticate on.
      The only correct answer is to reopen, and the error has to say so — this
      surfaced as a bare "no prompt within 12.0s" until the client's disconnect
      notice was recognised, which reads as a slow device rather than a dead
      link and invites a retry that can never work.

    The simulator covers the serial half deterministically via
    ``force_logout()``. Only the device establishes the timeout is real, that it
    is 5 minutes rather than the manual's 3, and that ssh behaves differently.
    """
    import time

    time.sleep(IDLE_LOGOUT_S)

    if pdu.transport == "serial":
        # A read is enough, and is the safe command for a test whose premise is
        # that the session state is unknown.
        assert pdu.read_identity().model == "PDU41002"
        return

    with pytest.raises(PDU41002ConnectionError) as exc:
        pdu.read_identity()
    # Naming the recovery is the whole value of the distinction.
    assert "reopen" in str(exc.value)
    # And reopening really is the recovery, not just advice.
    fresh = _open("ssh")
    try:
        assert fresh.read_identity().model == "PDU41002"
    finally:
        fresh.close()
