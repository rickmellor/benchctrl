"""SDG1032X driver tests, driven against the simulator over real pyvisa.

Sim-backed rather than mock-backed (CONTRIBUTING rule 2): every test below
runs the production driver, the production pyvisa stack, and real serial I/O
against :py:class:`SimulatedSDG1032X`. A mock would let the driver send any
command string at all and still pass.

The tests worth reading are the ones that pin down what "no error queue"
means for this driver:

* :py:func:`test_silent_clamp_surfaces_as_verify_error` — the instrument
  clamps ``AMP,0.001`` to 2 mVpp and says nothing; only the read-back knows.
* :py:func:`test_max_output_amp_is_accepted_but_inert` — the instrument's
  own amplitude cap is a no-op on this firmware, which is why the driver's
  cap is client-side.
* :py:func:`test_unknown_query_times_out_and_is_recorded` — a header the
  firmware does not implement produces no reply at all, and the next query
  still works.
"""

from __future__ import annotations

import logging
import struct
import time

import pytest

from benchctrl.drivers.siglent_sdg1032x import (
    ArbInfo,
    SDG1032XPolicyError,
    SDG1032XTimeoutError,
    SDG1032XValueError,
    SDG1032XVerifyError,
    SiglentSDG1032X,
)
from benchctrl.sim.factories import make_sdg1032x
from benchctrl.sim.sdg1032x import SimulatedSDG1032X


def _wait_for(predicate, timeout: float = 2.0) -> None:
    """Block until the simulator thread has acted on a write-only command."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("simulator did not act in time")
        time.sleep(0.005)


@pytest.fixture()
def gen():
    """Driver + sim with the driver's own defaults: both channels, no cap."""
    drv = make_sdg1032x()
    yield drv
    drv.close()


@pytest.fixture()
def big_screen_gen():
    """A sim whose ``SCDP`` returns a 200x200 BMP — a 120 KB blob."""
    drv = make_sdg1032x(sim={"screen_px": 200})
    yield drv
    drv.close()


def _sim(drv: SiglentSDG1032X) -> SimulatedSDG1032X:
    return drv._benchctrl_sim  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# Identity and lifecycle
# --------------------------------------------------------------------------


def test_open_makes_no_io(gen):
    assert _sim(gen).command_log == []
    assert gen.is_connected is True
    assert gen.resource.startswith("ASRL")


def test_identity_over_pyvisa(gen):
    info = gen.info()
    assert info.manufacturer == "Siglent Technologies"
    assert info.model == "SDG1032X"
    assert info.serial == "SIMSDG10320001"
    assert info.firmware == "1.01.01.33R1B6"
    assert gen.info() is info  # cached
    assert _sim(gen).command_log == ["*IDN?"]


def test_property_reads_make_no_io(gen):
    gen.info()
    before = list(_sim(gen).command_log)
    assert gen.channels == (1, 2)
    assert gen.allowed_channels == (1, 2)
    assert gen.max_amplitude_vpp is None
    assert gen.is_connected is True
    assert _sim(gen).command_log == before


def test_exit_disarms_both_outputs(gen):
    gen.set_output(1, True)
    gen.set_output(2, True)
    with gen:
        pass
    sim = _sim(gen)
    assert sim.channels[1].output is False
    assert sim.channels[2].output is False
    assert gen.is_connected is False


def test_reset_restores_power_on_defaults(gen):
    gen.set_output(1, True)
    gen.set_frequency(1, 5000)
    gen.reset()
    assert gen.get_output(1).enabled is False
    assert gen.get_basic_wave(1).frequency_hz == pytest.approx(1000.0)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def test_set_output_returns_typed_readback(gen):
    st = gen.set_output(1, True)
    assert st.channel == 1 and st.enabled is True and st.load_ohm is None
    assert gen.set_output(1, False).enabled is False
    assert _sim(gen).command_log[-2:] == ["C1:OUTP OFF", "C1:OUTP?"]


