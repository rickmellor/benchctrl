"""Pure tests for the SDG1032X driver's parsing — no instrument, no sim.

The canned strings are real bench captures from an SDG1032X on firmware
1.01.01.33R1B6 (plus the programming guide's ON forms for the headers the
bench unit had OFF at capture time). A ``FakeInst`` stands in for the pyvisa
resource so the driver's own query methods can be driven end to end through
``split_header`` / ``parse_pairs`` / ``parse_number`` without I/O.
"""

from __future__ import annotations

import logging
import struct

import pytest

from benchctrl.drivers.siglent_sdg1032x.driver import (
    ArbInfo,
    BasicWave,
    SDG1032XProtocolError,
    SDG1032XTimeoutError,
    SDG1032XValueError,
    SDG1032XVerifyError,
    SiglentSDG1032X,
    _close,
    _codes,
    basic_wave_from_pairs,
    parse_number,
    parse_pairs,
    split_header,
)

IDN = "Siglent Technologies,SDG1032X,SDG1XCBX5R1972,1.01.01.33R1B6"
BSWV = (
    "C1:BSWV WVTP,SINE,FRQ,1000HZ,PERI,0.001S,AMP,4V,AMPVRMS,1.414Vrms,OFST,0V,"
    "HLEV,2V,LLEV,-2V,PHSE,0"
)
MDWV_ON = (
    "C1:MDWV AM,STATE,ON,MDSP,SINE,SRC,INT,FRQ,100HZ,DEPTH,100,CARR,WVTP,RAMP,"
    "FRQ,1000HZ,AMP,4V,AMPVRMS,1.15473Vrms,OFST,0V,PHSE,0,SYM,50"
)
SWWV_ON = (
    "C2:SWWVSTATE,ON,TIME,1S,STOP,1500HZ,START,500HZ,CENTER,1000HZ,SPAN,1000HZ,"
    "TRSR,INT,TRMD,OFF,SWMD,LINE,DIR,UP,SYM,0,MARK_STATE,OFF,MARK_FREQ,1000HZ,"
    "CARR,WVTP,SINE,FRQ,1000HZ,AMP,4V,AMPVRMS,1.41421Vrms,OFST,0V,PHSE,0"
)
BTWV_ON = (
    "C2:BTWV STATE,ON,PRD,0.01S,STPS,0,TRSR,INT,TRMD,OFF,TIME,1,DLAY,2.4e-07S,"
    "GATE_NCYC,NCYC,CARR,WVTP,SINE,FRQ,1000HZ,AMP,4V,OFST,0V,PHSE,0"
)
HARM_ON = (
    "C1:HARM ,HARMSTATE,ON,HARMTYPE,EVEN,HARMORDER,2,HARMAMP,2.004748935V,HARMDBC,-6dBc,HARMPHASE,0"
)
MDWV_BENCH_AM = (
    "C1:MDWV STATE,ON,AM,MDSP,SINE,SRC,INT,FRQ,200HZ,DEPTH,50,CARR,WVTP,SINE,"
    "FRQ,1000HZ,AMP,1V,AMPVRMS,0.3535Vrms,OFST,0V,PHSE,0"
)
MDWV_BENCH_FM = (
    "C1:MDWV STATE,ON,FM,MDSP,SINE,SRC,INT,FRQ,100HZ,DEVI,100HZ,CARR,WVTP,SINE,"
    "FRQ,1000HZ,AMP,1V,AMPVRMS,0.3535Vrms,OFST,0V,PHSE,0"
)
FCNT_ON = (
    "FCNT STATE,ON,FRQ,10000000HZ,DUTY,59.8568,REFQ,1e+07HZ,TRG,0V,PW,5.98568e-08S,"
    "NW,4.01432e-08S,FRQDEV,0ppm,MODE,AC,HFR,OFF"
)


class FakeInst:
    """The slice of a pyvisa resource the driver touches. ``responses`` maps a
    query string to its canned answer; anything else times out the way
    pyvisa-py does for a header this firmware does not implement."""

    def __init__(self, responses=None, raw: bytes = b""):
        self.responses = dict(responses or {})
        self.writes: list[str] = []
        self.buffer = bytearray(raw)
        self.timeout = 5000

    def write(self, command: str) -> None:
        self.writes.append(command)

    def query(self, command: str) -> str:
        self.writes.append(command)
        try:
            return self.responses[command] + "\n"
        except KeyError:
            raise Exception("VI_ERROR_TMO: Timeout expired before operation completed.") from None

    def read_raw(self) -> bytes:
        return self.read_bytes(20480)

    def read_bytes(self, n: int) -> bytes:
        if not self.buffer:
            raise Exception("VI_ERROR_TMO: Timeout expired before operation completed.")
        out = bytes(self.buffer[:n])
        del self.buffer[:n]
        return out


