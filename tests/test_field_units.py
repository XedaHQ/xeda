"""A quantity field is shown in its own unit, whatever unit the value was written in.

`Unit("s")` on a field gives its value that display unit: `timeout: 2 min` is `120 s` in the
text of the value, in `settings.json`, in the documents of the command line and in the listing of
the settings. The unit changes how a value is written. The value, its equality and its identity
stay as they were.
"""

import copy
import json
import pickle
from typing import Annotated, Literal, Optional, Union

import pytest
from pydantic import ValidationError

from xeda.dataclass import Field, XedaBaseModel, tier
from xeda.flow import flowrun_hash
from xeda.flow.settings_group import SettingsGroup
from xeda.flows import GhdlSim, Vcs, VivadoSynth
from xeda.frozen import freeze, thaw
from xeda.introspect import settings_in_field_units, settings_info, unit_of
from xeda.outcomes import spec_document
from xeda.units import (
    Bounds,
    Frequency,
    Length,
    QuantityError,
    Time,
    Unit,
    UNIT_KEY,
)
from xeda.utils import dump_json, json_encodable, semantic_hash

from .test_cli_json_contract import json_stdout, run_xeda
from . import test_modelsim
from .test_modelsim import needs_tclsh

pytestmark = pytest.mark.python_compat

# ------------------------------------------------------------------------------ the value


def test_a_quantity_is_shown_in_its_display_unit_and_is_the_same_value():
    q = Time("2 min")
    assert str(q) == "120000000000 ns" and q.display_unit == "ns"
    shown = q.with_unit("s")
    assert (str(shown), shown.display_unit) == ("120 s", "s")
    assert repr(shown) == "Time('120 s')"
    assert shown == q and hash(shown) == hash(q) and shown.magnitude == q.magnitude
    assert not shown < q and not q < shown and shown <= q <= shown
    assert shown.to("ps") == q.to("ps")
    assert semantic_hash(shown) == semantic_hash(q)
    assert json_encodable(shown) == "120 s"


def test_with_unit_is_idempotent_and_never_changes_what_it_copies():
    q = Time("2 min")
    shown = q.with_unit("s")
    assert shown.with_unit("s") is shown
    assert q.with_unit("ns") is q
    assert shown.with_unit("ps").with_unit("s").display_unit == "s"
    assert q.display_unit == "ns"
    assert shown.with_unit("ns").display_unit == "ns" and shown.with_unit("ns") == q


@pytest.mark.parametrize(
    "unit,message",
    [
        ("min", "no power of ten"),
        ("h", "no power of ten"),
        ("MHz", "is a frequency, not a time"),
        ("parsec", "unknown unit"),
        ("NS", "did you mean `ns`"),
    ],
)
def test_a_display_unit_is_a_unit_of_the_kind_with_a_factor_that_is_a_power_of_ten(unit, message):
    with pytest.raises(QuantityError, match=message):
        Time("1 ns").with_unit(unit)


def test_the_display_unit_survives_copies_pickling_and_freezing():
    shown = Time("2 min").with_unit("s")
    for restored in (
        copy.copy(shown),
        copy.deepcopy(shown),
        pickle.loads(pickle.dumps(shown)),
        thaw(freeze({"a": [shown]}))["a"][0],
    ):
        assert (restored, restored.display_unit, str(restored)) == (shown, "s", "120 s")


@pytest.mark.parametrize("unit", ["fs", "ps", "ns", "us", "ms", "s"])
@pytest.mark.parametrize(
    "magnitude",
    [
        0.0,
        1e-05,
        0.1,
        123456.789,
        1e16,
        -2.5e-07,
        5e-324,
        2.2250738585072014e-308,
        1.7976931348623157e308,
    ],
)
def test_the_text_is_a_plain_decimal_that_reads_back_as_the_same_value(unit, magnitude):
    shown = Time.of(magnitude, "ns").with_unit(unit)
    text = str(shown)
    number, written_unit = text.split(" ")
    assert written_unit == unit and "e" not in number.lower() and number != "-0"
    back = Time(text)
    assert back.magnitude == magnitude and back == shown