def test_set_output_load_hiz_and_50(gen):
    assert gen.set_output_load(1, 50).load_ohm == pytest.approx(50.0)
    assert gen.query("C1:OUTP?") == "C1:OUTP OFF,LOAD,50,PLRT,NOR"
    assert gen.set_output_load(1, None).load_ohm is None
    assert gen.query("C1:OUTP?") == "C1:OUTP OFF,LOAD,HZ,PLRT,NOR"


def test_set_output_polarity(gen):
    assert gen.set_output_polarity(1, True).inverted is True
    assert gen.query("C1:OUTP?") == "C1:OUTP OFF,LOAD,HZ,PLRT,INVT"
    assert gen.set_output_polarity(1, False).inverted is False


def test_disable_outputs_covers_both_channels_regardless_of_allowed():
    drv = make_sdg1032x(allowed_channels=(1,))
    try:
        _sim(drv).channels[2].output = True
        drv.set_output(1, True)
        states = drv.disable_outputs()
        assert set(states) == {1, 2}
        assert not states[1].enabled and not states[2].enabled
    finally:
        drv.close()


def test_front_panel_output_key_is_seen_by_readback(gen):
    """A ``VKEY`` press the driver did not make still shows in ``get_output``."""
    assert gen.get_output(1).enabled is False
    gen.write("VKEY VALUE,KB_OUTPUT1,STATE,1")
    assert gen.get_output(1).enabled is True
    assert _sim(gen).key_presses == ["KB_OUTPUT1"]


# --------------------------------------------------------------------------
# Basic wave
# --------------------------------------------------------------------------


def test_set_basic_wave_multi_field(gen):
    bw = gen.set_basic_wave(
        1, wave_type="SQUARE", frequency_hz=2500, amplitude_vpp=2.0, offset_v=0.5, duty_pct=25
    )
    assert bw.wave_type == "SQUARE"
    assert bw.frequency_hz == pytest.approx(2500.0)
    assert bw.period_s == pytest.approx(4e-4)
    assert bw.amplitude_vpp == pytest.approx(2.0)
    assert bw.amplitude_vrms == pytest.approx(1.0)
    assert bw.offset_v == pytest.approx(0.5)
    assert bw.duty_pct == pytest.approx(25.0)
    # The wave type travels alone (the bench unit swallows the message after a
    # combined WVTP command), the other fields follow in one command.
    assert _sim(gen).command_log[-3:] == [
        "C1:BSWV WVTP,SQUARE",
        "C1:BSWV FRQ,2500,AMP,2,OFST,0.5,DUTY,25",
        "C1:BSWV?",
    ]


def test_power_on_bswv_readback_is_byte_exact(gen):
    assert gen.query("C1:BSWV?") == (
        "C1:BSWV WVTP,SINE,FRQ,1000HZ,PERI,0.001S,AMP,4V,AMPVRMS,1.414Vrms,"
        "OFST,0V,HLEV,2V,LLEV,-2V,PHSE,0"
    )


def test_convenience_setters_return_readback(gen):
    assert gen.set_wave_type(1, "RAMP").wave_type == "RAMP"
    assert gen.set_frequency(1, 1234.5).frequency_hz == pytest.approx(1234.5)
    assert gen.set_amplitude(1, 1.5).amplitude_vpp == pytest.approx(1.5)
    assert gen.set_offset(1, 0.25).offset_v == pytest.approx(0.25)
    assert gen.set_phase(1, 90).phase_deg == pytest.approx(90.0)
    bw = gen.get_basic_wave(1)
    assert bw.symmetry_pct == pytest.approx(50.0)
    assert bw.amplitude_vrms == pytest.approx(1.5 / (2 * 3**0.5), rel=1e-3)


