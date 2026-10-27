"""Clock specification: a period and a frequency are quantities with units.

`clock.period: 10` means nothing without a unit, so the clock settings that every synthesis flow
shares take text such as `10 ns` or `100 MHz`, and a bare number is refused. This includes the
command line's `-s clock.period=5.5` path, which hands the model raw text.
"""

import json
import subprocess
import sys
from decimal import Decimal

import pytest

from xeda.dataclass import ValidationError
from xeda.flow import FPGA, FlowSettingsError
from xeda.flow.flow import flowrun_hash
from xeda.flow.synth import PhysicalClock
from xeda.flows import Nextpnr, Quartus, VivadoSynth, Yosys, YosysFpga
from xeda.units import Frequency, Time
from .tool_utils import HANG_GUARD

pytestmark = pytest.mark.python_compat

REFUSED = (FlowSettingsError, ValidationError, ValueError)


@pytest.mark.parametrize(
    "period, expected",
    [("5.5 ns", 5.5), ("5.5ns", 5.5), ("5 ns", 5.0), ("5000 ps", 5.0), (Time("5 ns"), 5.0)],
)
def test_period_takes_a_time(period, expected):
    clock = PhysicalClock(period=period)  # type: ignore[call-arg]
    assert isinstance(clock.period, Time)
    assert clock.period.to("ns") == expected
    assert isinstance(clock.freq, Frequency)
    assert clock.freq.to("MHz") == pytest.approx(1000.0 / expected)


@pytest.mark.parametrize(
    "freq", ["200 MHz", "200MHz", "0.2 GHz", "200000 kHz", Frequency("200 MHz")]
)
def test_freq_takes_a_frequency(freq):
    clock = PhysicalClock(freq=freq)  # type: ignore[call-arg]
    assert clock.freq.to("MHz") == pytest.approx(200.0)
    assert clock.period == Time("5 ns")


def test_a_period_derived_from_a_frequency_is_the_one_computed_before_units():
    """The derivation is `1000.0 / MHz`, in float arithmetic, so a clock given by frequency
    keeps the period (and the run identity) it had."""
    for mhz in (125.0, 300.0, 333.333, 7.0):
        clock = PhysicalClock(freq=f"{mhz} MHz")  # type: ignore[call-arg]
        assert clock.period.to("ns") == 1000.0 / mhz


def test_the_frequency_setter_takes_text_or_a_frequency():
    clock = PhysicalClock(period="1 ns")  # type: ignore[call-arg]
    clock.freq = "250 MHz"
    assert clock.period == Time("4 ns")
    clock.freq = Frequency("0.1 GHz")
    assert clock.period == Time("10 ns")
    with pytest.raises(ValueError, match="needs a unit"):
        clock.freq = 100  # type: ignore[assignment]
    with pytest.raises(ValueError, match="greater than 0 MHz"):
        clock.freq = "0 MHz"
    assert clock.period == Time("10 ns")


def test_the_period_can_be_assigned_text():
    clock = PhysicalClock(period="1 ns")  # type: ignore[call-arg]
    clock.period = "2500 ps"  # type: ignore[assignment]
    assert clock.period == Time("2.5 ns")
    with pytest.raises(ValidationError, match="needs a unit"):
        clock.period = 2.5  # type: ignore[assignment]


def test_the_other_times_of_a_clock_are_times():
    clock = PhysicalClock(
        period="10 ns", rise="0.5 ns", uncertainty="100 ps", skew="0.05 ns"  # type: ignore[call-arg]
    )
    assert clock.rise == Time("0.5 ns")
    assert clock.uncertainty == Time("0.1 ns")
    assert clock.skew == Time("0.05 ns")
    assert PhysicalClock(period="10 ns").rise == Time("0 ns")  # type: ignore[call-arg]
    assert PhysicalClock(period="10 ns").uncertainty is None  # type: ignore[call-arg]
    assert clock.fall == Time("5.5 ns")


def test_the_fall_time_is_a_time_and_stays_inside_the_period():
    assert PhysicalClock(period="10 ns", duty_cycle=0.25).fall == Time("2.5 ns")  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="Fall time is beyond the period"):
        PhysicalClock(period="10 ns", rise="9 ns").fall  # type: ignore[call-arg]


