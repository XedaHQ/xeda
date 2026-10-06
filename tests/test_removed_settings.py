"""A removed setting is an error that names what replaced it, for every flow."""

import re

import pytest

from xeda.flow import FlowSettingsError, registered_flows

FLOWS = sorted({cls for _, cls in registered_flows.values()}, key=lambda c: c.name)


@pytest.mark.parametrize("flow", FLOWS, ids=lambda c: c.name)
def test_clean_is_no_longer_a_flow_setting(flow, tmp_path):
    with pytest.raises(FlowSettingsError, match="--clean"):
        flow.Settings.from_input({"clean": True}, design_root=tmp_path, runner_cwd=tmp_path)


def test_verilator_clean_before_run_names_the_option(tmp_path):
    verilator = registered_flows["verilator"][1]
    with pytest.raises(FlowSettingsError, match="--clean"):
        verilator.Settings.from_input(
            {"clean_before_run": True}, design_root=tmp_path, runner_cwd=tmp_path
        )


@pytest.mark.parametrize("flow_name", ["ghdl_sim", "ghdl_synth"])
def test_ghdl_clean_names_both_replacements(flow_name, tmp_path):
    """GHDL's old `clean` meant "run `ghdl remove` before analysis", not "empty the run
    directory": the error must point at `clean_before_analyze` as well as `--clean`, so a user
    who set `clean=False` to keep GHDL's incremental analysis is not steered into the option that
    does the opposite."""
    flow = registered_flows[flow_name][1]
    with pytest.raises(FlowSettingsError, match="clean_before_analyze") as exc_info:
        flow.Settings.from_input({"clean": False}, design_root=tmp_path, runner_cwd=tmp_path)
    assert "--clean" in str(exc_info.value)


@pytest.mark.parametrize("flow", FLOWS, ids=lambda c: c.name)
def test_no_flow_overrides_clean(flow):
    assert "clean" not in vars(flow), f"{flow.name} still defines clean()"


@pytest.mark.parametrize("kind", ["lpf", "pcf", "pdc"])
def test_nextpnr_pin_settings_name_typed_source_replacement(kind, tmp_path):
    flow = registered_flows["nextpnr"][1]
    with pytest.raises(FlowSettingsError) as exc:
        flow.Settings.from_input({f"{kind}_cfg": f"pins.{kind}"}, design_root=tmp_path)
    message = str(exc.value)
    assert "was removed" in message and "rtl.sources" in message
    assert f'type = "{kind.capitalize()}"' in message


# ------------------------------------------------------------------------------ a removed flow

REMOVED_OPEN_XC7 = "`open_xc7` was removed: use fpga_pack to build, openfpgaloader to program"


@pytest.mark.parametrize(
    "name", ["open_xc7", "openxc7", "OpenXC7", "open-xc7", "OPEN_XC7", "OpenXc7", " open_xc7 "]
)
def test_open_xc7_was_removed_and_names_what_replaced_it(name):
    from xeda.flow_runner import FlowNotFoundError, get_flow_class

    with pytest.raises(FlowNotFoundError) as error:
        get_flow_class(name)
    assert str(error.value) == REMOVED_OPEN_XC7


def test_no_open_xc7_flow_module_alias_or_tombstone_is_left():
    import importlib

    import xeda.flows
    from xeda.introspect import flows_info

    assert not [name for name in registered_flows if "xc7" in name.lower()]
    assert not [name for name in dir(xeda.flows) if "xc7" in name.lower()]
    assert not [flow["name"] for flow in flows_info() if "xc7" in flow["name"]]
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("xeda.flows.openxc7")


def test_launching_open_xc7_through_the_api_says_it_was_removed(tmp_path):
    from xeda import Design
    from xeda.flow_runner import DefaultRunner, FlowNotFoundError

    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "d"})
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    for launch in (runner.run, runner.plan):
        with pytest.raises(
            FlowNotFoundError, match="fpga_pack to build, openfpgaloader to program"
        ):
            launch("open_xc7", design)
    assert not (tmp_path / "run").exists()


# ------------------------------------------------- a removed flow's section in a `flows` table