def test_amp_offset_and_high_low_level_are_coupled(gen):
    bw = gen.set_basic_wave(1, amplitude_vpp=2.0, offset_v=1.0)
    assert bw.high_level_v == pytest.approx(2.0)
    assert bw.low_level_v == pytest.approx(0.0)
    bw = gen.set_basic_wave(1, high_level_v=3.0)
    assert bw.amplitude_vpp == pytest.approx(3.0)
    assert bw.offset_v == pytest.approx(1.5)
    bw = gen.set_basic_wave(1, low_level_v=-1.0)
    assert bw.amplitude_vpp == pytest.approx(4.0)
    assert bw.offset_v == pytest.approx(1.0)


def test_frequency_and_period_are_coupled(gen):
    assert gen.set_frequency(1, 250).period_s == pytest.approx(0.004)
    assert gen.set_basic_wave(1, period_s=0.0001).frequency_hz == pytest.approx(10000.0)


def test_noise_and_dc_report_their_own_fields(gen):
    bw = gen.set_basic_wave(1, wave_type="NOISE", noise_stdev_v=0.3, noise_mean_v=0.1)
    assert bw.frequency_hz is None and bw.amplitude_vpp is None
    assert bw.noise_stdev_v == pytest.approx(0.3)
    assert gen.query("C1:BSWV?") == "C1:BSWV WVTP,NOISE,STDEV,0.3V,MEAN,0.1V"
    bw = gen.set_basic_wave(1, wave_type="DC", offset_v=1.5)
    assert gen.query("C1:BSWV?") == "C1:BSWV WVTP,DC,OFST,1.5V"
    assert bw.offset_v == pytest.approx(1.5)


def test_silent_clamp_surfaces_as_verify_error(gen):
    """Bench: ``AMP,0.001`` at HiZ reads back ``0.002V``. The driver's
    client-side check allows 1 mVpp (it is legal into 50 Ω), so the only
    signal is the read-back."""
    with pytest.raises(SDG1032XVerifyError) as ei:
        gen.set_amplitude(1, 0.001)
    err = ei.value
    assert err.channel == 1
    assert err.field == "amplitude_vpp"
    assert err.wanted == pytest.approx(0.001)
    assert err.got == pytest.approx(0.002)
    bw = gen.get_basic_wave(1)
    assert bw.high_level_v == pytest.approx(0.001)
    assert bw.low_level_v == pytest.approx(-0.001)
    assert _sim(gen).rejections == [
        "BSWV AMP,0.001: amplitude Vpp 0.001 outside 0.002..20, clamped to 0.002"
    ]


def test_square_duty_clamped_at_high_frequency_is_a_verify_error(gen):
    """Bench: ``WVTP,SQUARE,FRQ,20000000,DUTY,99`` reads back a duty in the
    fifties. The request is legal on its face; the instrument disagrees."""
    with pytest.raises(SDG1032XVerifyError) as ei:
        gen.set_basic_wave(1, wave_type="SQUARE", frequency_hz=20e6, duty_pct=99)
    assert ei.value.field == "duty_pct"
    assert ei.value.wanted == pytest.approx(99.0)
    assert 40.0 <= ei.value.got <= 60.0
    assert gen.get_basic_wave(1).frequency_hz == pytest.approx(20e6)


def test_verify_false_returns_readback_and_logs(gen, caplog):
    with caplog.at_level(logging.WARNING, logger="benchctrl.drivers.siglent_sdg1032x"):
        bw = gen.set_amplitude(1, 0.001, verify=False)
    assert bw.amplitude_vpp == pytest.approx(0.002)
    assert any("verify=False" in r.getMessage() for r in caplog.records)


def test_max_output_amp_is_accepted_but_inert(gen):
    """Bench: the instrument's own cap is accepted, never echoed, not enforced."""
    gen.write("C1:BSWV MAX_OUTPUT_AMP,2")
    bw = gen.set_amplitude(1, 3.0)
    assert bw.amplitude_vpp == pytest.approx(3.0)
    assert bw.max_amplitude_vpp is None
    assert _sim(gen).channels[1].max_output_amp == pytest.approx(2.0)
    assert "MAX_OUTPUT_AMP" not in gen.query("C1:BSWV?")


