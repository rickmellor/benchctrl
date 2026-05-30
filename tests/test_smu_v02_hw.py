"""Hardware-required tests for the v0.2 newly-decoded wire commands."""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.hardware


# ----- GET-parameter interface ----------------------------------------------


def test_get_device_name_returns_arc(smu):
    assert smu.get_device_name() == "Arc"


def test_get_hw_version_returns_string(smu):
    v = smu.get_hw_version()
    assert isinstance(v, str)
    assert v  # non-empty


def test_get_fw_version_returns_dotted(smu):
    v = smu.get_fw_version()
    assert isinstance(v, str)
    assert v.count(".") >= 1


def test_get_device_id_is_32_hex(smu):
    d = smu.get_device_id()
    assert isinstance(d, str)
    assert len(d) == 32


def test_get_main_voltage_setpoint_readback(smu):
    smu.set_voltage(3.0)
    import time
    time.sleep(0.2)
    v = smu.get_main_voltage_setpoint()
    assert v is not None
    assert abs(v - 3.0) < 0.01
    # restore safe
    smu.set_voltage(3.25)


def test_get_max_current_setpoint_readback(smu):
    smu.set_current_limit(1.5)
    import time
    time.sleep(0.2)
    a = smu.get_max_current_setpoint()
    assert a is not None
    assert abs(a - 1.5) < 0.01


def test_get_exp_voltage_setpoint_readback(smu):
    smu.set_exp_voltage(2.8)
    import time
    time.sleep(0.2)
    v = smu.get_exp_voltage_setpoint()
    assert v is not None
    assert abs(v - 2.8) < 0.01


def test_get_uart_baudrate_setpoint_readback(smu):
    smu.set_uart_baudrate(9600)
    import time
    time.sleep(0.2)
    b = smu.get_uart_baudrate_setpoint()
    assert b == 9600


def test_channel_inventory_is_nonempty(smu):
    inv = smu.get_channel_inventory()
    assert isinstance(inv, bytes)
    # Otii ships at least ~13 channels at 16 bytes each
    assert len(inv) >= 16 * 13


# ----- set_power_regulation now actually fires a wire command ---------------


@pytest.mark.parametrize("mode", ["voltage", "current", "off"])
def test_set_power_regulation_wire(smu, mode):
    smu.set_power_regulation(mode)
    assert smu.power_regulation == mode


# ----- set_tx / set_gpo pin 3 ----------------------------------------------


def test_set_tx_uses_gpo_pin_3(smu):
    smu.set_tx(True)
    assert smu.gpo.get(3) is True
    smu.set_tx(False)
    assert smu.gpo.get(3) is False


def test_set_gpo_pin_3_allowed(smu):
    """Pin 3 (= TX pin) is valid per cap #43 decoding."""
    smu.set_gpo(3, True)
    smu.set_gpo(3, False)


# ----- write_tx -------------------------------------------------------------


def test_write_tx_sends_bytes(smu):
    """Just verify the call doesn't raise — there's no readback we can verify
    against without an external receiver wired to the TX pin."""
    smu.write_tx("hello benchctrl")
    smu.write_tx(b"\x01\x02\x03")


# ----- stop_recording now sends prepare-stop (0x7E) ------------------------


def test_recording_stop_includes_prepare_stop(smu):
    """The recording should still cleanly start + stop with the new
    0x7E prepare-stop frame inserted before the disable burst."""
    from benchctrl.drivers.otii_arc.channels import OtiiArcChannel as Channel
    import time

    smu.disable_all_channels()
    smu.enable_channels(Channel.MAIN_VOLTAGE, Channel.MAIN_CURRENT)
    with smu.record() as rec:
        time.sleep(1.0)
    assert rec.count(Channel.MAIN_VOLTAGE) > 500
    assert rec.count(Channel.MAIN_CURRENT) > 2000
