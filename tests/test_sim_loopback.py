"""End-to-end tests: real drivers against simulated devices over real ptys.

These are the tests that make the no-hardware development path credible.
Nothing is monkeypatched — ``OtiiArc.open()`` opens an actual serial device
path, pyserial does actual I/O, and the binary framing round-trips through
the kernel's tty layer.
"""

from __future__ import annotations

import time

import pytest

from benchctrl.drivers.otii_arc import OtiiArc
from benchctrl.drivers.otii_arc.channels import OtiiArcChannel
from benchctrl.drivers.eastwood_qr10x import QR10x
from benchctrl.exceptions import BenchCommandError
from benchctrl.interfaces import SourceMeasurementUnit
from benchctrl.sim import SimulatedOtiiArc, SimulatedQR10x, Square


# --------------------------------------------------------------------------
# Loopback plumbing
# --------------------------------------------------------------------------


def test_loopback_is_binary_safe():
    """A pty in cooked mode mangles CR/LF; ours must not."""
    from benchctrl.sim.loopback import SerialLoopback
    import serial

    with SerialLoopback() as link:
        ser = serial.Serial(link.port, timeout=0.5)
        try:
            # Every byte that a line discipline might rewrite.
            payload = bytes(range(256)) + b"\r\n\r\n\x00\x1a\x03\x04"
            link.write(payload)
            got = b""
            deadline = time.monotonic() + 2.0
            while len(got) < len(payload) and time.monotonic() < deadline:
                got += ser.read(len(payload) - len(got))
            assert got == payload
        finally:
            ser.close()


def test_loopback_client_to_device():
    from benchctrl.sim.loopback import SerialLoopback
    import serial

    with SerialLoopback() as link:
        ser = serial.Serial(link.port, timeout=0.5)
        try:
            ser.write(b"\xa3\x2c\xb5\x7f hello")
            ser.flush()
            got = b""
            deadline = time.monotonic() + 2.0
            while len(got) < 12 and time.monotonic() < deadline:
                got += link.read(64, timeout=0.1)
            assert got == b"\xa3\x2c\xb5\x7f hello"
        finally:
            ser.close()


# --------------------------------------------------------------------------
# Otii Arc
# --------------------------------------------------------------------------


@pytest.fixture()
def sim_arc():
    sim = SimulatedOtiiArc()
    sim.start()
    yield sim
    sim.close()


@pytest.fixture()
def arc(sim_arc):
    smu = OtiiArc.open(sim_arc.port)
    yield smu
    smu.close()


def test_open_runs_the_session_handshake(sim_arc, arc):
    """``_connect`` must complete the three-step wake sequence."""
    assert arc.is_connected
    assert sim_arc.handshake_seen
    kinds = [k for k, _, _ in sim_arc.command_log]
    assert "wake" in kinds
    assert "init_step3" in kinds


def test_remote_protocol_conformance(arc):
    assert isinstance(arc, SourceMeasurementUnit)


def test_set_voltage_reaches_the_device_in_wire_units(sim_arc, arc):
    arc.set_voltage(3.3)
    _wait_for(lambda: sim_arc.params[0x0B] == 3_300_000)
    assert sim_arc.voltage_v == pytest.approx(3.3)
    assert arc.voltage == pytest.approx(3.3)


def test_identity_queries_round_trip(arc):
    assert arc.get_device_name() == "Arc"
    assert arc.get_fw_version() == "3.1.3"
    assert arc.get_hw_version() == "1.3"
    assert arc.get_device_id() == "SIM0000000000000000000000000ARC1"


def test_channel_inventory_is_the_documented_length(arc):
    assert len(arc.get_channel_inventory()) == 268


def test_rejected_set_raises_bench_command_error(sim_arc, arc):
    """A device rejection must surface as a typed exception with attributes.

    Error frames are parsed by the reader thread, which only runs during a
    recording — so that is the only window in which an asynchronous device
    rejection can reach the caller. The exception lands on the *next*
    command, exactly as it would with real hardware.
    """
    with arc.record("mv"):
        sim_arc.inject_error()
        arc.set_voltage(3.3)
        _wait_for(lambda: sim_arc.rejected)
        time.sleep(0.2)  # let the reader thread consume the error frame

        with pytest.raises(BenchCommandError) as excinfo:
            for _ in range(20):
                arc.set_voltage(3.3)
                time.sleep(0.02)

    assert excinfo.value.error_code == -101


def test_device_side_range_check_rejects_what_the_driver_allows(sim_arc, arc):
    """5.4 V passes the driver's <=5.5 V guard but exceeds the device's 5.0 V.

    This is the interesting half of validation: the client-side check in
    ``set_voltage`` is a convenience, and the device remains the authority.
    """
    arc.set_voltage(5.4)
    _wait_for(lambda: sim_arc.rejected)
    assert sim_arc.rejected[0][0] == 0x0B
    assert sim_arc.params[0x0B] == 0  # setpoint unchanged by a rejected write