def make(responses=None, raw: bytes = b"", **kw) -> tuple[SiglentSDG1032X, FakeInst]:
    inst = FakeInst(responses, raw)
    return SiglentSDG1032X(inst, resource_string="SIM", **kw), inst


# ---------------------------------------------------------------- pure helpers


def test_split_header_strips_channel_and_header():
    assert split_header("C1:BSWV WVTP,SINE,FRQ,1000HZ\n", "BSWV") == (1, "WVTP,SINE,FRQ,1000HZ")


def test_split_header_tolerates_no_space_after_header():
    ch, body = split_header(SWWV_ON, "SWWV")
    assert ch == 2
    assert body.startswith("STATE,ON,TIME,1S")


def test_split_header_headerless_answer_is_the_whole_body():
    assert split_header("ON", "VOLTPRT") == (None, "ON")
    assert split_header('"10.11.13.230"', "IPAD") == (None, '"10.11.13.230"')


def test_split_header_without_channel():
    assert split_header("ROSC INT,10MOUT,OFF", "ROSC") == (None, "INT,10MOUT,OFF")


def test_parse_pairs_main_and_carrier():
    main, carr = parse_pairs("WVTP,SINE,FRQ,1000HZ,CARR,WVTP,RAMP,SYM,50")
    assert main == {"WVTP": "SINE", "FRQ": "1000HZ"}
    assert carr == {"WVTP": "RAMP", "SYM": "50"}


def test_parse_pairs_drops_leading_empty_token_and_strips_spaces():
    main, carr = parse_pairs(",HARMSTATE,ON,HARMTYPE,EVEN")
    assert main == {"HARMSTATE": "ON", "HARMTYPE": "EVEN"} and carr is None
    main, _ = parse_pairs("PNT, DOT, SEPT, ON")
    assert main == {"PNT": "DOT", "SEPT": "ON"}


def test_parse_pairs_keeps_odd_trailing_key():
    main, _ = parse_pairs("INDEX,0,NAME,")
    assert main == {"INDEX": "0", "NAME": ""}
    main, _ = parse_pairs("A,1,B")
    assert main == {"A": "1", "B": ""}


@pytest.mark.parametrize(
    "token, expected",
    [
        ("1000HZ", (1000.0, "Hz")),
        ("2.4e-07S", (2.4e-7, "s")),
        ("0", (0.0, "")),
        ("1.414Vrms", (1.414, "Vrms")),
        ("-6dBc", (-6.0, "dBc")),
        ("1e+07HZ", (1e7, "Hz")),
        ("0ppm", (0.0, "ppm")),
        ("5MIN", (5.0, "min")),
        ("59.8568", (59.8568, "")),
        (" 4V ", (4.0, "V")),
    ],
)
def test_parse_number(token, expected):
    assert parse_number(token) == expected


@pytest.mark.parametrize("token", ["HZ", "INF", "OFF", "", "SINE", "1.2.3", "4X"])
def test_parse_number_rejects_non_numbers(token):
    with pytest.raises(SDG1032XProtocolError):
        parse_number(token)


def test_basic_wave_from_pairs_typed_and_none_for_missing():
    pairs, _ = parse_pairs("WVTP,NOISE,STDEV,0.5V,MEAN,0V")
    bw = basic_wave_from_pairs(pairs, 2)
    assert bw == BasicWave(channel=2, wave_type="NOISE", noise_stdev_v=0.5, noise_mean_v=0.0)
    assert bw.frequency_hz is None and bw.amplitude_vpp is None


def test_close_tolerance():
    assert _close(1000.0, 1000.000001, "Hz")
    assert not _close(1000.0, 1001.0, "Hz")
    assert _close(2.4e-7, 2.4e-07, "s")
    assert _close(4.0, 4.0005, "V")  # under the 1 mV floor
    assert not _close(4.0, 4.002, "V")
    assert _close("sine", "SINE", "")
    assert _close(True, True, "") and not _close(True, False, "")
    assert not _close(1.0, None, "V")