def test_set_max_amplitude_is_a_driver_side_cap(gen):
    before = len(_sim(gen).command_log)
    assert gen.set_max_amplitude(5.0) == pytest.approx(5.0)
    assert gen.max_amplitude_vpp == pytest.approx(5.0)
    assert len(_sim(gen).command_log) == before  # nothing sent
    with pytest.raises(SDG1032XPolicyError):
        gen.set_amplitude(1, 6.0)
    with pytest.raises(SDG1032XPolicyError):
        gen.set_max_amplitude(6.0)  # can only be lowered
    assert gen.set_amplitude(1, 4.5).amplitude_vpp == pytest.approx(4.5)


def test_max_amplitude_at_open_writes_nothing_and_gates_amplitude():
    drv = make_sdg1032x(max_amplitude_vpp=5.0)
    try:
        assert _sim(drv).command_log == []
        assert drv.max_amplitude_vpp == pytest.approx(5.0)
        with pytest.raises(SDG1032XPolicyError):
            drv.set_amplitude(1, 6.0)
        with pytest.raises(SDG1032XPolicyError):
            drv.write_arb("cap", [0, 1], amplitude_vpp=6.0)
    finally:
        drv.close()


# --------------------------------------------------------------------------
# Modulation / sweep / burst
# --------------------------------------------------------------------------


def test_enabling_modulation_turns_sweep_and_burst_off(gen):
    gen.set_sweep(1, enabled=True)
    assert gen.get_sweep(1).enabled is True
    gen.set_burst(1, enabled=True)
    assert gen.get_sweep(1).enabled is False
    mod = gen.set_modulation(1, enabled=True, type="AM", depth_pct=50)
    assert mod.enabled is True and mod.type == "AM"
    assert mod.depth_pct == pytest.approx(50.0)
    assert gen.get_sweep(1).enabled is False
    assert gen.get_burst(1).enabled is False


def test_set_modulation_fm_deviation_with_bench_readback_order(gen):
    mod = gen.set_modulation(1, enabled=True, type="FM", deviation=5000)
    assert mod.type == "FM"
    assert mod.deviation == pytest.approx(5000.0)
    assert mod.carrier is not None and mod.carrier.wave_type == "SINE"
    assert gen.query("C1:MDWV?") == (
        "C1:MDWV STATE,ON,FM,MDSP,SINE,SRC,INT,FRQ,100HZ,DEVI,5000HZ,"
        "CARR,WVTP,SINE,FRQ,1000HZ,AMP,4V,AMPVRMS,1.414Vrms,OFST,0V,PHSE,0"
    )
    assert gen.set_modulation(1, enabled=False).enabled is False
    assert gen.query("C1:MDWV?") == "C1:MDWV STATE,OFF"


def test_modulation_guide_form_is_parsed_the_same():
    drv = make_sdg1032x(sim={"mdwv_guide_form": True})
    try:
        mod = drv.set_modulation(1, enabled=True, type="FM", deviation=5000)
        assert drv.query("C1:MDWV?").startswith("C1:MDWV FM,STATE,ON,")
        assert mod.type == "FM" and mod.deviation == pytest.approx(5000.0)
    finally:
        drv.close()


def test_mode_parameters_are_ignored_while_off(gen):
    gen.write("C1:SWWV TIME,2")
    sw = gen.set_sweep(1, enabled=True)
    assert sw.time_s == pytest.approx(1.0)
    assert "SWWV TIME,2: sweep is off" in _sim(gen).rejections


def test_set_sweep_and_trigger(gen):
    sw = gen.set_sweep(1, enabled=True, start_hz=100, stop_hz=2000, trigger_source="MAN")
    assert sw.start_hz == pytest.approx(100.0)
    assert sw.stop_hz == pytest.approx(2000.0)
    assert sw.center_hz == pytest.approx(1050.0)
    assert sw.span_hz == pytest.approx(1900.0)
    assert sw.trigger_source == "MAN"
    assert sw.carrier is not None and sw.carrier.amplitude_vrms == pytest.approx(1.41421)
    gen.trigger_sweep(1)
    gen.operation_complete()
    assert _sim(gen).triggers == ["C1:SWWV"]


