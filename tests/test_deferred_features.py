"""Every NotImplemented stub raises with a clear pointer to ROADMAP.md."""

from __future__ import annotations

import pytest

from opensmu.exceptions import SMUNotImplementedError


@pytest.fixture
def smu_skeleton():
    """Construct an SMU without opening a port — for testing pure-Python stubs."""
    from opensmu.device import SMU
    from opensmu.transport import Transport

    transport = Transport("__dummy__")  # never opened
    return SMU(transport)


@pytest.mark.parametrize(
    "method,args",
    [
        ("calibrate", ()),
        ("firmware_upgrade", ()),
        ("enable_battery_profiling", (True,)),
        ("set_battery_profile", ("uuid-1234",)),
        ("set_supply_battery_emulator", ("uuid-1234",)),
        ("wait_for_battery_data", (1.0,)),
        ("iter_uart_log", ()),
        ("write_tx", ("hello",)),
        ("set_tx", (True,)),
        ("get_rx", ()),
        ("set_channel_samplerate", ("mc", 4000)),
    ],
)
def test_deferred_smu_methods_raise(smu_skeleton, method, args):
    fn = getattr(smu_skeleton, method)
    with pytest.raises(SMUNotImplementedError) as exc:
        result = fn(*args)
        if hasattr(result, "__iter__") and method == "iter_uart_log":
            next(iter(result))  # force generator
    assert "ROADMAP.md" in str(exc.value)