def test_codes_int_float_mixed():
    assert _codes([0, 1, -1, 32767, -32768]) == struct.pack("<5h", 0, 1, -1, 32767, -32768)
    assert _codes([0.0, 1.0, -1.0]) == struct.pack("<3h", 0, 32767, -32767)
    assert _codes([0.5, -0.5]) == struct.pack("<2h", 16384, -16384)
    # A mix of ints and floats is all-Real, so it is taken as floats in [-1, 1]
    assert _codes([1, 0.5]) == struct.pack("<2h", 32767, 16384)
    with pytest.raises(SDG1032XValueError):
        _codes([2, 0.5])  # mixed and out of the float range
    with pytest.raises(SDG1032XValueError):
        _codes([40000])  # out of int16
    with pytest.raises(SDG1032XValueError):
        _codes([1.5])  # out of [-1, 1]
    with pytest.raises(SDG1032XValueError):
        _codes([])
    with pytest.raises(SDG1032XValueError):
        _codes([True, False])  # bools are not codes


# ---------------------------------------------------------------- identity / transport


def test_info_parses_idn_and_caches():
    gen, inst = make({"*IDN?": IDN})
    info = gen.info()
    assert (info.manufacturer, info.model, info.serial, info.firmware) == (
        "Siglent Technologies",
        "SDG1032X",
        "SDG1XCBX5R1972",
        "1.01.01.33R1B6",
    )
    assert info.resource == "SIM"
    gen.info()
    assert inst.writes.count("*IDN?") == 1
    assert info.to_dict()["model"] == "SDG1032X"


def test_query_unimplemented_header_is_a_timeout_error():
    gen, _ = make({})
    with pytest.raises(SDG1032XTimeoutError, match="unanswered"):
        gen.query("CURRPRT?")


def test_operation_complete():
    gen, _ = make({"*OPC?": "1"})
    assert gen.operation_complete() is True


# ---------------------------------------------------------------- per-query parsing


def test_get_output_off_hiz_normal():
    gen, _ = make({"C1:OUTP?": "C1:OUTP OFF,LOAD,HZ,PLRT,NOR"})
    st = gen.get_output(1)
    assert st.to_dict() == {"channel": 1, "enabled": False, "load_ohm": None, "inverted": False}


def test_get_output_on_50_inverted():
    gen, _ = make({"C2:OUTP?": "C2:OUTP ON,LOAD,50,PLRT,INVT"})
    st = gen.get_output("C2")
    assert (st.enabled, st.load_ohm, st.inverted) == (True, 50.0, True)


def test_get_basic_wave_bench_capture():
    gen, _ = make({"C1:BSWV?": BSWV})
    bw = gen.get_basic_wave(1)
    assert bw.channel == 1
    assert bw.wave_type == "SINE"
    assert bw.frequency_hz == 1000.0
    assert bw.period_s == 0.001
    assert bw.amplitude_vpp == 4.0
    assert bw.amplitude_vrms == 1.414
    assert bw.offset_v == 0.0
    assert bw.high_level_v == 2.0
    assert bw.low_level_v == -2.0
    assert bw.phase_deg == 0.0
    assert bw.duty_pct is None and bw.max_amplitude_vpp is None


def test_get_modulation_off_and_on():
    gen, _ = make({"C1:MDWV?": "C1:MDWV STATE,OFF"})
    m = gen.get_modulation(1)
    assert m.enabled is False and m.type is None and m.carrier is None

    gen, _ = make({"C1:MDWV?": MDWV_ON})
    m = gen.get_modulation(1)
    assert m.enabled is True
    assert m.type == "AM"
    assert m.shape == "SINE"
    assert m.source == "INT"
    assert m.frequency_hz == 100.0
    assert m.depth_pct == 100.0
    assert m.carrier is not None
    assert m.carrier.channel is None
    assert m.carrier.wave_type == "RAMP"
    assert m.carrier.frequency_hz == 1000.0
    assert m.carrier.amplitude_vrms == 1.15473
    assert m.carrier.symmetry_pct == 50.0
    assert m.to_dict()["carrier"]["symmetry_pct"] == 50.0