def test_set_burst_infinite_then_counted_and_trigger(gen):
    b = gen.set_burst(1, enabled=True, cycles="INF")
    assert b.enabled is True and b.cycles is None
    assert "TIME,INF" in gen.query("C1:BTWV?")
    b = gen.set_burst(1, cycles=5)
    assert b.cycles == 5
    assert b.carrier is not None and b.carrier.amplitude_vrms is None
    gen.trigger_burst(1)
    gen.operation_complete()
    assert _sim(gen).triggers == ["C1:BTWV"]


# --------------------------------------------------------------------------
# Sync / clock / phase / channel
# --------------------------------------------------------------------------


def test_set_sync_type_is_not_echoed(gen):
    s = gen.set_sync(1, True, type="CH2")
    assert s.enabled is True and s.type is None
    assert gen.query("C1:SYNC?") == "C1:SYNC ON"
    assert _sim(gen).channels[1].sync_type == "CH2"
    assert gen.set_sync(1, False).enabled is False


def test_set_clock(gen):
    c = gen.set_clock(source="EXT", output_10m=True)
    assert c.source == "EXT" and c.output_10m is None  # bench omits 10MOUT
    assert _sim(gen).rosc_10mout is True
    assert gen.query("ROSC?") == "ROSC EXT"


def test_set_clock_verifies_10mout_when_echoed():
    drv = make_sdg1032x(sim={"rosc_reports_10mout": True})
    try:
        assert drv.set_clock(output_10m=True).output_10m is True
        assert drv.query("ROSC?") == "ROSC INT,10MOUT,ON"
    finally:
        drv.close()


def test_phase_mode_hyphenated_readback_and_set_token(gen):
    assert gen.get_phase_mode() == "PHASELOCKED"
    assert gen.query("MODE?") == "MODE PHASE-LOCKED"
    assert gen.set_phase_mode("INDEPENDENT") == "INDEPENDENT"
    gen.write("MODE PHASELOCKED")  # the guide's token: silently ignored
    assert gen.get_phase_mode() == "INDEPENDENT"
    assert gen.set_phase_mode("PHASELOCKED") == "PHASELOCKED"
    assert _sim(gen).command_log[-2] == "MODE PHASE-LOCKED"


def test_apply_equal_phase_sends_eqphase(gen):
    gen.apply_equal_phase()
    gen.operation_complete()
    assert "EQPHASE" in _sim(gen).command_log


def test_set_invert(gen):
    assert gen.set_invert(1, True) is True
    assert gen.query("C1:INVT?") == "C1:INVT ON"
    assert gen.set_invert(1, False) is False


def test_apply_channel_copy(gen):
    gen.set_basic_wave(1, wave_type="RAMP", frequency_hz=321, amplitude_vpp=1.2, symmetry_pct=30)
    got = gen.apply_channel_copy(1, 2)
    assert got.channel == 2 and got.wave_type == "RAMP"
    assert got.frequency_hz == pytest.approx(321.0)
    assert got.symmetry_pct == pytest.approx(30.0)
    assert "PACP C2,C1" in _sim(gen).command_log


# --------------------------------------------------------------------------
# Coupling / harmonics / combine
# --------------------------------------------------------------------------


def test_set_coupling_freq_deviation(gen):
    c = gen.set_coupling(freq_coupled=True, freq_deviation_hz=5)
    assert c.freq_coupled is True and c.freq_deviation_hz == pytest.approx(5.0)
    assert gen.query("COUP?") == "COUP TRACE,OFF,FCOUP,ON,PCOUP,OFF,ACOUP,OFF,FDEV,5HZ"
    c = gen.set_coupling(freq_ratio=2)
    assert c.freq_ratio == pytest.approx(2.0) and c.freq_deviation_hz is None


