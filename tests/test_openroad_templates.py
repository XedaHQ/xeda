"""The scripts of the OpenROAD flows: the restart header, the procedures and the one-owner rule.

Every OpenROAD process starts with the restart header. The tests render it for the `tiny`
platform through two small flows over the shared base (`openroad_utils.probe_flows`), and run the
scripts under `tclsh` with the fake OpenROAD.
"""

import re
from pathlib import Path

import pytest

from xeda.flows.openroad import base
from xeda.flows.openroad.base import WireRcSettings, effective_base, merge_wire_rc, tcl_number
from xeda.platforms.model import WireRc
from xeda.utils import tcl_word

from .openroad_utils import probe_flows, render_driver, run_fake_openroad
from .tool_utils import fake_calls, require_tclsh

TEMPLATES = Path(base.__file__).parent / "templates"
HEADER_COMMAND = re.compile(
    r"^(read_liberty|read_lef|read_verilog|link_design|read_db|read_sdc|set_layer_rc|set_wire_rc"
    r"|set_dont_use)\b"
)

#: The wire RC of the `tiny` platform, as its residue file gives it.
TINY_LAYERS = {
    "m1": (0.0085, 0.00015),
    "m2": (0.0025, 0.00014),
    "m3": (0.0015, 0.00012),
    "m4": (0.0003, 0.0001),
}
TINY_VIAS = {"v1": 0.002, "v2": 0.0004, "v3": 0.0002}


def header(script: str) -> list[str]:
    """The lines of the restart header in a rendered driver: the commands that it holds."""
    return [line for line in script.splitlines() if HEADER_COMMAND.match(line)]


def wire_rc_lines(layers=TINY_LAYERS, vias=TINY_VIAS, signal="m2", clock="m3") -> list[str]:
    return [
        *(
            f"set_layer_rc -layer {tcl_word(name)} -resistance {r!r} -capacitance {c!r}"
            for name, (r, c) in layers.items()
        ),
        *(f"set_layer_rc -via {tcl_word(name)} -resistance {r!r}" for name, r in vias.items()),
        f"set_wire_rc -signal -layer {tcl_word(signal)}",
        f"set_wire_rc -clock -layer {tcl_word(clock)}",
    ]


# ------------------------------------------------------------------------------ the header


def test_the_header_of_a_netlist_start(tmp_path):
    with probe_flows() as probes:
        flow = probes.flow(tmp_path)
        files = flow.platform_model.files
        liberty, tech, cells = files["liberty"], files["tech_lef"], files["cell_lef"]
        netlist = flow.inputs.netlist
        assert header(render_driver(flow, "openroad_probe")) == [
            *(f"read_liberty {tcl_word(path)}" for path in liberty),
            f"read_lef {tcl_word(tech[0])}",
            *(f"read_lef {tcl_word(path)}" for path in cells),
            f"read_verilog {tcl_word(netlist)}",
            'link_design -omit_filename_prop "counter"',
            *wire_rc_lines(),
            'set_dont_use [list "INV_X4" "NAND2_X4"]',
        ]


def test_the_header_of_a_database_start(tmp_path):
    with probe_flows() as probes:
        flow = probes.flow(tmp_path, database=True)
        liberty = flow.platform_model.files["liberty"]
        assert header(render_driver(flow, "openroad_probe_db")) == [
            *(f"read_liberty {tcl_word(path)}" for path in liberty),
            f"read_db {tcl_word(flow.inputs.odb)}",
            f"read_sdc {tcl_word(flow.inputs.sdc)}",
            *wire_rc_lines(),
            'set_dont_use [list "INV_X4" "NAND2_X4"]',
        ]