def test_get_modulation_bench_order_state_first():
    """The bench unit answers ``STATE,ON,AM,…`` — the type is a bare token
    after the state pair, not before it as the guide prints."""
    gen, _ = make({"C1:MDWV?": MDWV_BENCH_AM})
    m = gen.get_modulation(1)
    assert m.enabled is True and m.type == "AM"
    assert m.depth_pct == 50.0
    assert m.frequency_hz == 200.0
    assert m.shape == "SINE" and m.source == "INT"
    assert m.carrier is not None and m.carrier.amplitude_vpp == 1.0
    assert m.carrier.amplitude_vrms == 0.3535

    gen, _ = make({"C1:MDWV?": MDWV_BENCH_FM})
    m = gen.get_modulation(1)
    assert m.type == "FM"
    assert m.deviation == 100.0
    assert m.frequency_hz == 100.0
    assert m.depth_pct is None


def test_get_sweep_no_space_form_and_off():
    gen, _ = make({"C2:SWWV?": SWWV_ON})
    s = gen.get_sweep(2)
    assert s.channel == 2 and s.enabled is True
    assert s.time_s == 1.0
    assert (s.start_hz, s.stop_hz, s.center_hz, s.span_hz) == (500.0, 1500.0, 1000.0, 1000.0)
    assert s.trigger_source == "INT"
    assert s.trigger_out is False
    assert s.mode == "LINE" and s.direction == "UP"
    assert s.symmetry_pct == 0.0
    assert s.mark_enabled is False and s.mark_hz == 1000.0
    assert s.carrier is not None and s.carrier.amplitude_vrms == 1.41421

    gen, _ = make({"C1:SWWV?": "C1:SWWV STATE,OFF"})
    s = gen.get_sweep(1)
    assert s.enabled is False and s.carrier is None and s.start_hz is None


def test_get_burst_cycles_and_delay():
    gen, _ = make({"C2:BTWV?": BTWV_ON})
    b = gen.get_burst(2)
    assert b.enabled is True
    assert b.period_s == 0.01
    assert b.start_phase_deg == 0.0
    assert b.trigger_source == "INT" and b.trigger_out == "OFF"
    assert b.cycles == 1
    assert b.delay_s == 2.4e-7
    assert b.mode == "NCYC"
    assert b.carrier is not None and b.carrier.wave_type == "SINE"


def test_get_burst_inf_cycles_is_none():
    gen, _ = make({"C2:BTWV?": BTWV_ON.replace("TIME,1,", "TIME,INF,")})
    assert gen.get_burst(2).cycles is None


def test_get_harmonics_stray_comma_and_off():
    gen, _ = make({"C1:HARM?": HARM_ON})
    h = gen.get_harmonics(1)
    assert h.enabled is True
    assert h.type == "EVEN"
    assert h.order == 2
    assert h.amplitude_v == 2.004748935
    assert h.amplitude_dbc == -6.0
    assert h.phase_deg == 0.0

    gen, _ = make({"C1:HARM?": "C1:HARM HARMSTATE,OFF"})
    h = gen.get_harmonics(1)
    assert h.enabled is False and h.type is None and h.order is None


def test_get_arb_empty_name_and_builtin():
    gen, _ = make({"C1:ARWV?": "C1:ARWV INDEX,0,NAME,"})
    a = gen.get_arb(1)
    assert (a.channel, a.index, a.name) == (1, 0, "")

    gen, _ = make({"C1:ARWV?": "C1:ARWV INDEX,2,NAME,StairUp"})
    a = gen.get_arb(1)
    assert (a.index, a.name) == (2, "StairUp")


def test_list_arbs_builtin_sorted_by_index():
    gen, _ = make(
        {"STL? BUILDIN": "STL M10, ExpFal, M100, ECG14, M101, ECG15, M11, ExpRise, M2, StairUp"}
    )
    arbs = gen.list_arbs()
    assert arbs == (
        ArbInfo(index=2, name="StairUp", builtin=True),
        ArbInfo(index=10, name="ExpFal", builtin=True),
        ArbInfo(index=11, name="ExpRise", builtin=True),
        ArbInfo(index=100, name="ECG14", builtin=True),
        ArbInfo(index=101, name="ECG15", builtin=True),
    )


