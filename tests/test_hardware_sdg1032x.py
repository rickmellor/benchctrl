"""SDG1032X on real hardware: the read-back contract against the instrument.

Gated on ``BENCHCTRL_SDG1032X`` (``auto`` or a VISA resource string) with no
default, so the suite never picks up a generator it was not told about.
Outputs are only ever enabled at 1 Vpp into whatever is on CH1 — the bench
convention is an open BNC — and disarmed in ``finally``.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.hardware

RESOURCE = os.environ.get("BENCHCTRL_SDG1032X")


@pytest.fixture(scope="module")
def gen():
    if not RESOURCE:
        pytest.skip("BENCHCTRL_SDG1032X not set (auto or a VISA resource)")
    from benchctrl.drivers.siglent_sdg1032x import SDG1032XConnectionError, SiglentSDG1032X

    try:
        g = SiglentSDG1032X.open(None if RESOURCE == "auto" else RESOURCE, max_amplitude_vpp=5.0)
    except SDG1032XConnectionError as exc:
        pytest.skip(f"no SDG1032X: {exc}")
    try:
        yield g
    finally:
        try:
            g.disable_outputs()
        finally:
            g.close()


def test_identity_is_an_sdg1032x(gen):
    info = gen.info()
    assert info.manufacturer.startswith("Siglent") and info.model == "SDG1032X"


def test_every_supported_query_answers(gen):
    gen.get_output(1)
    gen.get_basic_wave(1)
    gen.get_modulation(1)
    gen.get_sweep(1)
    gen.get_burst(1)
    gen.get_arb(1)
    gen.get_sync(1)
    gen.get_invert(1)
    gen.get_combine(1)
    gen.get_harmonics(1)
    gen.get_clock()
    gen.get_phase_mode()
    gen.get_coupling()
    gen.read_counter()
    gen.get_screen_saver()
    gen.get_number_format()
    gen.get_language()
    gen.get_power_on_config()
    gen.get_buzzer()
    gen.get_protection()
    gen.get_lan_config()
    assert len(gen.list_arbs("builtin")) > 100


def test_setters_return_the_read_back_with_the_output_off(gen):
    gen.disable_outputs()
    w = gen.set_basic_wave(
        1, wave_type="SINE", frequency_hz=1234.5, amplitude_vpp=1.0, offset_v=0.25, phase_deg=90
    )
    assert (w.frequency_hz, w.amplitude_vpp, w.offset_v, w.phase_deg) == (1234.5, 1.0, 0.25, 90.0)
    assert w.high_level_v == pytest.approx(0.75) and w.low_level_v == pytest.approx(-0.25)
    sq = gen.set_basic_wave(1, wave_type="SQUARE", duty_pct=30)
    assert sq.duty_pct == 30.0
    gen.set_basic_wave(
        1, wave_type="SINE", frequency_hz=1000, amplitude_vpp=1.0, offset_v=0.0, phase_deg=0.0
    )


def test_a_silently_clamped_value_is_a_verify_error(gen):
    """The instrument has no error queue; a 1 mVpp request reads back the
    2 mVpp floor, and the driver refuses to pretend otherwise."""
    from benchctrl.drivers.siglent_sdg1032x import SDG1032XVerifyError

    with pytest.raises(SDG1032XVerifyError) as excinfo:
        gen.set_amplitude(1, 0.001)
    assert excinfo.value.field == "amplitude_vpp"
    assert excinfo.value.got == pytest.approx(0.002, abs=1e-6)
    assert gen.set_amplitude(1, 0.001, verify=False).amplitude_vpp == pytest.approx(0.002)
    gen.set_amplitude(1, 1.0)


def test_phase_mode_round_trips_through_the_hyphenated_token(gen):
    assert gen.set_phase_mode("INDEPENDENT") == "INDEPENDENT"
    assert gen.set_phase_mode("PHASELOCKED") == "PHASELOCKED"


def test_screen_dump_is_a_full_bitmap(gen):
    bmp = gen.read_screen()
    assert bmp[:2] == b"BM" and len(bmp) == 391734
    # and the next query is not polluted by the trailing newline the firmware sends
    assert gen.info().model == "SDG1032X"


def test_an_unimplemented_query_goes_unanswered_without_wedging(gen):
    from benchctrl.drivers.siglent_sdg1032x import SDG1032XTimeoutError

    with pytest.raises(SDG1032XTimeoutError):
        gen.query("CURRPRT?")
    assert gen.get_output(1).channel == 1


def test_output_arms_at_one_volt_and_disarms(gen):
    gen.set_basic_wave(1, wave_type="SINE", frequency_hz=1000, amplitude_vpp=1.0, offset_v=0.0)
    assert gen.set_output(1, True).enabled is True
    states = gen.disable_outputs()
    assert not states[1].enabled and not states[2].enabled


def test_arb_round_trips_over_the_lan(gen):
    """The upload path USB refuses: a full-length float waveform lands over the
    instrument's socket and reads back byte-exact after newline escaping."""
    import math

    from benchctrl.drivers.siglent_sdg1032x import SDG1032XConnectionError

    n = 16384
    sine = [math.sin(2 * math.pi * i / n) for i in range(n)]
    try:
        arb = gen.write_arb("bench_sine16k", sine, frequency_hz=1000, amplitude_vpp=1.0)
    except SDG1032XConnectionError as exc:
        pytest.skip(f"generator not on the LAN: {exc}")
    assert len(arb.codes) == 2 * n
    assert 0 < arb.nudged < n // 50, "escaping touched an implausible number of samples"
    assert gen.read_arb("bench_sine16k").codes == arb.codes
    assert gen.select_arb(1, name="bench_sine16k").name == "bench_sine16k"
    assert "bench_sine16k" in [a.name for a in gen.list_arbs("user")]
