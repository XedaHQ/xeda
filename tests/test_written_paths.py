"""Every path a flow writes is declared, by a role: a working location is a name inside
the run directory; a deliverable is a name there or a location, which is delivered."""

import re
import typing
from pathlib import Path

import pytest
from pydantic import BaseModel

from xeda import Design
from xeda.dataclass import DELIVERABLE_ROLE, WORKING_ROLE, conventional_output, written_role
from xeda.deliver import split_deliveries
from xeda.flow import FlowSettingsError
from xeda.flow.flow import _annotation_contains_path, written_path_problems
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.trace_inputs import setting_files, setting_path_leaves
from xeda.utils import LOCATION_FORMS, PATH_VARIABLES

from .settings_samples import flow_classes, minimal_settings

W, D = WORKING_ROLE, DELIVERABLE_ROLE

#: (owner class, field) -> role: the table of every written field. Every other path field is read.
ROLES = {
    ("Flow.Settings", "reports_dir"): W,
    ("Flow.Settings", "outputs_dir"): W,
    ("Flow.Settings", "checkpoints_dir"): W,
    ("Openroad.Settings", "results_dir"): W,
    ("Openroad.Settings", "write_metrics"): D,
    ("BscFlow.Settings", "bobj_dir"): W,
    ("BscFlow.Settings", "info_dir"): W,
    ("Bsc.Settings", "verilog_out_dir"): W,
    ("BscSim.Settings", "sim_dir"): W,
    ("DiamondSynth.Settings", "impl_folder"): W,
    ("Dc.Settings", "alib_dir"): W,
    ("Dc.Settings", "log_file"): W,
    ("Verilator.Settings", "sim_dir"): W,
    ("Verilator.Settings", "fst"): D,
    ("Verilator.Settings", "saif"): D,
    ("Vcs.Settings", "work_dir"): W,
    ("Vcs.Settings", "vcs_log_file"): W,
    ("Vcs.Settings", "fsdb"): D,
    ("Vcs.Settings", "vpd"): D,
    ("Vcs.Settings", "evcd"): D,
    ("YosysBase.Settings", "log_file"): W,
    **{
        ("YosysBase.Settings", name): D
        for name in (
            "netlist_json",
            "netlist_verilog",
            "netlist_graph",
            "write_blif",
            "rtl_json",
            "rtl_verilog",
            "rtl_graph",
        )
    },
    ("YosysSim.Settings", "netlist_json"): D,
    ("YosysSim.Settings", "netlist_verilog"): D,
    ("Yosys.Settings", "merge_libs_to"): W,
    ("CxxRtl", "filename"): D,
    ("SimFlow.Settings", "vcd"): D,
    ("CocotbSettings", "results_xml"): D,
    ("GhdlSim.Settings", "fst"): D,
    ("GhdlSim.Settings", "wave"): D,
    ("GhdlSim.Settings", "write_wave_opt"): D,
    ("GhdlSynth.Settings", "verilog_output"): D,
    ("Nvc.Settings", "wave"): D,
    ("VivadoSim.Settings", "saif"): D,
    ("VivadoSim.Settings", "xelab_log"): W,
    ("VivadoPower.Settings", "power_report_xml"): D,
    ("VivadoImplementation.Settings", "bitstream"): D,
    ("YosysFpga.Settings", "netlist_edif"): D,
    **{
        ("Nextpnr.Settings", name): D
        for name in (
            "textcfg",
            "asc",
            "fasm",
            "write",
            "report",
            "placed_svg",
            "routed_svg",
            "sdf",
            "placement",
        )
    },
    ("Nextpnr.Settings", "log"): W,
    ("FpgaPack.Settings", "bitstream"): D,
}


def _models(annotation) -> list:
    try:
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            return [annotation]
    except TypeError:
        pass
    return [m for arg in typing.get_args(annotation) for m in _models(arg)]


def path_fields():
    """(flow, owner qualname, model, field) of every path field of every flow, nested models
    too."""
    for flow_cls, flow in flow_classes():
        seen: set = set()

        def walk(model):
            if model in seen:
                return
            seen.add(model)
            for field, info in model.model_fields.items():
                if _annotation_contains_path(info.annotation):
                    owner = next(
                        c for c in model.__mro__ if field in getattr(c, "__annotations__", {})
                    )
                    yield flow, owner.__qualname__, model, field
                for nested in _models(info.annotation):
                    yield from walk(nested)

        yield from walk(flow_cls.Settings)


@pytest.mark.parametrize("flow, owner, model, field", list(path_fields()), ids=str)
def test_every_path_field_has_its_declared_role(flow, owner, model, field):
    assert written_role(model, field) == ROLES.get((owner, field)), f"{flow}: {owner}.{field}"