def test_a_small_value_has_no_exponent():
    assert str(Time("1e-05 ns")) == "0.00001 ns"
    assert str(Time("1e-05 ns").with_unit("ps")) == "0.01 ps"
    assert str(Time("10 ms")) == "10000000 ns"
    assert str(Time("10 ms").with_unit("s")) == "0.01 s"
    assert str(Time("0 ns").with_unit("s")) == "0 s"
    assert str(Time("-1500 ps")) == "-1.5 ns"


# ------------------------------------------------------------------------------ the marker


def test_a_marker_that_cannot_work_fails_when_the_model_is_built():
    def build(annotation):
        return type("Toy", (XedaBaseModel,), {"__annotations__": {"x": annotation}})

    for annotation, message in (
        (Annotated[Time, Unit("min")], "no power of ten"),
        (Annotated[Time, Unit("MHz")], "is a frequency, not a time"),
        (Annotated[Time, Unit("s"), Unit("ns")], "has a unit already"),
        (Annotated[Time, Bounds(gt="0 s"), Unit("s")], "must come before Bounds"),
        (Annotated[int, Unit("s")], "gives a quantity its unit"),
        (Annotated[Optional[Time], Unit("s")], "gives a quantity its unit"),
    ):
        with pytest.raises(TypeError, match=message):
            build(annotation)
    build(Optional[Annotated[Time, Unit("s"), Bounds(gt="0 s")]])
    build(list[Annotated[Time, Unit("s")]])


class Group(SettingsGroup):
    settle: Annotated[Time, Unit("us")] = Field(
        Time("1 ns"), description="A group time.", json_schema_extra=tier("common")
    )


class Inner(XedaBaseModel):
    pause: Annotated[Time, Unit("ps")] = Time("1 ns")


class Toy(XedaBaseModel):
    seconds: Optional[Annotated[Time, Unit("s"), Bounds(gt="0 s")]] = Time("100 min")
    nanoseconds: Annotated[Time, Unit("ns")] = Time("1 us")
    plain: Time = Time("1 us")
    gigahertz: Annotated[Frequency, Unit("GHz")] = Frequency("100 MHz")
    megahertz: Frequency = Frequency("100 MHz")
    micrometers: Annotated[Length, Unit("nm")] = Length("1 um")
    mixed: Union[Literal["auto"], Annotated[Time, Unit("ms")]] = Time("1 s")
    inner: Inner = Field(default_factory=Inner)
    group: Group = Field(default_factory=Group)
    by_name: dict[str, Annotated[Time, Unit("ps")]] = {}
    in_order: list[Annotated[Time, Unit("us")]] = []
    pair: tuple[str, Annotated[Time, Unit("fs")]] = Field(
        default_factory=lambda: ("a", Time("1 ps"))
    )


def shown(model: XedaBaseModel) -> dict:
    return model.model_dump(mode="json")


def test_each_field_shows_its_value_in_its_own_unit():
    toy = Toy(
        seconds="2 min",
        nanoseconds="3 us",
        plain="3 us",
        gigahertz="250 MHz",
        megahertz="0.25 GHz",
        micrometers="0.002 mm",
        mixed="1500 us",
        inner={"pause": "2 ns"},
        group={"settle": "5000 ns"},
        by_name={"a": "1 ns", "b": "0.5 us"},
        in_order=["1 ns", "2 ms"],
        pair=("x", "1 ps"),
    )
    assert shown(toy) == {
        "seconds": "120 s",
        "nanoseconds": "3000 ns",
        "plain": "3000 ns",
        "gigahertz": "0.25 GHz",
        "megahertz": "250 MHz",
        "micrometers": "2000 nm",
        "mixed": "1.5 ms",
        "inner": {"pause": "2000 ps"},
        "group": {"settle": "5 us"},
        "by_name": {"a": "1000 ps", "b": "500000 ps"},
        "in_order": ["0.001 us", "2000 us"],
        "pair": ["x", "1000 fs"],
    }
    assert json.loads(toy.model_dump_json()) == shown(toy)
    assert str(toy.seconds) == "120 s" and toy.seconds == Time("2 min")
    assert str(toy.plain) == "3000 ns"