def test_client_side_guard_rejects_absurd_voltage_before_the_wire(sim_arc, arc):
    from benchctrl.exceptions import BenchValueError

    before = len(sim_arc.command_log)
    with pytest.raises(BenchValueError):
        arc.set_voltage(400.0)
    assert len(sim_arc.command_log) == before  # nothing was transmitted


def test_recording_captures_a_known_waveform(sim_arc):
    """A 50% duty square wave must come back with a mean at its midpoint.

    This exercises: channel-enable commands, the device's switch to packed
    framing, sub-4 multi-sample records, the reader thread, and the buffer
    demux — all over a real serial port.
    """
    sim_arc.waveforms["mc"] = Square(low=0.0, high=0.100, freq_hz=50.0, duty=0.5)
    sim_arc.packed_frame_hz = 400.0

    smu = OtiiArc.open(sim_arc.port)
    try:
        smu.set_voltage(3.3)
        smu.set_output(True)
        with smu.record("mc", "mv") as rec:
            time.sleep(1.0)

        mc = rec.statistics(OtiiArcChannel.MAIN_CURRENT)
        assert mc.sample_count > 500, f"expected sub-4 packing to yield many samples, got {mc.sample_count}"
        assert mc.average == pytest.approx(0.050, abs=0.010)
        assert mc.max == pytest.approx(0.100, abs=1e-6)
        assert mc.min == pytest.approx(0.0, abs=1e-6)

        mv = rec.statistics(OtiiArcChannel.MAIN_VOLTAGE)
        assert mv.max == pytest.approx(3.3, abs=1e-6)
        # Not 3.3 exactly: ``start_recording()`` does not flush the serial
        # input buffer, so baseline samples emitted before ``set_output(True)``
        # are still queued and land at the head of the recording. Real
        # hardware behaves the same way. See KNOWN_LIMITATIONS § A-3.
        assert mv.average == pytest.approx(3.3, rel=0.02)
    finally:
        smu.close()


def test_sub4_channel_yields_four_samples_per_frame(sim_arc):
    """``mc`` is subtype 4 and ``mv`` subtype 1 — a 4:1 sample ratio."""
    smu = OtiiArc.open(sim_arc.port)
    try:
        smu.set_voltage(1.0)
        smu.set_output(True)
        with smu.record("mc", "mv") as rec:
            time.sleep(0.6)
        n_mc = len(rec.data(OtiiArcChannel.MAIN_CURRENT))
        n_mv = len(rec.data(OtiiArcChannel.MAIN_VOLTAGE))
        assert n_mv > 10
        assert n_mc == pytest.approx(4 * n_mv, rel=0.15)
    finally:
        smu.close()


def test_recording_survives_garbage_on_the_wire(sim_arc):
    """``iter_frames`` must resync past noise without losing the stream."""
    smu = OtiiArc.open(sim_arc.port)
    try:
        smu.set_output(True)
        with smu.record("mv") as rec:
            time.sleep(0.2)
            sim_arc.emit_raw(b"\xde\xad\xbe\xef" * 32)  # unframed noise
            time.sleep(0.4)
        assert len(rec.data(OtiiArcChannel.MAIN_VOLTAGE)) > 10
    finally:
        smu.close()


def test_ohmic_load_closes_the_loop(sim_arc):
    """Commanded voltage across the default 330 R load produces current."""
    smu = OtiiArc.open(sim_arc.port)
    try:
        smu.set_voltage(3.3)
        smu.set_output(True)
        with smu.record("mc") as rec:
            time.sleep(0.5)
        mean = rec.statistics(OtiiArcChannel.MAIN_CURRENT).average
        assert mean == pytest.approx(3.3 / 330.0, rel=0.02)
    finally:
        smu.close()


def test_output_disabled_means_no_current(sim_arc):
    smu = OtiiArc.open(sim_arc.port)
    try:
        smu.set_voltage(3.3)
        smu.set_output(False)
        with smu.record("mc") as rec:
            time.sleep(0.4)
        assert rec.statistics(OtiiArcChannel.MAIN_CURRENT).max == pytest.approx(0.0)
    finally:
        smu.close()


def test_recording_round_trips_through_opensmu(sim_arc, tmp_path):
    smu = OtiiArc.open(sim_arc.port)
    try:
        smu.set_voltage(2.0)
        smu.set_output(True)
        with smu.record("mc", "mv") as rec:
            time.sleep(0.4)
        path = rec.save(tmp_path / "sim.opensmu")

        from benchctrl.recording import Recording

        loaded = Recording.load(path)
        assert loaded.channels == rec.channels
        for ch in rec.channels:
            assert loaded.data(ch) == rec.data(ch)
    finally:
        smu.close()


# --------------------------------------------------------------------------
# QR10x
# --------------------------------------------------------------------------


@pytest.fixture()
def sim_qr():
    sim = SimulatedQR10x()
    sim.start()
    yield sim
    sim.close()


def test_qr10x_identity(sim_qr):
    qr = QR10x.open(sim_qr.port)
    try:
        info = qr.info()
        assert info.device_type == "QR101A-1M-R1"
        assert info.serial == "SIM-QR10X-0001"
        assert info.hardware_version == "5.1N"
        assert info.firmware_version == "5.967KS"
        assert info.temperature_coefficient_ppm == 25
        # A YYYYMMDD date, not a model code: the field is production_date, and
        # real hardware answers e.g. 20221119.
        assert info.production_date.isdigit()
        assert len(info.production_date) == 8
    finally:
        qr.close()