def test_set_harmonics_parses_stray_comma_reply(gen):
    h = gen.set_harmonics(1, enabled=True, order=2, amplitude_dbc=-6)
    assert h.enabled is True and h.order == 2 and h.type == "EVEN"
    assert h.amplitude_dbc == pytest.approx(-6.0)
    assert h.amplitude_v == pytest.approx(2.004748935)
    assert gen.query("C1:HARM?") == (
        "C1:HARM ,HARMSTATE,ON,HARMTYPE,EVEN,HARMORDER,2,HARMAMP,2.004748935V,"
        "HARMDBC,-6dBc,HARMPHASE,0"
    )
    assert gen.set_harmonics(1, enabled=False).enabled is False
    assert gen.query("C1:HARM?") == "C1:HARM HARMSTATE,OFF"


def test_set_combine_refuses_square_silently(gen):
    assert gen.set_combine(1, True) is True
    with pytest.raises(SDG1032XVerifyError) as ei:
        gen.set_wave_type(1, "SQUARE")
    assert ei.value.field == "wave_type" and ei.value.got == "SINE"
    assert gen.set_combine(1, False) is False
    assert gen.set_wave_type(1, "SQUARE").wave_type == "SQUARE"


# --------------------------------------------------------------------------
# Counter / protection
# --------------------------------------------------------------------------


def test_read_counter_off_then_on_and_set_counter(gen):
    r = gen.read_counter()
    assert r.enabled is False and r.frequency_hz is None
    r = gen.set_counter(enabled=True, reference_hz=1e7, coupling="DC")
    assert r.enabled is True
    assert r.frequency_hz == pytest.approx(0.0)  # nothing connected
    assert r.reference_hz == pytest.approx(1e7)
    assert r.coupling == "DC"
    assert gen.query("FCNT?") == (
        "FCNT STATE,ON,FRQ,0HZ,DUTY,0,REFQ,1e+07HZ,TRG,0V,PW,0S,NW,0S,FRQDEV,0ppm,MODE,DC,HFR,OFF"
    )
    assert gen.set_counter(enabled=False).enabled is False


def test_protection_get_and_set(gen):
    assert gen.get_protection().over_voltage is True
    assert gen.query("VOLTPRT?") == "ON"  # bare, no header
    assert gen.set_protection(over_voltage=False).over_voltage is False


# --------------------------------------------------------------------------
# Screen
# --------------------------------------------------------------------------


def test_read_screen_returns_declared_bmp(gen):
    data = gen.read_screen()
    assert data[:2] == b"BM"
    assert len(data) == struct.unpack("<I", data[2:6])[0] == 70  # 2x2 24-bit
    assert gen.operation_complete()  # the trailing newline was drained


def test_read_screen_large_blob(big_screen_gen):
    data = big_screen_gen.read_screen()
    assert data[:2] == b"BM"
    assert len(data) == struct.unpack("<I", data[2:6])[0] == 120054
    assert b"\n" in data  # binary-safe through the whole transfer
    assert big_screen_gen.operation_complete()


# --------------------------------------------------------------------------
# System
# --------------------------------------------------------------------------


def test_buzzer(gen):
    assert gen.get_buzzer() is True
    assert gen.set_buzzer(False) is False


def test_screen_saver(gen):
    assert gen.get_screen_saver() == 0
    assert gen.set_screen_saver(5) == 5
    assert gen.query("SCSV?") == "SCSV 5MIN"
    assert gen.set_screen_saver(0) == 0
    assert gen.query("SCSV?") == "SCSV OFF"


def test_number_format(gen):
    nf = gen.get_number_format()
    assert nf.point == "DOT" and nf.separator == "SPACE"
    nf = gen.set_number_format(point="COMMA", separator="OFF")
    assert nf.point == "COMMA" and nf.separator == "OFF"