def test_the_hierarchy_reaches_both_forms(tmp_path):
    with probe_flows() as probes:
        flow = probes.flow(tmp_path, settings={"hierarchical": True})
        lines = header(render_driver(flow, "openroad_probe"))
        assert 'link_design -hier -omit_filename_prop "counter"' in lines
        flat = probes.flow(tmp_path / "flat")
        assert 'link_design -omit_filename_prop "counter"' in header(
            render_driver(flat, "openroad_probe")
        )
        flow = probes.flow(tmp_path / "db", database=True, settings={"hierarchical": True})
        assert f"read_db -hier {tcl_word(flow.inputs.odb)}" in header(
            render_driver(flow, "openroad_probe_db")
        )
        flat = probes.flow(tmp_path / "flatdb", database=True)
        assert f"read_db {tcl_word(flat.inputs.odb)}" in header(
            render_driver(flat, "openroad_probe_db")
        )


def test_the_wire_rc_setting_changes_only_what_it_gives(tmp_path):
    with probe_flows() as probes:
        flow = probes.flow(
            tmp_path,
            settings={
                "wire_rc": {
                    "signal_layer": "m3",
                    "layers": {"m2": [0.5, 0.25], "m5": [0.1, 0.2]},
                    "vias": {"v2": 0.75},
                }
            },
        )
        lines = header(render_driver(flow, "openroad_probe"))
    layers = {**TINY_LAYERS, "m2": (0.5, 0.25), "m5": (0.1, 0.2)}
    vias = {**TINY_VIAS, "v2": 0.75}
    assert [line for line in lines if line.startswith("set_")][:-1] == wire_rc_lines(
        layers, vias, signal="m3", clock="m3"
    )


def test_a_platform_without_wire_rc_layers_writes_no_layer_commands(tmp_path):
    with probe_flows() as probes:
        flow = probes.flow(tmp_path)
        flow.platform_model = flow.platform_model.model_copy(update={"wire_rc": WireRc()})
        lines = header(render_driver(flow, "openroad_probe"))
    assert not [line for line in lines if line.startswith(("set_layer_rc", "set_wire_rc"))]


def test_the_cells_not_to_use_are_one_list_without_repeats(tmp_path):
    with probe_flows() as probes:
        flow = probes.flow(tmp_path, settings={"dont_use_cells": ["NAND2_X4", "OTHER_X1"]})
        lines = header(render_driver(flow, "openroad_probe"))
    assert lines[-1] == 'set_dont_use [list "INV_X4" "NAND2_X4" "OTHER_X1"]'
    assert sum(line.startswith("set_dont_use") for line in lines) == 1


def test_no_cells_not_to_use_write_no_command(tmp_path):
    with probe_flows() as probes:
        flow = probes.flow(tmp_path)
        flow.platform_model = flow.platform_model.model_copy(update={"dont_use_cells": []})
        lines = header(render_driver(flow, "openroad_probe"))
    assert not [line for line in lines if line.startswith("set_dont_use")]


def test_a_driver_must_say_how_the_process_starts(tmp_path):
    from jinja2 import UndefinedError

    with probe_flows() as probes:
        flow = probes.flow(tmp_path)
        flow.jinja_env.globals["settings"] = flow.settings
        template = flow.jinja_env.from_string(
            '{% set restart = "elsewhere" %}{% set sdc_files = [] %}{% include "restart.tcl.j2" %}'
        )
        flow.bind_template_values()
        with pytest.raises(UndefinedError, match="restart_must_be_netlist_or_database"):
            template.render(settings=flow.settings, design=flow.design)
        template = flow.jinja_env.from_string('{% include "restart.tcl.j2" %}')
        with pytest.raises(UndefinedError, match="restart"):
            template.render(settings=flow.settings, design=flow.design)


def test_the_templates_get_the_values_the_flow_binds(tmp_path):
    with probe_flows() as probes:
        flow = probes.flow(tmp_path)
        flow.bind_template_values()
        names = flow.jinja_env.globals
        for name in ("render_flags", "P", "inputs", "outputs", "reports", "failed_odb", "dont_use"):
            assert name in names, name
        assert names["failed_odb"] == f"{flow.name}-failed.odb"
        assert names["dont_use"] == ["INV_X4", "NAND2_X4"]
        assert names["P"].dont_use == ("INV_X4", "NAND2_X4")
        assert names["inputs"] is flow.inputs and names["outputs"] is flow.outputs
        assert "tcl_number" in flow.jinja_env.filters