def test_list_arbs_user_empty_and_named():
    gen, _ = make({"STL? USER": "STL WVNM"})
    assert gen.list_arbs("user") == ()

    gen, _ = make({"STL? USER": "STL WVNM,sinc_8M,wave1"})
    assert gen.list_arbs("user") == (
        ArbInfo(index=None, name="sinc_8M", builtin=False),
        ArbInfo(index=None, name="wave1", builtin=False),
    )
    with pytest.raises(SDG1032XValueError):
        gen.list_arbs("all")


def test_read_counter_off_and_on():
    gen, _ = make({"FCNT?": "FCNT STATE,OFF"})
    c = gen.read_counter()
    assert c.enabled is False and c.frequency_hz is None

    gen, _ = make({"FCNT?": FCNT_ON})
    c = gen.read_counter()
    assert c.enabled is True
    assert c.frequency_hz == 1e7
    assert c.duty_pct == 59.8568
    assert c.reference_hz == 1e7
    assert c.trigger_level_v == 0.0
    assert c.pulse_width_s == 5.98568e-08
    assert c.negative_width_s == 4.01432e-08
    assert c.deviation_ppm == 0.0
    assert c.coupling == "AC"
    assert c.high_frequency_reject is False


def test_get_coupling_off_and_on():
    gen, _ = make({"COUP?": "COUP TRACE,OFF,FCOUP,OFF,PCOUP,OFF,ACOUP,OFF"})
    c = gen.get_coupling()
    assert c.trace is False
    assert (c.freq_coupled, c.phase_coupled, c.amp_coupled) == (False, False, False)
    assert c.freq_deviation_hz is None

    gen, _ = make({"COUP?": "COUP TRACE,OFF,FCOUP,ON,PCOUP,ON,ACOUP,ON,FDEV,5HZ,PRAT,1,ARAT,2"})
    c = gen.get_coupling()
    assert (c.freq_coupled, c.phase_coupled, c.amp_coupled) == (True, True, True)
    assert c.freq_deviation_hz == 5.0
    assert c.phase_ratio == 1.0
    assert c.amp_ratio == 2.0
    assert c.freq_ratio is None and c.amp_deviation_v is None


def test_get_clock_bench_form_omits_10mout():
    gen, _ = make({"ROSC?": "ROSC INT"})
    assert gen.get_clock().to_dict() == {"source": "INT", "output_10m": None}

    gen, _ = make({"ROSC?": "ROSC INT,10MOUT,OFF"})
    assert gen.get_clock().to_dict() == {"source": "INT", "output_10m": False}


def test_get_sync_off_and_on():
    gen, _ = make({"C1:SYNC?": "C1:SYNC OFF"})
    s = gen.get_sync(1)
    assert (s.channel, s.enabled, s.type) == (1, False, None)

    gen, _ = make({"C1:SYNC?": "C1:SYNC ON,TYPE,MOD_CH1"})
    s = gen.get_sync(1)
    assert (s.enabled, s.type) == (True, "MOD_CH1")


def test_get_phase_mode_normalises_hyphen():
    gen, _ = make({"MODE?": "MODE PHASE-LOCKED"})
    assert gen.get_phase_mode() == "PHASELOCKED"
    gen, _ = make({"MODE?": "MODE INDEPENDENT"})
    assert gen.get_phase_mode() == "INDEPENDENT"


def test_get_screen_saver():
    gen, _ = make({"SCSV?": "SCSV OFF"})
    assert gen.get_screen_saver() == 0
    gen, _ = make({"SCSV?": "SCSV 5MIN"})
    assert gen.get_screen_saver() == 5


def test_get_number_format_with_spaces():
    gen, _ = make({"NBFM?": "NBFM PNT, DOT, SEPT, ON"})
    assert gen.get_number_format().to_dict() == {"point": "DOT", "separator": "ON"}


def test_get_protection_bare_on():
    gen, _ = make({"VOLTPRT?": "ON"})
    assert gen.get_protection().to_dict() == {"over_voltage": True}


def test_get_lan_config_quoted_quads():
    gen, _ = make(
        {
            "SYST:COMM:LAN:IPAD?": '"10.11.13.230"',
            "SYST:COMM:LAN:SMAS?": '"255.255.255.0"',
            "SYST:COMM:LAN:GAT?": '"10.11.13.1"',
        }
    )
    assert gen.get_lan_config().to_dict() == {
        "ip": "10.11.13.230",
        "mask": "255.255.255.0",
        "gateway": "10.11.13.1",
    }