def test_every_role_names_a_path_field():
    found = {(owner, field) for _flow, owner, _model, field in path_fields()}
    assert set(ROLES) <= found, sorted(set(ROLES) - found)


def _dotted_fields(model, prefix: str = "", seen: frozenset = frozenset()):
    """(dotted name, model, field) of every path field of `model`, nested models too."""
    if model in seen:
        return
    for field, info in model.model_fields.items():
        if _annotation_contains_path(info.annotation):
            yield prefix + field, model, field
        for nested in _models(info.annotation):
            yield from _dotted_fields(nested, f"{prefix}{field}.", seen | {model})


#: O8: every working location of every flow, nested ones (`ghdl.outputs_dir`) included
WORKING_CASES = sorted(
    {
        (flow, dotted)
        for cls, flow in flow_classes()
        for dotted, model, field in _dotted_fields(cls.Settings)
        if written_role(model, field) == WORKING_ROLE
    }
)


def _settings_of(flow: str):
    return next(cls for cls, name in flow_classes() if name == flow)


def _nested(dotted: str, value) -> dict:
    head, _, rest = dotted.partition(".")
    return {head: _nested(rest, value) if rest else value}


def test_the_working_locations_are_all_swept():
    assert {dotted.rsplit(".", 1)[-1] for _flow, dotted in WORKING_CASES} >= {
        name for (_owner, name), role in ROLES.items() if role == WORKING_ROLE
    }


@pytest.mark.parametrize("flow, dotted", WORKING_CASES)
@pytest.mark.parametrize("value", ["/abs/build", "$DESIGN_ROOT/build", "../build", "~/build"])
def test_a_working_location_is_a_name(tmp_path, flow, dotted, value):
    cls = _settings_of(flow)
    settings = cls.Settings.from_input(
        {**minimal_settings(cls), **_nested(dotted, value)},
        design_root=tmp_path,
        runner_cwd=tmp_path,
    )
    problems = dict(written_path_problems(settings))
    assert dotted in problems and "inside its run directory" in problems[dotted]


def test_a_deliverable_may_be_a_location_but_not_escape_as_a_name(tmp_path):
    cls = _settings_of("vivado_synth")

    def problems(value):
        s = cls.Settings.from_input(
            {**minimal_settings(cls), "bitstream": value}, design_root=tmp_path, runner_cwd=tmp_path
        )
        return dict(written_path_problems(s))

    assert problems("top.bit") == {} and problems("$PWD/top.bit") == {}
    assert "leaves the run directory" in problems("../top.bit")["bitstream"]


def test_the_forms_of_a_location_are_the_variables_a_setting_expands(tmp_path):
    """One definition (`LOCATION_FORMS`) of how a setting is given a location: a path under each
    variable a path-typed setting expands (`PATH_VARIABLES`), or an absolute path. Each form it
    names is a location: an absolute path once the settings are made."""
    names = re.findall(r"\$(\w+)/\.\.\.", LOCATION_FORMS)
    assert names == list(PATH_VARIABLES) and LOCATION_FORMS.endswith(" or an absolute path")
    cls = _settings_of("vivado_synth")
    for name in names:
        settings = cls.Settings.from_input(
            {**minimal_settings(cls), "bitstream": f"${name}/top.bit"},
            design_root=tmp_path / "design",
            runner_cwd=tmp_path / "start",
        )
        assert Path(settings.bitstream).is_absolute(), name
        assert written_path_problems(settings) == [], name


@pytest.mark.parametrize("value", ["../top.bit", "~/top.bit"])
def test_a_message_on_how_to_give_a_location_names_every_form(tmp_path, value):
    cls = _settings_of("vivado_synth")
    settings = cls.Settings.from_input(
        {**minimal_settings(cls), "bitstream": value}, design_root=tmp_path, runner_cwd=tmp_path
    )
    message = dict(written_path_problems(settings))["bitstream"]
    assert f"a location ({LOCATION_FORMS})" in message, message


def test_a_variable_xeda_does_not_know_is_told_every_one_it_expands(tmp_path):
    cls = _settings_of("vivado_synth")
    settings = cls.Settings.from_input(
        {**minimal_settings(cls), "bitstream": "$NO_SUCH_XEDA_VARIABLE/top.bit"},
        design_root=tmp_path,
        runner_cwd=tmp_path,
    )
    message = dict(written_path_problems(settings))["bitstream"]
    assert "not a variable xeda knows" in message, message
    assert all(f"${name}" in message for name in PATH_VARIABLES), message


def test_cwd_is_no_variable_and_names_pwd(tmp_path):
    cls = _settings_of("vivado_synth")
    s = cls.Settings.from_input(
        {**minimal_settings(cls), "bitstream": "$CWD/top.bit"},
        design_root=tmp_path,
        runner_cwd=tmp_path,
    )
    assert "use $PWD" in dict(written_path_problems(s))["bitstream"]