def test_qr10x_setpoint_quantises_to_the_relay_ladder(sim_qr):
    sim_qr.step_ohm = 0.5
    qr = QR10x.open(sim_qr.port)
    try:
        qr.set_resistance(100.3)
        assert qr.get_setpoint() == pytest.approx(100.5)
        assert qr.actual_resistance() == pytest.approx(100.5)
    finally:
        qr.close()


def test_qr10x_incr_decr(sim_qr):
    qr = QR10x.open(sim_qr.port)
    try:
        qr.set_resistance(100.0)
        qr.incr(10.0)
        assert qr.get_setpoint() == pytest.approx(110.0)
        qr.decr(25.0)
        assert qr.get_setpoint() == pytest.approx(85.0)
    finally:
        qr.close()


def test_qr10x_safety_limit_refuses_larger_setpoints(sim_qr):
    qr = QR10x.open(sim_qr.port)
    try:
        qr.set_safety_limit(200.0)
        result = qr.set_resistance(500.0)
        assert result["ok"] is False
        assert qr.get_setpoint() == pytest.approx(0.1)  # unchanged
    finally:
        qr.close()


def test_qr10x_temperature(sim_qr):
    qr = QR10x.open(sim_qr.port)
    try:
        assert qr.get_temperature() == pytest.approx(26.5)
    finally:
        qr.close()


# --------------------------------------------------------------------------
# SCPI instruments — real driver + real pyvisa over a pty
# --------------------------------------------------------------------------


def test_scpi_header_normalisation():
    from benchctrl.sim.scpi import normalise

    assert normalise(":SOURce:CURRent:LEVel:IMMediate") == ":SOUR:CURR:LEV:IMM"
    assert normalise(":SOUR:CURR:LEV:IMM?") == ":SOUR:CURR:LEV:IMM"
    assert normalise(":FETCh:DISChargingTime?") == ":FETC:DISC"
    assert normalise("*IDN?") == "*IDN"


@pytest.fixture()
def dl3031a():
    from benchctrl.sim.factories import make_dl3031a

    drv = make_dl3031a()
    yield drv
    drv.close()


def test_dl3031a_identity_over_pyvisa(dl3031a):
    info = dl3031a.info()
    assert info.manufacturer == "RIGOL TECHNOLOGIES"
    assert info.model == "DL3031A"
    assert info.resource.startswith("ASRL")


def test_dl3031a_cc_mode_measurement(dl3031a):
    dl3031a.set_mode("CC")
    dl3031a.set_current(0.25)
    dl3031a.set_input(True)
    assert dl3031a.get_mode() == "CC"
    assert dl3031a.get_input() is True
    assert dl3031a.get_current() == pytest.approx(0.25)
    assert dl3031a.measure_current() == pytest.approx(0.25)
    assert dl3031a.measure_voltage() == pytest.approx(3.3)
    assert dl3031a.measure_power() == pytest.approx(3.3 * 0.25)


def test_dl3031a_input_off_sinks_nothing(dl3031a):
    dl3031a.set_mode("CC")
    dl3031a.set_current(0.5)
    dl3031a.set_input(False)
    assert dl3031a.measure_current() == pytest.approx(0.0)


def test_dl3031a_cr_mode_follows_ohms_law(dl3031a):
    dl3031a.set_mode("CR")
    dl3031a.set_resistance(33.0)
    dl3031a.set_input(True)
    assert dl3031a.measure_current() == pytest.approx(3.3 / 33.0, rel=1e-4)


def test_dl3031a_error_queue_round_trips(dl3031a):
    assert dl3031a.last_error() is None
    dl3031a._benchctrl_sim.inject_error(-222, "Data out of range")
    dl3031a.write(":SOURce:CURRent:LEVel:IMMediate 1.0")
    code, message = dl3031a.last_error()
    assert code == -222
    assert "out of range" in message


def test_dp2031_identity_and_channels():
    from benchctrl.sim.factories import make_dp2031

    psu = make_dp2031()
    try:
        assert psu.info().model == "DP2031"
        sim = psu._benchctrl_sim
        sim.voltage[1] = 5.0
        sim.output_on[1] = True
        volts, amps = sim.measured(1)
        assert volts == pytest.approx(5.0)
        assert amps == pytest.approx(5.0 / 100.0)
    finally:
        psu.close()


def test_dp2031_current_limit_folds_back():
    from benchctrl.sim.scpi import SimulatedRigolDP2031

    with SimulatedRigolDP2031(load_ohm=1.0) as sim:
        sim.voltage[1] = 5.0
        sim.current_limit[1] = 0.5
        sim.output_on[1] = True
        volts, amps = sim.measured(1)
        assert amps == pytest.approx(0.5)
        assert volts == pytest.approx(0.5)  # I*R, not the 5 V setpoint


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _wait_for(predicate, timeout: float = 2.0, interval: float = 0.01) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("condition not met within timeout")