def test_the_default_is_shown_in_the_unit_of_the_field():
    toy = Toy()
    assert str(toy.seconds) == "6000 s" and shown(toy)["seconds"] == "6000 s"
    assert shown(toy)["group"] == {"settle": "0.001 us"}
    schema = Toy.model_json_schema()["properties"]
    assert schema["seconds"]["default"] == "6000 s"
    assert schema["nanoseconds"]["default"] == "1000 ns"
    assert schema["gigahertz"]["default"] == "0.1 GHz"
    assert schema["megahertz"]["default"] == "100 MHz"
    assert Toy.model_json_schema(mode="serialization")["properties"]["seconds"]["default"] == (
        "6000 s"
    )


def test_the_schema_names_the_unit_of_each_quantity():
    schema = Toy.model_json_schema()
    properties = schema["properties"]
    assert properties["seconds"]["anyOf"][0][UNIT_KEY] == "s"
    assert properties["plain"][UNIT_KEY] == "ns"  # the unit of the kind
    assert properties["megahertz"][UNIT_KEY] == "MHz"
    assert properties["by_name"]["additionalProperties"][UNIT_KEY] == "ps"
    assert unit_of(properties["seconds"]) == "s"
    assert unit_of(properties["in_order"]) == "us"
    assert unit_of(properties["mixed"]) == "ms"
    assert unit_of(properties["inner"]) is None  # a model: its own fields have units
    assert unit_of(properties["pair"]) == "fs"
    assert schema["$defs"]["Inner"]["properties"]["pause"][UNIT_KEY] == "ps"


def test_assignment_and_reloading_give_the_field_unit_again():
    toy = Toy()
    toy.seconds = "3 min"
    toy.by_name = {"k": "2 ns"}
    toy.inner.pause = "1 ns"
    assert (str(toy.seconds), str(toy.by_name["k"]), str(toy.inner.pause)) == (
        "180 s",
        "2000 ps",
        "1000 ps",
    )
    assert toy.seconds.display_unit == "s"
    again = Toy(**shown(toy))
    assert again == toy and shown(again) == shown(toy)
    assert Toy.model_validate_json(toy.model_dump_json()) == toy
    with pytest.raises(ValidationError, match="must be greater than 0 s, got -1 s"):
        toy.seconds = "-1 s"
    with pytest.raises(ValidationError, match="must be greater than 0 s, got 0 s"):
        Toy(seconds="0 ns")


def test_a_validated_value_keeps_its_unit_through_copies_and_pickling():
    toy = Toy(seconds="2 min", by_name={"a": "1 ns"})
    for twin in (toy.model_copy(deep=True), pickle.loads(pickle.dumps(toy))):
        assert shown(twin) == shown(toy)
        assert str(twin.seconds) == "120 s" and str(twin.by_name["a"]) == "1000 ps"
    assert Toy(**{**shown(toy), "seconds": toy.seconds}).seconds.display_unit == "s"


def test_a_field_without_a_unit_writes_in_the_unit_of_its_kind():
    """A value comes with the display unit of the field it was validated by. Another field
    does not keep it."""
    seconds = Toy(seconds="2 min").seconds
    assert seconds.display_unit == "s"
    toy = Toy(plain=seconds, nanoseconds=seconds, by_name={"a": seconds})
    assert str(toy.plain) == "120000000000 ns"
    assert str(toy.nanoseconds) == "120000000000 ns"
    assert str(toy.by_name["a"]) == "120000000000000 ps"
    toy.plain = toy.seconds
    assert toy.plain.display_unit == "ns" and toy.seconds.display_unit == "s"