@pytest.mark.parametrize("value", [0.5, "0.5", " 0.5 ", Decimal("0.5")])
def test_duty_cycle_accepts_unitless_numeric_values(value):
    """The command line form `-s clock.duty_cycle=0.5` arrives as the text `"0.5"`."""
    clock = PhysicalClock(period="5 ns", duty_cycle=value)  # type: ignore[call-arg]
    assert clock.duty_cycle == 0.5
    assert clock.fall == Time("2.5 ns")


def test_the_duty_cycle_is_a_marked_fraction():
    from xeda.units import measure_of

    assert measure_of(PhysicalClock, "duty_cycle") == "ratio"


def test_contradictory_period_and_freq_are_rejected():
    with pytest.raises(ValidationError, match="disagree"):
        PhysicalClock(period="4 ns", freq="1000 MHz")  # type: ignore[call-arg]


def test_a_period_and_a_frequency_that_agree_are_accepted():
    clock = PhysicalClock(period="4 ns", freq="250 MHz")  # type: ignore[call-arg]
    assert clock.period == Time("4 ns")


def test_a_historically_rounded_period_frequency_pair_is_accepted():
    clock = PhysicalClock(period="3.333 ns", freq="300 MHz")  # type: ignore[call-arg]
    assert clock.period == Time("3.333 ns")
    assert clock.freq.to("MHz") == pytest.approx(300.030003)


def test_frequency_input_has_one_stored_value_and_round_trips_exactly():
    clock = PhysicalClock(freq="300 MHz")  # type: ignore[call-arg]
    dumped = clock.model_dump()
    reloaded = PhysicalClock.model_validate(dumped)

    assert "freq" not in dumped
    assert reloaded == clock
    assert reloaded.freq.to("MHz") == pytest.approx(300.0)
    assert PhysicalClock.model_validate(clock.model_dump(mode="json")) == clock


def test_the_json_form_of_a_clock_is_text_with_a_unit():
    clock = PhysicalClock(period="5000 ps", rise="0.5 ns")  # type: ignore[call-arg]
    dumped = clock.model_dump(mode="json")
    assert dumped["period"] == "5 ns"
    assert dumped["rise"] == "0.5 ns"
    assert dumped["uncertainty"] is None
    assert dumped["duty_cycle"] == 0.5


def test_physical_clock_schema_accepts_either_input_spelling():
    schema = PhysicalClock.model_json_schema()
    assert schema["properties"]["freq"]["type"] == "string"
    assert schema["properties"]["freq"]["x-xeda-quantity"] == "frequency"
    assert schema["properties"]["period"]["x-xeda-quantity"] == "time"
    assert schema["properties"]["period"]["type"] == "string"
    assert "default" not in schema["properties"]["period"]
    assert schema["anyOf"] == [{"required": ["period"]}, {"required": ["freq"]}]
    assert "period" not in schema.get("required", [])
    assert schema["properties"]["rise"]["x-xeda-quantity"] == "time"


def test_the_descriptions_say_that_a_value_takes_a_unit():
    properties = PhysicalClock.model_json_schema()["properties"]
    for name in ("period", "freq", "rise", "uncertainty", "skew"):
        description = properties[name]["description"]
        assert " in ns" not in description and "in MHz" not in description, name
        assert "unit" in description, name


@pytest.mark.parametrize(
    "period, problem",
    [
        (10, "needs a unit"),
        (10.0, "needs a unit"),
        ("10", "needs a unit"),
        ("5 MHz", "is a frequency, not a time"),
        ("0 ns", "greater than 0 ns"),
        ("-1 ns", "greater than 0 ns"),
        ("0 ps", "greater than 0 ns"),
        ("5 NS", "case-sensitive"),
        ("5 furlongs", "unknown unit"),
        (True, "needs a unit"),
    ],
)
def test_a_bad_period_is_refused_at_the_period(period, problem):
    with pytest.raises(ValidationError, match=problem) as excinfo:
        PhysicalClock(period=period)  # type: ignore[call-arg]
    assert [error["loc"] for error in excinfo.value.errors()] == [("period",)]


@pytest.mark.parametrize("field", ["rise", "uncertainty", "skew"])
@pytest.mark.parametrize("value", [1, 0.5, "0.5", "5 MHz"])
def test_the_other_times_refuse_a_bare_number_and_another_kind(field, value):
    with pytest.raises(ValidationError) as excinfo:
        PhysicalClock(period="10 ns", **{field: value})  # type: ignore[call-arg]
    assert [error["loc"] for error in excinfo.value.errors()] == [(field,)]