def test_get_buzzer_invert_combine_language_power_on():
    gen, _ = make(
        {
            "BUZZ?": "BUZZ ON",
            "C1:INVT?": "C1:INVT OFF",
            "C2:CMBN?": "C2:CMBN ON",
            "LAGG?": "LAGG EN",
            "SCFG?": "SCFG DEFAULT",
        }
    )
    assert gen.get_buzzer() is True
    assert gen.get_invert(1) is False
    assert gen.get_combine(2) is True
    assert gen.get_language() == "EN"
    assert gen.get_power_on_config() == "DEFAULT"


def test_protocol_errors_on_unexpected_shapes():
    gen, _ = make({"C1:BSWV?": "C1:BSWV FRQ,1000HZ", "C1:OUTP?": "C1:OUTP LOAD,HZ"})
    with pytest.raises(SDG1032XProtocolError):
        gen.get_basic_wave(1)
    with pytest.raises(SDG1032XProtocolError):
        gen.get_output(1)
    gen, _ = make({"*IDN?": "Siglent,SDG1032X"})
    with pytest.raises(SDG1032XProtocolError):
        gen.info()


# ---------------------------------------------------------------- setters through the fake


def test_set_frequency_wire_format_and_read_back():
    gen, inst = make({"C1:BSWV?": BSWV})
    bw = gen.set_frequency(1, 1000)
    assert inst.writes[0] == "C1:BSWV FRQ,1000"
    assert bw.frequency_hz == 1000.0


def test_set_frequency_verify_error_carries_wanted_and_got():
    gen, inst = make({"C1:BSWV?": BSWV})
    with pytest.raises(SDG1032XVerifyError) as ei:
        gen.set_frequency(1, 1234.5)
    assert inst.writes[0] == "C1:BSWV FRQ,1234.5"
    e = ei.value
    assert (e.channel, e.field, e.wanted, e.got) == (1, "frequency_hz", 1234.5, 1000.0)
    assert "no error queue" in str(e)


def test_set_basic_wave_sends_wave_type_first():
    gen, inst = make({"C2:BSWV?": BSWV.replace("C1", "C2").replace("SINE", "SQUARE")})
    gen.set_basic_wave(2, frequency_hz=1000, wave_type="square", amplitude_vpp=4)
    assert inst.writes[0] == "C2:BSWV WVTP,SQUARE,FRQ,1000,AMP,4"


def test_set_output_on_off_verified():
    gen, inst = make({"C1:OUTP?": "C1:OUTP ON,LOAD,HZ,PLRT,NOR"})
    assert gen.set_output(1, True).enabled is True
    assert inst.writes[0] == "C1:OUTP ON"
    with pytest.raises(SDG1032XVerifyError) as ei:
        gen.set_output(1, False)
    assert ei.value.field == "enabled" and ei.value.wanted is False and ei.value.got is True


def test_set_phase_mode_sends_hyphenated_token():
    gen, inst = make({"MODE?": "MODE PHASE-LOCKED"})
    assert gen.set_phase_mode("PHASELOCKED") == "PHASELOCKED"
    assert inst.writes[0] == "MODE PHASE-LOCKED"
    gen, inst = make({"MODE?": "MODE INDEPENDENT"})
    assert gen.set_phase_mode("independent") == "INDEPENDENT"
    assert inst.writes[0] == "MODE INDEPENDENT"


def test_set_sync_type_verified_only_when_echoed():
    gen, inst = make({"C1:SYNC?": "C1:SYNC ON"})
    s = gen.set_sync(1, True, type="MOD_CH1")
    assert inst.writes[0] == "C1:SYNC ON,TYPE,MOD_CH1"
    assert s.enabled is True and s.type is None
    gen, _ = make({"C1:SYNC?": "C1:SYNC ON,TYPE,CH1"})
    with pytest.raises(SDG1032XVerifyError) as ei:
        gen.set_sync(1, True, type="MOD_CH1")
    assert ei.value.field == "type"