def test_two_fields_that_get_one_value_each_show_their_own_unit():
    q = Time("1 ns")
    toy = Toy(seconds=q, nanoseconds=q, plain=q, inner={"pause": q})
    assert [str(v) for v in (toy.seconds, toy.nanoseconds, toy.plain, toy.inner.pause)] == [
        "0.000000001 s",
        "1 ns",
        "1 ns",
        "1000 ps",
    ]
    assert q.display_unit == "ns"


# ------------------------------------------------------------------------------ the flows


def test_the_timeout_is_in_seconds_and_the_simulated_times_in_nanoseconds():
    settings = GhdlSim.Settings(timeout="2 min", stop_time="1 us")
    assert (str(settings.timeout), str(settings.stop_time)) == ("120 s", "1000 ns")
    vcs = Vcs.Settings(lic_wait="2 h")
    assert (str(vcs.lic_wait), str(vcs.time_unit), str(vcs.time_resolution)) == (
        "7200 s",
        "1 ns",
        "0.001 ns",
    )
    synth = VivadoSynth.Settings(
        fpga={"part": "xc7a100tcsg324-1"}, default_max_input_delay="500 ps"
    )
    assert str(synth.default_max_input_delay) == "0.5 ns"


def test_the_unit_of_a_flow_setting_changes_no_value_and_no_identity():
    """The identities were computed before the units existed, from the same values."""
    ghdl = GhdlSim.Settings(timeout="2 min", stop_time="100 us")
    assert flowrun_hash("ghdl_sim", ghdl, "d") == (
        "35256887510ed8ad72a68ec1450a3164874306750bd1d65e522843dd81f70355"
    )
    vcs = Vcs.Settings(lic_wait="2 h", time_unit="10 ps")
    assert flowrun_hash("vcs", vcs, "d") == (
        "e2f39d649c997102c127c924d6d5e656b760e0bdd6b74528dcd56c5e6324075f"
    )
    synth = VivadoSynth.Settings(
        fpga={"part": "xc7a100tcsg324-1"},
        clocks={"main_clock": {"period": "5 ns"}},
        default_max_input_delay="1.5 ns",
    )
    assert flowrun_hash("vivado_synth", synth, "d") == (
        "ceb16ada5879b862803ec62f784c1405592e5719482176847a1b5d09421398ce"
    )
    assert semantic_hash([Time("2 min"), Frequency("100 MHz")]) == (
        "7a0db1f8f6037166c8f4f3bb04ba9656a3a969720fc32155a0b281a7d2dc3ab8"
    )
    assert flowrun_hash("ghdl_sim", GhdlSim.Settings(timeout="120 s"), "d") == flowrun_hash(
        "ghdl_sim", GhdlSim.Settings(timeout="2 min"), "d"
    )


@needs_tclsh
def test_a_launch_writes_the_timeout_in_seconds(tmp_path, monkeypatch):
    flow, _ = test_modelsim._run(tmp_path, monkeypatch, {"timeout": "2 min", "stop_time": "1 us"})
    written = json.loads((flow.run_path / "settings.json").read_text())
    for section in ("flow_settings", "effective_flow_settings"):
        assert (written[section]["timeout"], written[section]["stop_time"]) == ("120 s", "1000 ns")
    # the file reads back as the same settings
    reloaded = type(flow.settings)(**written["effective_flow_settings"])
    assert str(reloaded.timeout) == "120 s" and reloaded.timeout == flow.settings.timeout
    dumped = tmp_path / "dump.json"
    dump_json({"timeout": flow.settings.timeout}, dumped)
    assert json.loads(dumped.read_text()) == {"timeout": "120 s"}