# ------------------------------------------------------------------------------ effective values


def test_the_effective_values_are_a_pure_function_of_the_settings_and_the_model(tmp_path):
    with probe_flows() as probes:
        flow = probes.flow(
            tmp_path, settings={"dont_use_cells": ["A", "INV_X4"], "wire_rc": {"clock_layer": "m4"}}
        )
        model = flow.platform_model
        values = effective_base(flow.settings, model)
    assert values == flow.effective()
    assert values.liberty == tuple(model.files["liberty"])
    assert values.tech_lef == model.files["tech_lef"][0]
    assert values.cell_lefs == tuple(model.files["cell_lef"])
    assert values.dont_use == ("INV_X4", "NAND2_X4", "A")
    assert values.time_unit == "1ns"
    assert (values.wire_rc.signal_layer, values.wire_rc.clock_layer) == ("m2", "m4")
    assert values.wire_rc.layers == model.wire_rc.layers


def test_a_platform_that_names_no_time_unit_has_the_unit_of_the_format(tmp_path):
    with probe_flows() as probes:
        flow = probes.flow(tmp_path)
        model = flow.platform_model.model_copy(update={"time_unit": None})
        assert effective_base(flow.settings, model).time_unit == "1ns"


def test_the_wire_rc_merges_leaf_by_leaf():
    platform = WireRc(signal_layer="a", clock_layer="b", layers={"a": (1.0, 2.0)}, vias={"v": 3.0})
    assert merge_wire_rc(platform, None) is platform
    given = WireRcSettings(clock_layer="c", layers={"d": (4.0, 5.0)})
    merged = merge_wire_rc(platform, given)
    assert merged == WireRc(
        signal_layer="a",
        clock_layer="c",
        layers={"a": (1.0, 2.0), "d": (4.0, 5.0)},
        vias={"v": 3.0},
    )
    assert platform.layers == {"a": (1.0, 2.0)} and given.signal_layer is None  # not changed


@pytest.mark.parametrize(
    "value, text", [(1, "1"), (0.5, "0.5"), (3.75e-05, "3.75e-05"), (-2.0, "-2.0")]
)
def test_a_number_is_written_from_the_number(value, text):
    assert tcl_number(value) == text


@pytest.mark.parametrize("value", ["1.0", "[exit]", None, True, float("nan"), float("inf")])
def test_text_is_never_written_as_a_number(value):
    with pytest.raises(TypeError):
        tcl_number(value)


# ------------------------------------------------------------------------------ the procedures


def run_procs(tmp_path: Path, body: str, **kwargs):
    """Run `body` under the fake OpenROAD after the procedures; return the process."""
    with probe_flows() as probes:
        flow = probes.flow(tmp_path / "flow")
        flow.bind_template_values()
        text = flow.jinja_env.get_template("procs.tcl.j2").render()
    return run_fake_openroad(tmp_path / "work", f"{text}\n{body}\n", **kwargs)


def test_the_four_procedures_are_defined(tmp_path):
    require_tclsh()
    done = run_procs(
        tmp_path,
        "foreach p {xeda_stage xeda_metrics xeda_tie_pin xeda_physical_only_masters} "
        '{ puts "proc:$p:[llength [info procs $p]]" }',
    )
    assert done.returncode == 0, done.stderr
    for name in ("xeda_stage", "xeda_metrics", "xeda_tie_pin", "xeda_physical_only_masters"):
        assert f"proc:{name}:1" in done.stdout


def test_a_stage_sets_its_own_metrics_stage(tmp_path):
    require_tclsh()
    done = run_procs(tmp_path, "xeda_stage floorplan\nxeda_stage detailedplace")
    assert done.returncode == 0, done.stderr
    calls = fake_calls(tmp_path / "work")
    assert calls == [
        ["utl::set_metrics_stage", "floorplan__{}"],
        ["utl::set_metrics_stage", "detailedplace__{}"],
    ]
    assert "xeda: stage floorplan" in done.stdout