def test_set_burst_inf_cycles_alone_is_valid():
    gen, inst = make({"C2:BTWV?": BTWV_ON.replace("TIME,1,", "TIME,INF,")})
    b = gen.set_burst(2, cycles="INF")
    assert inst.writes[0] == "C2:BTWV TIME,INF"
    assert b.cycles is None
    gen, _ = make({"C2:BTWV?": BTWV_ON})
    with pytest.raises(SDG1032XVerifyError) as ei:
        gen.set_burst(2, cycles="INF")
    assert (ei.value.wanted, ei.value.got) == ("INF", 1)


def test_silent_rejection_amplitude_clamped_to_2mv():
    """Bench capture: ``C1:BSWV AMP,0.001`` reads back ``AMP,0.002V`` — the
    instrument clamps to its 2 mVpp minimum and says nothing."""
    gen, inst = make({"C1:BSWV?": BSWV.replace("AMP,4V", "AMP,0.002V")})
    with pytest.raises(SDG1032XVerifyError) as ei:
        gen.set_amplitude(1, 0.001)
    assert inst.writes[0] == "C1:BSWV AMP,0.001"
    assert (ei.value.field, ei.value.wanted, ei.value.got) == ("amplitude_vpp", 0.001, 0.002)
    assert gen.set_amplitude(1, 0.001, verify=False).amplitude_vpp == 0.002


def test_silent_rejection_duty_quantised_at_20mhz():
    """Bench capture: ``C1:BSWV WVTP,SQUARE,FRQ,20000000,DUTY,99`` reads back
    ``DUTY,59`` — the duty a 20 MHz square can support."""
    gen, inst = make(
        {
            "C1:BSWV?": (
                "C1:BSWV WVTP,SQUARE,FRQ,20000000HZ,PERI,5e-08S,AMP,4V,AMPVRMS,2Vrms,"
                "OFST,0V,HLEV,2V,LLEV,-2V,PHSE,0,DUTY,59"
            )
        }
    )
    with pytest.raises(SDG1032XVerifyError) as ei:
        gen.set_basic_wave(1, wave_type="SQUARE", frequency_hz=20e6, duty_pct=99)
    assert inst.writes[0] == "C1:BSWV WVTP,SQUARE,FRQ,20000000,DUTY,99"
    assert (ei.value.field, ei.value.wanted, ei.value.got) == ("duty_pct", 99.0, 59.0)


def test_set_max_amplitude_only_lowers_the_driver_cap():
    from benchctrl.drivers.siglent_sdg1032x.driver import SDG1032XPolicyError

    gen, inst = make({"C1:BSWV?": BSWV}, max_amplitude_vpp=4.0)
    assert gen.set_max_amplitude(2.0) == 2.0
    assert gen.max_amplitude_vpp == 2.0
    assert inst.writes == []  # nothing sent: the firmware does not enforce MAX_OUTPUT_AMP
    with pytest.raises(SDG1032XPolicyError):
        gen.set_max_amplitude(3.0)  # raising is an open() decision
    with pytest.raises(SDG1032XPolicyError):
        gen.set_amplitude(1, 3.0)
    with pytest.raises(SDG1032XValueError):
        gen.set_max_amplitude(0)
    gen, _ = make({})
    assert gen.set_max_amplitude(5.0) == 5.0  # no cap at open: any value sets one


def test_disable_outputs_reports_every_channel():
    gen, inst = make(
        {"C1:OUTP?": "C1:OUTP OFF,LOAD,HZ,PLRT,NOR", "C2:OUTP?": "C2:OUTP OFF,LOAD,50,PLRT,NOR"}
    )
    out = gen.disable_outputs()
    assert sorted(out) == [1, 2] and not out[1].enabled and out[2].load_ohm == 50.0
    assert inst.writes[0] == "C1:OUTP OFF" and inst.writes[2] == "C2:OUTP OFF"


def test_disable_outputs_still_on_is_a_verify_error():
    gen, _ = make(
        {"C1:OUTP?": "C1:OUTP ON,LOAD,HZ,PLRT,NOR", "C2:OUTP?": "C2:OUTP OFF,LOAD,HZ,PLRT,NOR"}
    )
    with pytest.raises(SDG1032XVerifyError, match="C1 still ON"):
        gen.disable_outputs()


def test_policy_gates_mutators_not_reads():
    from benchctrl.drivers.siglent_sdg1032x.driver import SDG1032XPolicyError

    gen, _ = make({"C2:BSWV?": BSWV.replace("C1", "C2")}, allowed_channels=(1,))
    assert gen.get_basic_wave(2).wave_type == "SINE"
    with pytest.raises(SDG1032XPolicyError):
        gen.set_frequency(2, 1000)
    with pytest.raises(SDG1032XPolicyError):
        gen.trigger_key("KB_OUTPUT1")