def test_language_and_power_on_config(gen):
    assert gen.get_language() == "EN"
    assert gen.set_language("CH") == "CH"
    assert gen.get_power_on_config() == "DEFAULT"
    assert gen.set_power_on_config("LAST") == "LAST"


def test_lan_config(gen):
    lan = gen.get_lan_config()
    assert lan.ip == "10.11.13.230" and lan.mask == "255.255.255.0"
    assert gen.query("SYST:COMM:LAN:IPAD?") == '"10.11.13.230"'
    lan = gen.set_lan_config(ip="192.168.1.5", gateway="192.168.1.1")
    assert lan.ip == "192.168.1.5" and lan.gateway == "192.168.1.1"


# --------------------------------------------------------------------------
# Arbitrary waveforms
# --------------------------------------------------------------------------


def test_list_builtin_arbs_sorted_by_index(gen):
    arbs = gen.list_arbs("builtin")
    assert len(arbs) == 197
    assert arbs[0] == ArbInfo(index=2, name="StairUp", builtin=True)
    assert arbs[2].name == "StairUD"
    assert [a.index for a in arbs] == sorted(a.index for a in arbs)
    assert gen.query("STL? BUILDIN").startswith("STL M10, ExpFal, M100, ECG14, ")


def test_list_user_arbs_empty_then_populated(gen):
    assert gen.list_arbs("user") == ()
    assert gen.query("STL? USER") == "STL WVNM"
    gen.write_arb("wave1", [0, 1, 2, 3])
    assert gen.list_arbs("user") == (ArbInfo(index=None, name="wave1", builtin=False),)


def test_select_builtin_by_index(gen):
    assert gen.get_arb(1).name == ""  # power-on: INDEX,0,NAME,
    sel = gen.select_arb(1, index=2)
    assert sel.index == 2 and sel.name == "StairUp"
    assert gen.query("C1:ARWV?") == "C1:ARWV INDEX,2,NAME,StairUp"


def test_select_user_by_name_after_upload(gen):
    with pytest.raises(SDG1032XVerifyError):
        gen.select_arb(1, name="nothere")  # not stored: silently refused
    gen.write_arb("wave1", [0, 100, -100, 0])
    assert gen.select_arb(1, name="wave1").name == "wave1"


def test_write_arb_int_codes_round_trip(gen):
    data = gen.write_arb("ints", [0, 1000, -1000, 32767, -32768], frequency_hz=2500)
    assert data.samples == (0, 1000, -1000, 32767, -32768)
    assert data.frequency_hz == pytest.approx(2500.0)
    assert gen.read_arb("ints").samples == data.samples


def test_write_arb_floats_round_trip(gen):
    data = gen.write_arb("floats", [0.0, 0.5, -0.5, 1.0, -1.0])
    assert data.samples == (0, 16384, -16384, 32767, -32767)


def test_write_arb_payload_with_newline_bytes_is_framed(gen):
    samples = [10, 0x0A0A, -0x0A0B, 10]  # int16 codes whose bytes contain 0x0A
    assert gen.write_arb("nl", samples).samples == tuple(samples)


def test_write_arb_padded_readback_is_tolerated():
    drv = make_sdg1032x(sim={"arb_pad_to": 8})
    try:
        data = drv.write_arb("short", [1, 2, 3])
        assert data.samples == (1, 2, 3, 3, 3, 3, 3, 3)
    finally:
        drv.close()


def test_write_arb_refuses_too_many_samples_and_bad_names(gen):
    with pytest.raises(SDG1032XValueError):
        gen.write_arb("big", [0] * 16385)
    with pytest.raises(SDG1032XValueError):
        gen.write_arb("bad name", [0, 1])
    with pytest.raises(SDG1032XValueError):
        gen.write_arb("x" * 25, [0, 1])
    assert _sim(gen).command_log == []  # refused before anything was sent