def test_the_document_of_a_run_echoes_the_settings_it_got_in_the_unit_of_each_field():
    document = spec_document(
        flow="ghdl_sim",
        design="d",
        design_file=None,
        target=None,
        settings={"ghdl_sim": {"timeout": "2min", "stop_time": "1 us", "vcd": "w.vcd"}},
    )
    assert document["settings"] == {
        "ghdl_sim": {"timeout": "120 s", "stop_time": "1000 ns", "vcd": "w.vcd"}
    }
    # keys that were not given stay out; text that is no quantity stays for the launch to refuse
    odd = settings_in_field_units(GhdlSim, {"timeout": "soon", "stop_time": 5, "x": "1 min"})
    assert odd == {"timeout": "soon", "stop_time": 5, "x": "1 min"}
    nested = settings_in_field_units(
        VivadoSynth,
        {"clocks": {"main_clock": {"period": "5000 ps"}, "c": {"freq": "0.1 GHz"}}},
    )
    assert nested == {"clocks": {"main_clock": {"period": "5 ns"}, "c": {"freq": "100 MHz"}}}


@needs_tclsh
def test_a_command_line_launch_shows_the_unit_of_the_field(tmp_path):
    design = tmp_path / "design"
    design.mkdir()
    test_modelsim._design(design)
    (design / "d.yaml").write_text(
        "name: d\nrtl: {top: uut, sources: [uut.vhd]}\ntb: {top: tb, sources: [tb.v]}\n"
    )
    proc = run_xeda(
        "run",
        "modelsim",
        str(design / "d.yaml"),
        "-s",
        "timeout=2min",
        "--json",
        cwd=tmp_path,
        fake_tools=True,
    )
    document = json_stdout(proc)
    assert document["spec"]["settings"] == {"modelsim": {"timeout": "120 s"}}
    node = document["nodes"][-1]
    assert node["record"]["settings"]["timeout"] == "120 s"


def test_the_listing_of_the_settings_has_a_unit_column():
    fields = {f["name"]: f for f in settings_info("vcs")["fields"]}
    assert (fields["timeout"]["unit"], fields["timeout"]["default"]) == ("s", None)
    assert (fields["lic_wait"]["unit"], fields["lic_wait"]["default"]) == ("s", "6000 s")
    assert (fields["time_unit"]["unit"], fields["time_unit"]["default"]) == ("ns", "1 ns")
    assert (fields["time_resolution"]["default"]) == "0.001 ns"
    assert fields["stop_time"]["unit"] == "ns"
    assert fields["full64"]["unit"] is None
    assert fields["lic_wait"]["json_schema"]["default"] == "6000 s"
    clock = {f["name"]: f for f in settings_info("vivado_synth")["fields"]}
    assert clock["default_max_input_delay"]["unit"] == "ns"
    assert clock["clocks"]["unit"] is None  # a table of models
    listing = settings_info("vivado_synth")["json_schema"]["$defs"]["PhysicalClock"]
    assert listing["properties"]["period"][UNIT_KEY] == "ns"


def test_the_command_line_lists_the_unit_of_each_setting(tmp_path):
    proc = run_xeda("list-settings", "vcs", "--json", cwd=tmp_path)
    fields = {f["name"]: f for f in json_stdout(proc)["fields"]}
    assert fields["timeout"]["unit"] == "s" and fields["lic_wait"]["default"] == "6000 s"
    table = run_xeda("list-settings", "vcs", cwd=tmp_path).stdout
    assert "Unit" in table and "6000 s" in table


def test_a_bound_message_shows_both_numbers_in_the_unit_of_the_field():
    with pytest.raises(Exception, match="must be greater than 0 s, got -1 s"):
        GhdlSim.Settings(timeout="-1 s")
    with pytest.raises(Exception, match="must be at least 0 ns, got -1 ns"):
        GhdlSim.Settings(stop_time="-1000 ps")