def test_verify_helper_raises_or_logs(caplog):
    gen, _ = make({})
    with pytest.raises(SDG1032XVerifyError) as ei:
        gen._verify(True, 2, "amplitude_vpp", 4.0, 3.5, "V")
    e = ei.value
    assert (e.channel, e.field, e.wanted, e.got) == (2, "amplitude_vpp", 4.0, 3.5)
    assert "C2 amplitude_vpp" in str(e)

    with caplog.at_level(logging.WARNING, logger="benchctrl.drivers.siglent_sdg1032x"):
        gen._verify(False, None, "phase_mode", "PHASELOCKED", "INDEPENDENT", "")
    assert "verify=False, accepted" in caplog.text
    assert "phase_mode" in caplog.text

    gen._verify(True, 1, "frequency_hz", 1000.0, 1000.000001, "Hz")  # within tolerance


# ---------------------------------------------------------------- binary paths through the fake


def test_read_screen_uses_bmp_size_and_drains_newline():
    body = b"\x00" * 100
    bmp = b"BM" + struct.pack("<I", 14 + len(body)) + bytes(8) + body
    gen, inst = make({}, raw=bmp + b"\n")
    got = gen.read_screen()
    assert got == bmp
    assert inst.writes == ["SCDP"]
    assert inst.buffer == b""  # stray newline drained
    assert inst.timeout == 5000  # transfer timeout restored


def test_read_screen_not_a_bmp():
    gen, _ = make({}, raw=b"C1:BSWV WVTP,SINE\n")
    with pytest.raises(SDG1032XProtocolError, match="BMP"):
        gen.read_screen()


def test_read_arb_parses_header_and_codes():
    codes = struct.pack("<4h", 0, 1000, -1000, 32767)
    head = b"C1:WVDT WVNM,wave1,LENGTH,8B,FREQ,1000HZ,AMPL,2V,OFST,0V,PHASE,0,WAVEDATA,"
    gen, inst = make({}, raw=head + codes + b"\n")
    a = gen.read_arb("wave1")
    assert inst.writes == ["WVDT? USER,wave1"]
    assert a.name == "wave1"
    assert (a.frequency_hz, a.amplitude_vpp, a.offset_v, a.phase_deg) == (1000.0, 2.0, 0.0, 0.0)
    assert a.codes == codes
    assert a.samples == (0, 1000, -1000, 32767)
    assert a.to_dict()["samples"] == 4 and a.to_dict()["bytes"] == 8
    with pytest.raises(SDG1032XValueError):
        gen.read_arb("bad name!")


def test_write_arb_frame_has_no_terminator_and_verifies():
    class RawInst(FakeInst):
        def write_raw(self, frame: bytes) -> None:
            self.frame = frame
            # The instrument stores it; read_arb then sees it back.
            n = len(frame.rsplit(b"WAVEDATA,", 1)[1])
            self.buffer = bytearray(
                b"C1:WVDT WVNM,w,LENGTH," + str(n).encode() + b"B,FREQ,1000HZ,AMPL,1V,OFST,0V,"
                b"PHASE,0,WAVEDATA," + frame.rsplit(b"WAVEDATA,", 1)[1] + b"\n"
            )

    inst = RawInst()
    gen = SiglentSDG1032X(inst, resource_string="SIM", max_amplitude_vpp=2.0)
    got = gen.write_arb("w", [0.0, 0.5, -0.5, 1.0])
    assert inst.frame.startswith(
        b"C1:WVDT WVNM,w,FREQ,1000,AMPL,1,OFST,0,PHASE,0,LENGTH,8B,WAVEDATA,"
    )
    assert not inst.frame.endswith(b"\n")
    assert got.samples == (0, 16384, -16384, 32767)

    from benchctrl.drivers.siglent_sdg1032x.driver import SDG1032XPolicyError

    with pytest.raises(SDG1032XPolicyError):
        gen.write_arb("w", [0.0, 1.0], amplitude_vpp=5.0)
    with pytest.raises(SDG1032XValueError):
        gen.write_arb("w", [0.0])