@pytest.mark.parametrize(
    "erc, skew, expected",
    [
        (0, 0, []),
        (1, 0, ["report_erc_metrics"]),
        (0, 1, ["report_clock_skew_metric", "report_clock_skew_metric -hold"]),
        (
            1,
            1,
            ["report_clock_skew_metric", "report_clock_skew_metric -hold", "report_erc_metrics"],
        ),
    ],
)
def test_the_metrics_of_a_report_depend_on_what_the_stage_has(tmp_path, erc, skew, expected):
    require_tclsh()
    done = run_procs(tmp_path, f"xeda_metrics {erc} {skew}")
    assert done.returncode == 0, done.stderr
    calls = [" ".join(call) for call in fake_calls(tmp_path / "work")]
    always = [
        "report_tns_metric",
        "report_tns_metric -hold",
        "report_worst_slack_metric",
        "report_worst_slack_metric -hold",
        "report_fmax_metric",
    ]
    assert calls == [
        *always,
        *[call for call in expected if "skew" in call],
        *[call for call in expected if "erc" in call],
        "report_power_metric",
        "report_design_area_metrics",
    ]


def test_a_tie_pin_names_the_library_cell_and_port(tmp_path):
    require_tclsh()
    done = run_procs(tmp_path, "puts [xeda_tie_pin TIELO_X1 Z]")
    assert done.returncode == 0, done.stderr
    assert "fake_name/TIELO_X1/Z" in done.stdout  # the fake names the library `fake_name`
    assert [call[0] for call in fake_calls(tmp_path / "work")] == [
        "get_lib_cell",
        "get_property",
        "get_name",
    ]


@pytest.mark.parametrize(
    "master_type, found",
    [("ENDCAP_PRE", "FAKE_MASTER"), ("CORE_SPACER", "FAKE_MASTER"), ("CORE", "")],
)
def test_the_physical_only_masters_are_found_by_their_type(
    tmp_path, monkeypatch, master_type, found
):
    require_tclsh()
    monkeypatch.setenv(
        "XEDA_FAKE_TOOL_RETURNS",
        f"getLibs ::fake_lib getMasters ::fake_master getType {master_type}",
    )
    # `getLibs` gives one object, `getMasters` of it gives one more: both name the same fake
    monkeypatch.setenv(
        "XEDA_FAKE_TOOL_RETURNS",
        f"getLibs ::fake_lib getMasters ::fake_master getType {master_type}",
    )
    done = run_procs(tmp_path, 'puts "masters:[xeda_physical_only_masters]:"')
    assert done.returncode == 0, done.stderr
    assert f"masters:{found}:" in done.stdout


def test_the_header_and_the_procedures_run_under_the_fake(tmp_path):
    require_tclsh()
    with probe_flows() as probes:
        flow = probes.flow(tmp_path / "flow")
        flow.outputs.odb = Path("probe.odb")  # the fake writes only in its working directory
        script = render_driver(flow, "openroad_probe")
    done = run_fake_openroad(tmp_path / "work", script)
    assert done.returncode == 0, done.stderr
    names = [call[0] for call in fake_calls(tmp_path / "work")]
    assert names[:2] == ["read_liberty", "read_lef"]
    assert names.count("set_layer_rc") == 7 and names.count("set_wire_rc") == 2
    assert names[-1] == "write_db"
    assert names.index("link_design") < names.index("set_layer_rc") < names.index("set_dont_use")


# ------------------------------------------------------------------------------ one owner


#: The commands whose state the database stores. `openroad_place` sets them at the floorplan, and
#: no later process sets them again, since the database carries them to every later stage.
OWNED_BY_PLACE = (
    "set_routing_layers",
    "set_global_routing_layer_adjustment",
    "set_opt_config",
    "set_global_sizing_config",
    "set_dont_touch",
)
INCLUDE = re.compile(r"\{%-?\s*include\s+(?P<name>[^%]*?)\s*-?%\}")
LITERAL = re.compile(r'^"(?P<name>[^"{}]+)"$')