def test_upload_without_length_is_silently_rejected(gen):
    sim = _sim(gen)
    gen._inst.write_raw(b"C1:WVDT WVNM,nolen,WAVEDATA," + b"\x00\x01\x02\x03")
    _wait_for(lambda: sim.rejections)
    assert sim.rejections[0].endswith("no LENGTH field")
    assert sim.user_arbs == {}


# --------------------------------------------------------------------------
# Keys and policy
# --------------------------------------------------------------------------


def test_trigger_key_refuses_output_keys(gen):
    with pytest.raises(SDG1032XPolicyError):
        gen.trigger_key("KB_OUTPUT1")
    with pytest.raises(SDG1032XPolicyError):
        gen.trigger_key("kb_output2")
    with pytest.raises(SDG1032XValueError):
        gen.trigger_key("KB_NOPE")
    assert _sim(gen).command_log == []


def test_trigger_key_records_press(gen):
    gen.trigger_key("KB_UTILITY")
    gen.operation_complete()
    assert _sim(gen).key_presses == ["KB_UTILITY"]
    assert gen.get_output(1).enabled is False


def test_allowed_channels_gates_mutators_not_reads():
    drv = make_sdg1032x(allowed_channels=(1,))
    try:
        assert drv.allowed_channels == (1,)
        with pytest.raises(SDG1032XPolicyError):
            drv.set_output(2, True)
        assert drv.get_output(2).enabled is False
        assert drv.get_basic_wave(2).wave_type == "SINE"
        assert drv.set_output(1, True).enabled is True
    finally:
        drv.close()


def test_unknown_query_times_out_and_is_recorded():
    drv = make_sdg1032x(timeout_ms=300)
    try:
        with pytest.raises(SDG1032XTimeoutError):
            drv.query("CURRPRT?")
        assert _sim(drv).unanswered_queries == ["CURRPRT?"]
        assert drv.operation_complete() is True  # the next query works
    finally:
        drv.close()


def test_swwv_without_header_space_is_parsed_the_same():
    drv = make_sdg1032x(sim={"swwv_header_space": False})
    try:
        assert drv.query("C1:SWWV?") == "C1:SWWVSTATE,OFF"
        sw = drv.set_sweep(1, enabled=True, start_hz=200, stop_hz=800)
        assert sw.enabled is True
        assert sw.start_hz == pytest.approx(200.0)
        assert sw.center_hz == pytest.approx(500.0)
    finally:
        drv.close()


def test_a_sim_factory_exists_for_the_key():
    from benchctrl.sim.factories import factory_for

    assert factory_for("siglent_sdg1032x") is make_sdg1032x


def test_a_wave_type_change_with_fields_reads_back_despite_the_swallowed_message(gen):
    """Bench quirk: a BSWV carrying WVTP *and* other fields makes the
    instrument swallow the next message. The driver therefore sends the wave
    type on its own first; the combined read-back must arrive intact."""
    sim = gen._benchctrl_sim
    w = gen.set_basic_wave(
        1, wave_type="SQUARE", frequency_hz=2500.0, amplitude_vpp=1.5, duty_pct=30
    )
    assert (w.wave_type, w.frequency_hz, w.amplitude_vpp, w.duty_pct) == (
        "SQUARE",
        2500.0,
        1.5,
        30.0,
    )
    assert not sim.swallowed, "the driver combined WVTP with other fields"


def test_the_simulator_swallows_one_message_after_a_combined_wave_command():
    """The raw behaviour the driver works around, so a regression to a single
    combined command shows up here rather than on the bench."""
    from benchctrl.drivers.siglent_sdg1032x import SDG1032XTimeoutError
    from benchctrl.sim.factories import make_sdg1032x

    g = make_sdg1032x(timeout_ms=300)
    try:
        g.write("C1:BSWV WVTP,SINE,FRQ,1500")
        with pytest.raises(SDG1032XTimeoutError):
            g.query("C1:BSWV?")
        assert g.get_basic_wave(1).frequency_hz == 1500.0, "the one after answers"
    finally:
        g.close()