_COUNTER = "module d(input wire clk, output reg q);\n  always @(posedge clk) q <= ~q;\nendmodule\n"


def _design_with_sections(tmp_path, sections):
    from xeda import Design

    (tmp_path / "d.v").write_text(_COUNTER)
    sections = {"yosys_fpga": {"fpga": {"part": "xc7a100tcsg324-1"}}, **sections}
    return Design(
        name="d", design_root=tmp_path, rtl={"sources": ["d.v"], "top": "d"}, flows=sections
    )


@pytest.mark.parametrize("name", ["open_xc7", "openxc7", "OpenXC7", "open-xc7"])
def test_a_design_s_open_xc7_section_is_an_error_naming_the_replacement(name, tmp_path):
    """A removed flow is not an unknown plugin: its section configures nothing any more, and
    saying nothing would leave the design's settings silently unused."""
    from xeda.flow_runner import DefaultRunner, FlowNotFoundError

    design = _design_with_sections(tmp_path, {name: {"fpga": {"part": "xc7a100tcsg324-1"}}})
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    for launch in (runner.run, runner.plan):
        with pytest.raises(FlowNotFoundError) as error:
            launch("yosys_fpga", design)
        assert str(error.value) == REMOVED_OPEN_XC7
    assert not (tmp_path / "run").exists()


def test_a_project_s_open_xc7_section_is_an_error_naming_the_replacement(tmp_path):
    from xeda.flow_runner import DefaultRunner, FlowNotFoundError

    (tmp_path / "d.v").write_text(_COUNTER)
    project = tmp_path / "xedaproject.yaml"
    project.write_text(
        "designs:\n"
        "  - name: d\n"
        "    rtl: {sources: [d.v], top: d}\n"
        "flows:\n"
        "  yosys_fpga:\n"
        "    fpga: {part: xc7a100tcsg324-1}\n"
        "  openxc7:\n"
        "    fpga: {part: xc7a100tcsg324-1}\n"
    )
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    with pytest.raises(FlowNotFoundError) as error:
        runner.run("yosys_fpga", xedaproject=str(project), select_design_in_project="d")
    assert str(error.value) == REMOVED_OPEN_XC7
    assert not (tmp_path / "run").exists()


def test_an_open_xc7_section_given_as_a_setting_is_an_error_naming_the_replacement(tmp_path):
    from xeda.flow_runner import DefaultRunner, FlowNotFoundError

    design = _design_with_sections(tmp_path, {})
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    with pytest.raises(FlowNotFoundError) as error:
        runner.run("yosys_fpga", design, flow_settings=["flows.open_xc7.seed=1"])
    assert str(error.value) == REMOVED_OPEN_XC7
    with pytest.raises(FlowNotFoundError) as error:
        runner.run_flow("yosys_fpga", design, all_flows_settings={"open_xc7": {"seed": 1}})
    assert str(error.value) == REMOVED_OPEN_XC7


