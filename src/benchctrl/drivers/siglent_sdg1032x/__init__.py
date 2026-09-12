"""Siglent SDG1032X — two-channel 30 MHz function / arbitrary waveform generator.

Device key ``siglent_sdg1032x``; USB-TMC (``f4ec:1103``) through pyvisa-py;
MCP tools ``sdg1032x_*``. The contract in one line: **every setter returns
what the instrument reads back, and raises** :py:class:`SDG1032XVerifyError`
**when that differs from what was asked** — the SDG has no error queue, so
read-back is the only way to know a value took. See the driver module
docstring for the protocol, the bench-measured firmware deviations, and the
safety rules (``allowed_channels``, ``max_amplitude_vpp``,
``disable_outputs``).

Implements no benchctrl Protocol: it is the first signal source in the tree
and a ``WaveformGenerator`` Protocol waits for the second concrete instance
(CONTRIBUTING.md convention 3). ``safety.default_safe_state`` reaches it
through ``disable_outputs()``.
"""

from benchctrl.drivers.siglent_sdg1032x.driver import (
    ARB_MAX_SAMPLES,
    CHANNELS,
    MOD_TYPES,
    VIRTUAL_KEYS,
    WAVE_TYPES,
    ArbData,
    ArbInfo,
    ArbSelection,
    BasicWave,
    Burst,
    ClockConfig,
    CounterReading,
    Coupling,
    Harmonic,
    LanConfig,
    Modulation,
    NumberFormat,
    OutputState,
    ProtectionState,
    SDG1032XConnectionError,
    SDG1032XError,
    SDG1032XInfo,
    SDG1032XPolicyError,
    SDG1032XProtocolError,
    SDG1032XTimeoutError,
    SDG1032XValueError,
    SDG1032XVerifyError,
    SiglentSDG1032X,
    Sweep,
    SyncConfig,
    discover,
)

__all__ = [
    "ARB_MAX_SAMPLES",
    "CHANNELS",
    "MOD_TYPES",
    "VIRTUAL_KEYS",
    "WAVE_TYPES",
    "ArbData",
    "ArbInfo",
    "ArbSelection",
    "BasicWave",
    "Burst",
    "ClockConfig",
    "CounterReading",
    "Coupling",
    "Harmonic",
    "LanConfig",
    "Modulation",
    "NumberFormat",
    "OutputState",
    "ProtectionState",
    "SDG1032XConnectionError",
    "SDG1032XError",
    "SDG1032XInfo",
    "SDG1032XPolicyError",
    "SDG1032XProtocolError",
    "SDG1032XTimeoutError",
    "SDG1032XValueError",
    "SDG1032XVerifyError",
    "SiglentSDG1032X",
    "Sweep",
    "SyncConfig",
    "discover",
]
