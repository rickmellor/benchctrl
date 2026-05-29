"""Hardware-required: every SET command, every range check."""

from __future__ import annotations

import time

import pytest

from opensmu.exceptions import SMUValueError

pytestmark = pytest.mark.hardware


# --- client-side range checks (no wire traffic) ----------------------------


def test_set_voltage_negative_raises(smu):
    with pytest.raises(SMUValueError):
        smu.set_voltage(-0.1)


def test_set_voltage_above_max_raises(smu):
    with pytest.raises(SMUValueError):
        smu.set_voltage(10.0)


def test_set_current_limit_out_of_range_raises(smu):
    with pytest.raises(SMUValueError):
        smu.set_current_limit(0.0)
    with pytest.raises(SMUValueError):
        smu.set_current_limit(10.0)


def test_set_exp_voltage_out_of_range_raises(smu):
    with pytest.raises(SMUValueError):
        smu.set_exp_voltage(1.0)
    with pytest.raises(SMUValueError):
        smu.set_exp_voltage(6.0)


def test_set_adc_resistor_out_of_range_raises(smu):
    with pytest.raises(SMUValueError):
        smu.set_adc_resistor(0.0)
    with pytest.raises(SMUValueError):
        smu.set_adc_resistor(30.0)


def test_set_gpo_invalid_pin_raises(smu):
    with pytest.raises(SMUValueError):
        smu.set_gpo(0, True)
    with pytest.raises(SMUValueError):
        smu.set_gpo(3, True)


def test_set_range_invalid_raises(smu):
    with pytest.raises(SMUValueError):
        smu.set_range("medium")


def test_set_power_regulation_invalid_raises(smu):
    with pytest.raises(SMUValueError):
        smu.set_power_regulation("ridiculous")


# --- happy-path wire commands ----------------------------------------------


def test_set_voltage_3v25(smu):
    smu.set_voltage(3.25)
    assert smu.voltage == 3.25
    time.sleep(0.1)


def test_set_voltage_low_range_safe_values(smu):
    for v in (0.0, 1.0, 2.0, 3.0, 3.25):
        smu.set_voltage(v)
        time.sleep(0.05)


def test_set_current_limit_values(smu):
    for a in (0.001, 0.1, 1.0, 2.5):
        smu.set_current_limit(a)
        assert smu.current_limit == a
        time.sleep(0.05)


def test_set_exp_voltage_values(smu):
    for v in (1.2, 2.8, 3.3, 5.0):
        smu.set_exp_voltage(v)
        assert smu.exp_voltage == v
        time.sleep(0.05)


def test_set_output_toggle(smu):
    smu.set_output(True)
    assert smu.output_enabled is True
    time.sleep(0.05)
    smu.set_output(False)
    assert smu.output_enabled is False


def test_set_four_wire_toggle(smu):
    smu.set_four_wire(True)
    assert smu.four_wire_enabled is True
    time.sleep(0.05)
    smu.set_four_wire(False)
    assert smu.four_wire_enabled is False


def test_set_current_limit_enabled_toggle(smu):
    smu.set_current_limit_enabled(True)
    assert smu.current_limit_enabled is True
    smu.set_current_limit_enabled(False)
    assert smu.current_limit_enabled is False


def test_set_range_low_then_high(smu):
    smu.set_range("low")
    assert smu.range == "low"
    smu.set_range("high")
    assert smu.range == "high"
    # leave in low range for safety
    smu.set_range("low")


def test_set_exp_5v_toggle(smu):
    smu.set_exp_5v(True)
    assert smu.exp_5v_enabled is True
    smu.set_exp_5v(False)
    assert smu.exp_5v_enabled is False


def test_set_adc_resistor_values(smu):
    for r in (0.1, 1.0, 10.0):
        smu.set_adc_resistor(r)
        assert smu.adc_resistor == r
        time.sleep(0.05)


def test_set_uart_baudrate_and_enable(smu):
    smu.set_uart_baudrate(115200)
    assert smu.uart_baudrate == 115200
    smu.set_uart(True)
    assert smu.uart_enabled is True
    smu.set_uart(False)


def test_set_gpo_all_pin_state_combinations(smu):
    for pin in (1, 2):
        for state in (True, False):
            smu.set_gpo(pin, state)
            assert smu.gpo[pin] == state
            time.sleep(0.05)


def test_set_legacy_sink_toggle(smu):
    smu.set_legacy_sink(True)
    assert smu.legacy_sink_enabled is True
    smu.set_legacy_sink(False)


def test_set_main_current_value(smu):
    smu.set_main_current(0.05)
    assert smu.main_current == 0.05