def test_the_launcher_refuses_a_working_location_before_anything_runs(tmp_path):
    design = Design(name="d", design_root=tmp_path, rtl={"sources": []})
    cls = _settings_of("verilator")
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    with pytest.raises(FlowSettingsError, match="sim_dir"):
        runner.launch_flow(cls, design, {"sim_dir": str(tmp_path / "outside")})
    assert not (tmp_path / "outside").exists()
    assert not (tmp_path / "xeda_run" / "d").exists()


def test_a_written_path_is_not_an_input(tmp_path):
    """`netlist_json = "netlist.json"` names the run's output, not the design root's file of
    that name."""
    (tmp_path / "netlist.json").write_text("{}")
    cls = _settings_of("yosys_fpga")
    settings = cls.Settings.from_input(
        {**minimal_settings(cls), "netlist_json": "netlist.json"},
        design_root=tmp_path,
        runner_cwd=tmp_path,
    )
    assert (tmp_path / "netlist.json").resolve() not in setting_files(settings)
    assert "netlist_json" not in {key for key, _ in setting_path_leaves(settings, written=False)}
    assert "netlist_json" in {key for key, _ in setting_path_leaves(settings, written=True)}


def test_vcs_writes_a_relative_waveform_in_its_run_directory(tmp_path):
    from xeda.flows import Vcs

    (tmp_path / "design").mkdir()
    (tmp_path / "design" / "tb.sv").write_text("module tb; endmodule\n")
    design = Design(
        name="d", design_root=tmp_path / "design", rtl={"sources": ["tb.sv"]}, tb={"top": "tb"}
    )
    flow = Vcs({"fsdb": "dump.fsdb"}, design, tmp_path / "run")
    flow.init()
    assert flow.settings.fsdb == Path("dump.fsdb")


#: names xeda keeps for itself in a run directory
RESERVED = {"settings.json", "results.json", "trace.json", "trace.json.tmp"}


@pytest.mark.parametrize("flow", sorted({name for _cls, name in flow_classes()}))
def test_every_deliverable_has_a_conventional_name_of_its_own(flow):
    """The name a deliverable is written under when it is given a location: fixed per setting
    (never the location's), inside the run directory, no name xeda keeps, none shared."""
    names: dict = {}
    for flow_name, owner, model, field in path_fields():
        if flow_name != flow or written_role(model, field) != DELIVERABLE_ROLE:
            continue
        name = conventional_output(model, field, "d")
        assert name is not None, f"{flow}: {owner}.{field} has no conventional name"
        assert not name.is_absolute() and ".." not in name.parts, f"{owner}.{field}: {name}"
        assert str(name) not in RESERVED, f"{owner}.{field}: {name}"
        assert names.setdefault(name, f"{owner}.{field}") == f"{owner}.{field}", (flow, name)


NESTED_DELIVERABLES = [("ghdl_sim", "cocotb.results_xml"), ("yosys_sim", "cxxrtl.filename")]


@pytest.mark.parametrize("flow, dotted", NESTED_DELIVERABLES)
@pytest.mark.parametrize("given", ["mapping", "instance"])
def test_pwd_in_a_plain_nested_model_is_a_location(tmp_path, flow, dotted, given):
    """`-s cocotb.results_xml=$PWD/r.xml` names a location, as a flow's own setting
    would: expanded, split into a delivery -- and the caller's model instance is not edited."""
    cls = _settings_of(flow)
    head, field = dotted.split(".")
    default = getattr(cls.Settings.from_input(minimal_settings(cls)), head)
    nested = {field: "$PWD/out/r.x"}
    value = nested if given == "mapping" else type(default)(**nested)
    settings = cls.Settings.from_input(
        {**minimal_settings(cls), head: value}, design_root=tmp_path / "d", runner_cwd=tmp_path
    )
    assert getattr(getattr(settings, head), field) == tmp_path / "out" / "r.x"
    assert written_path_problems(settings) == []
    (delivery,) = split_deliveries(settings, "d")
    assert delivery.key == dotted and delivery.destination == tmp_path / "out" / "r.x"
    if given == "instance":
        assert getattr(value, field) == Path("$PWD/out/r.x"), "the caller's model is its own"


def test_a_known_variable_left_unexpanded_is_named_as_such(tmp_path):
    cls = _settings_of("ghdl_sim")
    settings = cls.Settings.from_input(
        minimal_settings(cls), design_root=tmp_path, runner_cwd=tmp_path
    )
    settings.cocotb.results_xml = Path("$PWD/r.xml")  # on the nested model: nothing expands it
    (message,) = [m for key, m in written_path_problems(settings) if key == "cocotb.results_xml"]
    assert "$PWD was not expanded here" in message and "not a variable xeda knows" not in message