def included(root: Path, template: Path) -> set[Path]:
    """The templates that `template` includes, and the ones they include, as literal names."""
    found: set[Path] = set()
    pending = [template]
    while pending:
        current = pending.pop()
        for match in INCLUDE.finditer(current.read_text()):
            literal = LITERAL.match(match["name"])
            assert literal, f"{current.name}: the include {match['name']} is no literal name"
            target = root / literal["name"]
            assert target.is_file(), f"{current.name} includes {literal['name']}, which is absent"
            if target not in found:
                found.add(target)
                pending.append(target)
    return found


def owned_commands(root: Path) -> dict[str, list[str]]:
    """The commands of `OWNED_BY_PLACE` that a driver of another flow than `openroad_place`
    writes, by driver, through the templates that it includes. Comments are not commands."""
    problems: dict[str, list[str]] = {}
    for driver in sorted((root / "drivers").glob("*.tcl.j2")):
        if driver.name.startswith("openroad_place"):
            continue
        for template in (driver, *sorted(included(root, driver))):
            for number, line in enumerate(template.read_text().splitlines(), 1):
                words = line.split("#", 1)[0] if line.lstrip().startswith("#") else line
                for command in OWNED_BY_PLACE:
                    if re.search(rf"(?<![\w:]){command}\b", words):
                        problems.setdefault(driver.name, []).append(
                            f"{template.relative_to(root)}:{number}: {command}"
                        )
    return problems


def test_no_later_flow_sets_the_state_that_the_database_stores():
    """The database stores the routing layers, the layer adjustments, the optimization options and
    the cells that must not be touched. `openroad_place` sets them, and the restart header re-applies
    only what the database does not store."""
    assert owned_commands(TEMPLATES) == {}
    for name in ("restart.tcl.j2", "procs.tcl.j2"):
        text = (TEMPLATES / name).read_text()
        assert not [c for c in OWNED_BY_PLACE if re.search(rf"(?<![\w:]){c}\b", text)], name


def test_the_scan_finds_a_command_in_a_stage_that_a_driver_includes(tmp_path):
    (tmp_path / "drivers").mkdir()
    (tmp_path / "stages").mkdir()
    (tmp_path / "drivers" / "openroad_cts.tcl.j2").write_text(
        '{% include "restart.tcl.j2" %}\n{% include "stages/cts.tcl.j2" %}\n'
    )
    (tmp_path / "drivers" / "openroad_place.tcl.j2").write_text(
        '{% include "stages/floor.tcl.j2" %}'
    )
    (tmp_path / "restart.tcl.j2").write_text("# set_dont_touch is named in a comment\n")
    (tmp_path / "stages" / "cts.tcl.j2").write_text("clock_tree_synthesis\nset_dont_touch {a}\n")
    (tmp_path / "stages" / "floor.tcl.j2").write_text("set_routing_layers -signal m2-m4\n")
    assert owned_commands(tmp_path) == {
        "openroad_cts.tcl.j2": ["stages/cts.tcl.j2:2: set_dont_touch"]
    }


def test_a_driver_lists_its_stages_as_literal_includes(tmp_path):
    (tmp_path / "drivers").mkdir()
    (tmp_path / "drivers" / "openroad_cts.tcl.j2").write_text(
        "{% for stage in stages %}{% include stage %}{% endfor %}"
    )
    with pytest.raises(AssertionError, match="no literal name"):
        owned_commands(tmp_path)


def test_the_restart_header_quotes_with_the_tcl_filters():
    """A path or a name in the header reaches Tcl through `tcl_word` or `tcl_list`, and a number
    through `tcl_number`."""
    text = (TEMPLATES / "restart.tcl.j2").read_text()
    for expression in re.findall(r"\{\{\s*(.*?)\s*\}\}", text):
        if expression.startswith("restart_must_be"):
            continue
        assert re.search(r"\|\s*(tcl_word|tcl_list|tcl_number)\b", expression), expression