@pytest.mark.parametrize(
    "freq, problem",
    [
        (100, "needs a unit"),
        (100.0, "needs a unit"),
        ("100", "needs a unit"),
        ("10 ns", "is a time, not a frequency"),
        ("0 MHz", "greater than 0 MHz"),
        ("-5 MHz", "greater than 0 MHz"),
        ("5 mhz", "case-sensitive"),
    ],
)
def test_a_bad_frequency_is_refused_and_named(freq, problem):
    """A zero frequency names the real problem, not an omitted `freq`."""
    with pytest.raises(ValidationError, match=problem) as excinfo:
        PhysicalClock(freq=freq)  # type: ignore[call-arg]
    assert "`freq`" in str(excinfo.value)
    assert "Neither `freq` nor `period`" not in str(excinfo.value)


def test_neither_period_nor_freq_is_rejected():
    with pytest.raises(REFUSED) as excinfo:
        PhysicalClock()  # type: ignore[call-arg]
    assert "Neither `freq` nor `period` was specified" in str(excinfo.value)


@pytest.mark.parametrize(
    "clock, problem",
    [
        ({"period": 10}, "period.*needs a unit"),
        ({"period": "10"}, "period.*needs a unit"),
        ({"period": "5 MHz"}, "period.*is a frequency, not a time"),
        ({"freq": "10 ns"}, "`freq`.*is a time, not a frequency"),
        ({"freq": 100}, "`freq`.*needs a unit"),
        ({"period": "0 ns"}, "period.*greater than 0 ns"),
        ({"period": "-1 ns"}, "period.*greater than 0 ns"),
    ],
)
def test_a_flow_refuses_a_bad_clock(clock, problem):
    with pytest.raises(REFUSED, match=f"(?s){problem}"):
        VivadoSynth.Settings(fpga=FPGA("xc7a12tcsg325-1"), clock=clock)


@pytest.mark.parametrize("value", ["5.5", "5", "0"])
def test_a_bare_number_from_the_command_line_names_the_missing_unit(value):
    """`-s clock.period=5.5` hands the model the text `5.5`."""
    with pytest.raises(REFUSED, match="needs a unit, such as `10 ns`"):
        VivadoSynth.Settings(fpga="xc7a100tftg256-2L", clock={"period": value})  # type: ignore[arg-type]


@pytest.mark.parametrize("extra", [{}, {"clocks": {"main_clock": {"period": "5 ns"}}}])
def test_clock_period_was_removed(extra):
    with pytest.raises(
        REFUSED,
        match="`clock_period` was removed: use `clock.period` or `clock.freq`",
    ):
        VivadoSynth.Settings(fpga=FPGA("xc7a12tcsg325-1"), clock_period=5.0, **extra)


@pytest.mark.parametrize("value", ["0.5ns", "50%", "half"])
def test_duty_cycle_rejects_units_and_non_numeric_values(value):
    """Duty cycle is a unitless ratio, unlike the clock timing fields."""
    with pytest.raises(ValidationError, match="unitless number"):
        PhysicalClock(period="5 ns", duty_cycle=value)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "overrides",
    [
        {"clock": {"period": "5.5 ns"}},
        {"clocks": {"main_clock": {"period": "5.5ns"}}},
        {"clocks": {"main_clock": {"freq": "181.818 MHz"}}},
    ],
)
def test_synth_flow_settings_accept_string_clock_overrides(overrides):
    """`xeda run vivado_synth <design> -s clock.period=5.5ns` hands settings raw strings."""
    settings = VivadoSynth.Settings(fpga="xc7a100tftg256-2L", **overrides)  # type: ignore[arg-type]
    assert settings.main_clock is not None
    assert settings.main_clock.period.to("ns") == pytest.approx(5.5, abs=1e-3)


def test_settings_json_writes_a_period_as_text_and_reads_it_back_with_the_same_identity():
    settings = VivadoSynth.Settings(
        fpga="xc7a100tftg256-2L", clock={"period": "5000 ps"}  # type: ignore[arg-type]
    )
    document = json.loads(settings.model_dump_json())
    assert document["clocks"]["main_clock"]["period"] == "5 ns"
    reloaded = VivadoSynth.Settings.model_validate(document)
    assert reloaded.clocks == settings.clocks
    assert flowrun_hash("vivado_synth", reloaded, "d") == flowrun_hash(
        "vivado_synth", settings, "d"
    )