def test_the_command_line_reports_a_design_file_s_open_xc7_section(tmp_path):
    import json
    import subprocess
    import sys

    (tmp_path / "d.v").write_text(_COUNTER)
    design_file = tmp_path / "d.yaml"
    design_file.write_text(
        "name: d\nrtl: {sources: [d.v], top: d}\nflows:\n"
        "  yosys_fpga: {fpga: {part: xc7a100tcsg324-1}}\n  open_xc7: {seed: 1}\n"
    )
    for extra in ([], ["--dry-run"]):
        proc = subprocess.run(
            [sys.executable, "-m", "xeda", "run", "yosys_fpga", str(design_file), "--json"]
            + extra
            + ["--run-root", str(tmp_path / "run")],
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 0
        error = json.loads(proc.stdout)["error"]
        assert error["type"] == "FlowRemovedError" and REMOVED_OPEN_XC7 in error["message"]


def test_a_section_of_a_flow_that_is_not_installed_is_still_tolerated(tmp_path):
    """A plugin flow that is not installed here: its section is nobody's settings."""
    from xeda.flow_runner import DefaultRunner

    design = _design_with_sections(tmp_path, {"some_plugin_flow": {"anything": 1}})
    plan = DefaultRunner(tmp_path / "run", display_results=False).plan("yosys_fpga", design)
    assert plan is not None


#: A `flows` entry for the removed flow, in any of the formats an example may be written in:
#: the name in any spelling it had, optionally quoted, then what ends a key, a table header or a
#: dotted path (`open_xc7:`, `"open-xc7": {`, `[flows.open_xc7]`, `flows.OpenXC7.seed = 1`).
OPEN_XC7_ENTRY = re.compile(r"""open[_-]?xc7["']?\s*[:=\].]""", re.IGNORECASE)


@pytest.mark.parametrize(
    "text, found",
    [
        ("[flows.open_xc7]\nseed = 1\n", True),
        ('[flows."open-xc7"]\n', True),
        ("flows:\n  open_xc7:\n    seed: 1\n", True),
        ("flows:\n  'openxc7': {seed: 1}\n", True),
        ('{"flows": {"OpenXC7": {"seed": 1}}}', True),
        ("flows.open_xc7.seed = 1\n", True),
        ("open_xc7 = {}\n", True),
        ("[flows.nextpnr]\nfpga.part = 'xc7a100tcsg324-1'\n", False),
        ("flows:\n  nextpnr:\n    fpga: {part: xc7a35tcsg324-1}\n", False),
        ("# the open_xc7 flow was removed\n", False),
    ],
)
def test_the_open_xc7_pattern_sees_every_spelling_and_spares_the_rest(text, found):
    assert bool(OPEN_XC7_ENTRY.search(text)) is found


def test_no_example_keeps_an_open_xc7_section():
    from pathlib import Path

    examples = Path(__file__).parent.parent / "examples"
    kept = [
        str(path)
        for suffix in ("toml", "yaml", "yml", "json")
        for path in examples.rglob(f"*.{suffix}")
        if OPEN_XC7_ENTRY.search(path.read_text())
    ]
    assert not kept


@pytest.mark.parametrize("name", ["open_xc7", "OpenXC7", "openxc7"])
def test_scrub_still_removes_a_removed_flow_s_run_directories(name, tmp_path):
    """The flow is gone, the directories its runs left are not: `xeda scrub` only removes
    directories, so it takes a removed flow's name, in any spelling the flow had."""
    import json
    import subprocess
    import sys

    from xeda.run_root import ensure_run_root

    root = ensure_run_root(tmp_path / "run")
    for directory in ("open_xc7", "open_xc7_0123456789abcdef", "nextpnr"):
        (root / "blinky" / directory).mkdir(parents=True)
        (root / "blinky" / directory / "results.json").write_text("{}\n")
    proc = subprocess.run(
        [sys.executable, "-m", "xeda", "scrub", name, "blinky", "--run-root", str(root), "--json"],
        capture_output=True,
        text=True,
        input="yes\n",
    )
    assert proc.returncode == 0, proc.stderr
    document = json.loads(proc.stdout)
    assert document["success"] is True and document["flow"] == "open_xc7"
    assert sorted(p.name for p in (root / "blinky").iterdir() if p.is_dir()) == ["nextpnr"]


def test_a_removed_flow_named_on_the_command_line_as_a_section_says_so(tmp_path):
    import json
    import subprocess
    import sys

    (tmp_path / "d.v").write_text(_COUNTER)
    design_file = tmp_path / "d.yaml"
    design_file.write_text("name: d\nrtl: {sources: [d.v], top: d}\n")
    for spelling in ("open_xc7", "openxc7", "OpenXC7"):
        proc = subprocess.run(
            [sys.executable, "-m", "xeda", "run", "yosys_fpga", str(design_file), "--json"]
            + ["--dry-run", "-s", "fpga.part=xc7a100tcsg324-1", f"flows.{spelling}.seed=1"],
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 0
        error = json.loads(proc.stdout)["error"]
        assert error["type"] == "FlowRemovedError" and REMOVED_OPEN_XC7 in error["message"]


# ------------------------------------- openroad's settings that configured yosys (R-PC-b)

#: what `openroad` had only to hand to its `yosys` dependency, now yosys's own settings
MOVED_TO_YOSYS = {"optimize": "speed", "abc_driver_cell": "BUF_X4", "abc_load_in_ff": 2.5}


@pytest.mark.parametrize("origin", ["design", "project", "cli", "flows-cli", "api"])
@pytest.mark.parametrize("name", MOVED_TO_YOSYS)
def test_openroad_s_yosys_settings_moved_and_name_where(tmp_path, monkeypatch, name, origin):
    """`optimize`, `abc_driver_cell` and `abc_load_in_ff` act on yosys's mapping, so they are
    yosys's settings: given to `openroad` from any origin, each is an error naming
    `flows.yosys.<name>`."""
    from xeda import Design
    from xeda.flow_runner import DefaultRunner

    monkeypatch.chdir(tmp_path)
    (tmp_path / "mac.v").write_text("module mac(input clk); endmodule\n")
    value = MOVED_TO_YOSYS[name]
    sections = {"openroad": {name: value}} if origin == "design" else {}
    design = Design(
        name="mac",
        design_root=tmp_path,
        rtl={"sources": ["mac.v"], "top": "mac", "clock": {"port": "clk"}},
        flows=sections,
    )
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    base = ["platform=nangate45", "clock.period=2.0"]
    message = f"`{name}` was removed: use `flows.yosys.{name}`"
    with pytest.raises(Exception, match=re.escape(message)):
        if origin == "project":
            project = tmp_path / "project.yaml"
            project.write_text(f"flows:\n  openroad:\n    {name}: {value}\n")
            runner.plan("openroad", design, xedaproject=str(project), flow_settings=base)
        elif origin == "cli":
            runner.plan("openroad", design, flow_settings=[*base, f"{name}={value}"])
        elif origin == "flows-cli":
            runner.plan("openroad", design, flow_settings=[*base, f"flows.openroad.{name}={value}"])
        elif origin == "api":
            openroad = registered_flows["openroad"][1]
            openroad.Settings.from_input({name: value}, design_root=tmp_path)
        else:
            runner.plan("openroad", design, flow_settings=base)
    assert not (tmp_path / "run").exists() or origin == "api"


def test_a_moved_setting_reaches_openroad_s_synthesis_from_a_file_section(tmp_path, monkeypatch):
    """Where the tombstone sends a user: a design's `flows.yosys` section reaches the yosys run
    `openroad` launches. (`-s flows.yosys.*` with `xeda run openroad` is refused until openroad
    declares its input, PC Task 6: its synthesis is not yet a flow of the run the command line
    can see -- `test_flows_yosys_on_openroad_s_command_line_waits_for_its_declared_input`.)"""
    import contextlib

    from xeda import Design
    from xeda.flow import FlowException
    from xeda.flow_runner import DefaultRunner

    def record(executable, args=None, **kwargs):
        return "" if kwargs.get("stdout") is True else None

    monkeypatch.setattr("xeda.tool.run_process", record)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "mac.v").write_text("module mac(input clk); endmodule\n")
    design = Design(
        name="mac",
        design_root=tmp_path,
        rtl={"sources": ["mac.v"], "top": "mac", "clock": {"port": "clk"}},
        flows={"yosys": {"optimize": "speed"}},
    )
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    with contextlib.suppress(FlowException):
        runner.run("openroad", design, flow_settings=["platform=nangate45", "clock.period=2.0"])
    script = (tmp_path / "run" / "mac" / "yosys" / "yosys_synth.ys").read_text()
    assert "&if,-g,-K,6" in script


def test_flows_yosys_on_openroad_s_command_line_waits_for_its_declared_input(tmp_path):
    """Tripwire for PC Task 6: once `openroad` declares its `netlist` input from `yosys`,
    `-s flows.yosys.optimize=...` is a setting of a flow of the run and this must be inverted."""
    from xeda import Design
    from xeda.flow_runner import DefaultRunner

    (tmp_path / "mac.v").write_text("module mac(input clk); endmodule\n")
    design = Design(name="mac", design_root=tmp_path, rtl={"sources": ["mac.v"], "top": "mac"})
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    with pytest.raises(FlowSettingsError, match="names no flow of this run"):
        runner.plan(
            "openroad",
            design,
            flow_settings=["platform=nangate45", "flows.yosys.optimize=speed"],
        )