# ------------------------------------------------------------------------- run identity

#: The digests that the clock settings had when they were plain numbers (`10.0`, `2.5`, ...).
#: An unchanged clock keeps its run identity, whatever unit it is written in. The digests of the
#: yosys flows changed once more when their setting `script_format` was removed.
IDENTITY_PINS = [
    pytest.param(
        VivadoSynth,
        "vivado_synth",
        {"fpga.part": "xc7a100tcsg324-1", "clock.period": "10 ns"},
        "d60eef71efbb6f3c35f9d8a3a1da122533ac16cc058b732c930fc5904db22db9",
        id="vivado_synth",
    ),
    pytest.param(
        YosysFpga,
        "yosys_fpga",
        {"fpga.part": "xc7a100tcsg324-1", "clock.freq": "125 MHz"},
        "c198e8be903e03680398ca23e735a956546cbfe08c9468011859086839b1a3a2",
        id="yosys_fpga",
    ),
    pytest.param(
        Nextpnr,
        "nextpnr",
        {"fpga.part": "LFE5U-25F-6BG381C", "clocks.clk": {"period": "3.3 ns", "port": "clk"}},
        "27d04419599f2909bc75daee90b194c0e203bac884823d79e3e0e66ce05c9cda",
        id="nextpnr",
    ),
    pytest.param(
        Quartus,
        "quartus",
        {
            "fpga.part": "10CL016YU256C8G",
            "clock": {
                "period": "4 ns",
                "uncertainty": "0.1 ns",
                "skew": "0.05 ns",
                "rise": "0.5 ns",
            },
        },
        "0ed7b7739b3b7a683651aff88f2bfc6d6ff99604596cdcca970a0aa2195743f8",
        id="quartus",
    ),
    pytest.param(
        Yosys,
        "yosys",
        {"platform": "nangate45", "clock.period": "2.5 ns"},
        "76cdee59359d9b28d4c40942d1202c60304e297b9ed8341d37db1e383ff762c3",
        id="yosys",
    ),
]


def _settings(cls, flat):
    from xeda.utils import set_hierarchy

    nested: dict = {}
    for key, value in flat.items():
        set_hierarchy(nested, key, value)
    return cls.Settings(**nested)


@pytest.mark.parametrize("cls, name, flat, digest", IDENTITY_PINS)
def test_a_clock_keeps_the_run_identity_it_had_as_a_plain_number(cls, name, flat, digest):
    assert flowrun_hash(name, _settings(cls, flat), "pin") == digest


def test_the_same_clock_in_another_unit_has_the_same_identity():
    in_ns = Yosys.Settings(platform="nangate45", clock={"period": "2.5 ns"})  # type: ignore[arg-type]
    in_ps = Yosys.Settings(platform="nangate45", clock={"period": "2500 ps"})  # type: ignore[arg-type]
    assert (
        flowrun_hash("yosys", in_ps, "pin")
        == flowrun_hash("yosys", in_ns, "pin")
        == "76cdee59359d9b28d4c40942d1202c60304e297b9ed8341d37db1e383ff762c3"
    )


# ------------------------------------------------------------------------- the command line


def test_a_bare_period_on_the_command_line_fails_with_the_unit_message_in_json(tmp_path):
    (tmp_path / "top.v").write_text("module top(input clk); endmodule\n")
    (tmp_path / "d.yaml").write_text(
        'name: d\nrtl:\n  sources: ["top.v"]\n  top: top\n  clock:\n    port: clk\n'
    )
    proc = subprocess.run(
        [sys.executable, "-m", "xeda", "run", "vivado_synth", "d.yaml", "--run-root"]
        + [str(tmp_path / "xeda_run"), "-s", "fpga.part=xc7a12tcsg325-1", "clock.period=5.5"]
        + ["--json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=HANG_GUARD,
    )
    document = json.loads(proc.stdout)
    assert proc.returncode == 2
    assert document["outcome"] is None and document["nodes"] == []
    assert "needs a unit" in json.dumps(document["error"])
    assert "clock" in json.dumps(document["error"])
