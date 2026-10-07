"""`yosys_fpga` and `nextpnr` declare their files: nextpnr's netlist is `yosys_fpga`'s recorded
output, or a `JsonNetlist` among the design's sources, and nextpnr reads nothing else of
yosys_fpga's -- not its settings, not its run directory. The tools are stand-ins that write
what the real ones would; the real-tool tests in `tests/test_nextpnr.py` run the same path."""

import ast
import contextlib
import inspect
import os
import sys
import textwrap
from pathlib import Path

import pytest

from xeda import Design
from xeda.flow import FlowSettingsError
from xeda.flow.io import declared_inputs, declared_outputs
from xeda.flow_runner import DefaultRunner
from xeda.flows import FpgaPack, Nextpnr, Openfpgaloader, YosysFpga
from xeda.flows.nextpnr import NextpnrTool

from .project_files import PROJECT_FILE
from .settings_samples import flow_classes, minimal_settings
from .tool_utils import producers_of

PART = "LFE5U-25F-6BG381C"
OTHER_PART = "LFE5U-85F-6BG381C"
BLINK = "module blink(input clk, output reg q); always @(posedge clk) q <= ~q; endmodule\n"
NETLIST = '{"creator": "a stand-in yosys", "modules": {}}\n'
REPORT = '{"fmax": {}, "utilization": {}, "critical_paths": []}\n'

#: the files opened while a flow of interest ran (`_audit`); None outside `_watching`
_OPENED: list[str] | None = None


def _audit(event: str, args: tuple) -> None:
    if _OPENED is not None and event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        _OPENED.append(os.fsdecode(args[0]))


sys.addaudithook(_audit)


@contextlib.contextmanager
def _watching(opened: list[str]):
    global _OPENED
    _OPENED = opened
    try:
        yield
    finally:
        _OPENED = None


@pytest.fixture
def tools(monkeypatch):
    """Stand-ins for yosys and nextpnr: yosys_fpga writes its netlist and a decoy beside it;
    nextpnr writes its report and configuration where its arguments say. Every nextpnr
    command's arguments are returned."""
    calls: list[list[str]] = []

    def yosys_run(self):
        netlist = self.run_path / self.settings.netlist_json
        netlist.write_text(NETLIST)
        (self.run_path / "decoy.json").write_text(NETLIST)
        self.outputs.netlist = netlist

    def nextpnr_run(tool, *args, env=None):
        calls.append([str(arg) for arg in args])
        for arg in map(str, args):
            name, _, value = arg.partition("=")
            if name in ("--report", "--textcfg", "--asc", "--fasm"):
                Path(value).write_text(REPORT if name == "--report" else "config\n")

    monkeypatch.setattr(YosysFpga, "run", yosys_run)
    monkeypatch.setattr(YosysFpga, "parse_reports", lambda self: True)
    monkeypatch.setattr(NextpnrTool, "run", nextpnr_run)
    return calls


def _design(root: Path, sources=("blink.v",), flows=None) -> Design:
    root.mkdir(exist_ok=True)
    (root / "blink.v").write_text(BLINK)
    return Design(
        name="blink",
        design_root=root,
        rtl={"sources": list(sources), "top": "blink"},
        flow=flows or {},
    )


def _runner(tmp_path: Path, monkeypatch) -> DefaultRunner:
    monkeypatch.chdir(tmp_path)
    return DefaultRunner(tmp_path / "xeda_run", display_results=False)


DECLARED = [YosysFpga, Nextpnr, FpgaPack, Openfpgaloader]


def test_the_declared_flows():
    """The FPGA graph, the Vivado synthesis flows, `yosys`, whose gate-level netlist
    is a declared output, and `openroad`, which consumes it."""
    assert {
        cls.name for cls, _ in flow_classes() if declared_inputs(cls) or declared_outputs(cls)
    } == {
        "fpga_pack",
        "nextpnr",
        "openfpgaloader",
        "openroad",
        "vivado_alt_synth",
        "vivado_impl",
        "vivado_synth",
        "vivado_postsynth_sim",
        "vivado_power",
        "yosys",
        "yosys_fpga",
    }


@pytest.mark.parametrize("flow_class", DECLARED, ids=lambda cls: cls.name)
def test_a_declared_flow_reads_another_flow_only_through_its_inputs(flow_class):
    """Plan 2's oracle 3, for every declared flow: no reading of another flow's settings, run
    directory or artifacts by way of the flows it depends on."""
    from xeda.flow import Flow

    tree = ast.parse(
        "\n".join(
            textwrap.dedent(inspect.getsource(cls))
            for cls in flow_class.__mro__
            if issubclass(cls, Flow) and cls is not Flow
        )
    )
    forbidden = {"completed_dependencies", "pop_dependency", "dependencies", "add_dependency"}
    used = sorted(
        {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)} & forbidden
    )
    assert not used, f"{flow_class.name} uses {used}"


@pytest.mark.parametrize("flow_class", DECLARED, ids=lambda cls: cls.name)
def test_every_declared_flow_resolves_with_its_default_producers(flow_class, tmp_path):
    plan = DefaultRunner(tmp_path / "xeda_run").resolve(
        flow_class, _design(tmp_path / "d"), minimal_settings(flow_class)
    )
    producers = {d.producer for d in declared_inputs(flow_class).values() if d.producer}
    assert producers <= {node.name for node in plan.nodes}


def test_nextpnr_places_the_netlist_yosys_fpga_hands_over(tmp_path, monkeypatch, tools):
    runner = _runner(tmp_path, monkeypatch)
    nextpnr = runner.run_flow(Nextpnr, _design(tmp_path / "d"), {"fpga": {"part": PART}})
    assert nextpnr is not None and nextpnr.succeeded
    (yosys,) = producers_of(runner, nextpnr)
    netlist = Path(yosys.results["outputs"]["netlist"]["path"])
    assert nextpnr.inputs.netlist == netlist
    (args,) = tools
    assert f"--json={netlist}" in args
    assert nextpnr.results["outputs"]["config"]["path"].endswith("config.txt")


def test_a_second_launch_reuses_both_and_hands_over_the_recorded_netlist(
    tmp_path, monkeypatch, tools
):
    first = _runner(tmp_path, monkeypatch).run_flow(
        Nextpnr, _design(tmp_path / "d"), {"fpga": {"part": PART}}
    )
    runner = _runner(tmp_path, monkeypatch)
    again = runner.run_flow(Nextpnr, _design(tmp_path / "d"), {"fpga": {"part": PART}})
    assert again.reused and producers_of(runner, again)[0].reused
    assert again.inputs.netlist == first.inputs.netlist
    assert len(tools) == 1, "nextpnr ran once"


def test_a_json_netlist_among_the_design_s_sources_skips_synthesis(tmp_path, monkeypatch, tools):
    root = tmp_path / "d"
    root.mkdir()
    (root / "top.json").write_text(NETLIST)
    design = _design(root, sources=("blink.v", {"file": "top.json", "type": "JsonNetlist"}))
    runner = _runner(tmp_path, monkeypatch)
    nextpnr = runner.run_flow(Nextpnr, design, {"fpga": {"part": PART}})
    assert nextpnr.succeeded and not producers_of(runner, nextpnr)
    assert [flow.name for flow in runner.launched] == ["nextpnr"]
    assert f"--json={root / 'top.json'}" in tools[0]


def test_nextpnr_switches_on_the_netlist_it_reads(tmp_path, monkeypatch, tools):
    runner = _runner(tmp_path, monkeypatch)
    cli = [f"fpga.part={PART}", "flows.yosys_fpga.netlist_json="]
    plan = runner.plan("nextpnr", _design(tmp_path / "d"), flow_settings=cli)
    assert plan.node("yosys_fpga").switched_on == ("netlist",)
    nextpnr = runner.run("nextpnr", _design(tmp_path / "d"), flow_settings=cli)
    assert nextpnr is not None and nextpnr.succeeded
    assert producers_of(runner, nextpnr)[0].settings.netlist_json == Path("netlist.json")


def _files(tmp_path: Path, nextpnr_part: str, yosys_part: str) -> Path:
    root = tmp_path / "d"
    root.mkdir()
    (root / "blink.v").write_text(BLINK)
    (tmp_path / PROJECT_FILE).write_text(
        f"flows:\n  nextpnr:\n    fpga: {{part: {nextpnr_part}}}\n"
    )
    design_file = root / "blink.toml"
    design_file.write_text(
        'name = "blink"\n[rtl]\nsources = ["blink.v"]\ntop = "blink"\nclock.port = "clk"\n'
        f'[flows.yosys_fpga]\nfpga.part = "{yosys_part}"\n'
    )
    return design_file


def test_devices_that_differ_in_two_files_are_an_error_naming_both(tmp_path, monkeypatch, tools):
    """The project file's `[flows.nextpnr] fpga` must not silently win over the design file's
    `[flows.yosys_fpga] fpga`."""
    design_file = _files(tmp_path, OTHER_PART, PART)
    with pytest.raises(FlowSettingsError) as raised:
        _runner(tmp_path, monkeypatch).run("nextpnr", str(design_file))
    message = str(raised.value)
    for text in (PART, OTHER_PART, str(tmp_path / PROJECT_FILE), str(design_file)):
        assert text in message, text
    assert not (tmp_path / "xeda_run").exists(), "nothing ran"


def test_a_device_given_on_the_command_line_is_the_whole_run_s(tmp_path, monkeypatch, tools):
    design_file = _files(tmp_path, OTHER_PART, PART)
    runner = _runner(tmp_path, monkeypatch)
    nextpnr = runner.run("nextpnr", str(design_file), flow_settings=[f"fpga.part={PART}"])
    assert nextpnr is not None and nextpnr.succeeded
    assert nextpnr.settings.fpga.part == PART
    assert producers_of(runner, nextpnr)[0].settings.fpga.part == PART


def test_nextpnr_reads_nothing_of_yosys_fpga_s_but_its_netlist(tmp_path, monkeypatch, tools):
    """The oracle of undeclared reads: what nextpnr opens, and what it hands its tool, of
    yosys_fpga's run directory is the declared netlist and nothing else (not the decoy beside
    it, not yosys_fpga's settings.json or results.json)."""
    opened: list[str] = []
    for method in ("run", "parse_reports"):
        original = getattr(Nextpnr, method)

        def watched(self, _original=original):
            with _watching(opened):
                return _original(self)

        monkeypatch.setattr(Nextpnr, method, watched)
    runner = _runner(tmp_path, monkeypatch)
    nextpnr = runner.run_flow(Nextpnr, _design(tmp_path / "d"), {"fpga": {"part": PART}})
    assert nextpnr.succeeded
    producer_dir = producers_of(runner, nextpnr)[0].run_path.resolve()
    declared = {nextpnr.inputs.netlist.resolve()}
    read = {Path(p).resolve() for p in opened if Path(p).resolve().is_relative_to(producer_dir)}
    handed = {
        Path(value).resolve()
        for value in (arg.partition("=")[2] for arg in tools[0])
        if value
        and Path(value).is_absolute()
        and Path(value).resolve().is_relative_to(producer_dir)
    }
    assert read <= declared, f"nextpnr read {read - declared}"
    assert handed == declared, f"nextpnr handed its tool {handed}"


@pytest.mark.parametrize(
    "settings",
    [
        {"fpga": "xc7a35tcpg236"},  # a 7-series part without its speed grade
        {"fpga": {"family": "ice40", "device": "iCE40UL1K"}},
        {"fpga": PART, "opt_timing": True},
    ],
)
def test_invalid_target_is_rejected_by_pure_planning(tmp_path, monkeypatch, settings):
    from xeda.flow import FlowSettingsException

    design = _design(tmp_path / "d")

    def unexpected(*args, **kwargs):
        raise AssertionError("planning constructed a flow or tool")

    monkeypatch.setattr(Nextpnr, "__init__", unexpected)
    monkeypatch.setattr(YosysFpga, "__init__", unexpected)
    monkeypatch.setattr(NextpnrTool, "__init__", unexpected)
    with pytest.raises(FlowSettingsException):
        DefaultRunner(tmp_path / "run").plan(Nextpnr, design, flow_settings=settings)
    assert not (tmp_path / "run").exists()


CONFIGS = [
    (PART, "textcfg", "config.txt"),
    ("iCE40HX1K-TQ144", "asc", "config.asc"),
    ("LIFCL-40-9BG400C", "fasm", "config.fasm"),
]
# Xilinx 7-series takes its FASM as `-o fasm=`: tests/test_nextpnr_xilinx.py.


@pytest.mark.parametrize("part,setting,filename", CONFIGS)
def test_enabled_family_configuration_is_recorded(
    tmp_path, monkeypatch, tools, part, setting, filename
):
    flow = _runner(tmp_path, monkeypatch).run_flow(Nextpnr, _design(tmp_path / "d"), {"fpga": part})
    assert flow.succeeded
    assert flow.outputs.config == flow.run_path / filename
    assert flow.results["outputs"]["config"]["path"] == str(flow.outputs.config)
    assert f"--{setting}={filename}" in tools[0]


@pytest.mark.parametrize("part,setting,filename", CONFIGS)
@pytest.mark.parametrize("stale", [False, True])
def test_missing_or_stale_configuration_fails(
    tmp_path, monkeypatch, tools, part, setting, filename, stale
):
    from xeda.flow import FlowFatalError

    runner = _runner(tmp_path, monkeypatch)
    design = _design(tmp_path / "d")
    if stale:
        assert runner.run_flow(Nextpnr, design, {"fpga": part}).succeeded

    def no_config(self, *args, env=None):
        for arg in map(str, args):
            if arg.startswith("--report="):
                Path(arg.partition("=")[2]).write_text(REPORT)

    monkeypatch.setattr(NextpnrTool, "run", no_config)
    runner = DefaultRunner(tmp_path / "xeda_run", rebuild_all=True, display_results=False)
    with pytest.raises(FlowFatalError, match=setting):
        runner.run_flow(Nextpnr, design, {"fpga": part})
    import json

    result = json.loads((tmp_path / "xeda_run" / "blink" / "nextpnr" / "results.json").read_text())
    assert result["success"] is False
    assert not result.get("outputs")


def test_ecp5_out_of_context_has_no_configuration(tmp_path, monkeypatch, tools):
    flow = _runner(tmp_path, monkeypatch).run_flow(
        Nextpnr, _design(tmp_path / "d"), {"fpga": PART, "out_of_context": True}
    )
    assert flow.succeeded and flow.outputs.config is None
    assert not flow.results["outputs"]
    assert not any(arg.startswith("--textcfg=") for arg in tools[0])


@pytest.mark.parametrize("cli_leaf", ["fpga.part", "flows.yosys_fpga.fpga.part"])
def test_cli_device_leaf_preserves_section_origins(tmp_path, monkeypatch, tools, cli_leaf):
    design_file = _files(tmp_path, OTHER_PART, PART)
    project = tmp_path / PROJECT_FILE
    project.write_text(
        project.read_text()
        + "    clock: {period: 10}\n  yosys_fpga: {netlist_src_attrs: false, flatten: true}\n"
    )
    design_file.write_text(design_file.read_text() + "clock.uncertainty = 0.3\nflatten = false\n")
    runner = _runner(tmp_path, monkeypatch)
    plan = runner.plan("nextpnr", design_file, flow_settings=[f"{cli_leaf}={PART}"])
    flow = runner.run("nextpnr", design_file, flow_settings=[f"{cli_leaf}={PART}"])
    assert flow.succeeded
    (producer,) = producers_of(runner, flow)
    assert flow.settings.fpga.part == producer.settings.fpga.part == PART
    assert producer.settings.flatten is False and producer.settings.netlist_src_attrs is False
    assert producer.settings.main_clock.period == flow.settings.main_clock.period == 10
    assert producer.settings.main_clock.uncertainty == flow.settings.main_clock.uncertainty == 0.3
    assert producer.settings.flatten is plan.node("yosys_fpga").settings.flatten
    assert producer.flow_hash == plan.node("yosys_fpga").flowrun_hash
    assert flow.flow_hash == plan.node("nextpnr").flowrun_hash


def test_yosys_inherited_init_creates_no_output_directories(tmp_path, monkeypatch):
    design = _design(tmp_path / "d")
    flow = YosysFpga(
        YosysFpga.Settings(
            fpga=PART,
            netlist_json="nested/netlist.json",
            rtl_json="rtl/design.json",
            rtl_verilog="rtl/design.v",
        ),
        design,
        tmp_path / "run",
    )
    monkeypatch.chdir(tmp_path)
    flow.init()
    assert not (tmp_path / "nested").exists()
    assert not (tmp_path / "rtl").exists()
    assert not flow.run_path.exists()


def test_reused_yosys_initialization_makes_no_directory_calls(tmp_path, monkeypatch, tools):
    runner = _runner(tmp_path, monkeypatch)
    design = _design(tmp_path / "d")
    assert runner.run_flow(Nextpnr, design, {"fpga": PART}).succeeded
    original = YosysFpga.init

    def init(self):
        with monkeypatch.context() as patch:

            def unexpected(*args, **kwargs):
                raise AssertionError("reused Yosys init called mkdir")

            patch.setattr(Path, "mkdir", unexpected)
            original(self)

    monkeypatch.setattr(YosysFpga, "init", init)
    again = runner.run_flow(Nextpnr, design, {"fpga": PART})
    assert again.reused and producers_of(runner, again)[0].reused


def test_successful_declared_launch_and_delivery_preserve_outside_tree(
    tmp_path, monkeypatch, tools
):
    from .test_isolation import _state, _world, watching

    world = _world(tmp_path)
    monkeypatch.chdir(world.work)
    (world.work / "input.json").write_text(NETLIST)
    design = Design(
        name="placed",
        design_root=world.work,
        rtl={"sources": [{"file": "input.json", "type": "JsonNetlist"}], "top": "blink"},
    )
    runner = DefaultRunner(world.root, display_results=False, outputs_to=world.delivered)
    before = _state(world.parent, [world.root, world.delivered])
    with watching(world) as violations:
        flow = runner.run_flow(Nextpnr, design, {"fpga": PART})
    assert flow.succeeded
    assert not violations
    assert _state(world.parent, [world.root, world.delivered]) == before
    assert (world.delivered / "config.txt").read_text() == "config\n"
    assert flow.inputs.netlist == world.work / "input.json"


_SUBPROCESS_PNR = """
import json, os, sys
from pathlib import Path
opened = []
def audit(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
        opened.append(os.fsdecode(args[0]))
sys.addaudithook(audit)
options = dict(arg[2:].split('=', 1) for arg in sys.argv[1:] if arg.startswith('--') and '=' in arg)
json.loads(Path(options['json']).read_text())
for name in ('textcfg', 'asc', 'fasm'):
    if options.get(name):
        Path(options[name]).write_text('config\\n')
Path(options['report']).write_text('{"fmax": {}, "utilization": {}, "critical_paths": []}\\n')
Path('opened.json').write_text(json.dumps(opened))
"""


@pytest.mark.parametrize("source_supplied", [False, True])
def test_nextpnr_subprocess_reads_only_the_selected_netlist(
    tmp_path, monkeypatch, tools, source_supplied
):
    import json

    script = tmp_path / "pnr.py"
    script.write_text(_SUBPROCESS_PNR)
    design = _design(tmp_path / "d")
    if source_supplied:
        (design.root_path / "input.json").write_text(NETLIST)
        design = _design(design.root_path, sources=({"file": "input.json", "type": "JsonNetlist"},))
    (tmp_path / "canary.json").write_text("outside canary")

    def run(self, *args, env=None):
        return self.execute(sys.executable, script, *args, env=env)

    monkeypatch.setattr(NextpnrTool, "run", run)
    runner = _runner(tmp_path, monkeypatch)
    flow = runner.run_flow(Nextpnr, design, {"fpga": PART})
    assert flow.succeeded
    opened = {Path(p).resolve() for p in json.loads((flow.run_path / "opened.json").read_text())}
    producer = flow.inputs.netlist.parent
    assert {p for p in opened if p.is_relative_to(producer)} == {flow.inputs.netlist}
    assert tmp_path / "canary.json" not in opened
    again = runner.run_flow(Nextpnr, design, {"fpga": PART})
    assert again.reused
    if not source_supplied:
        assert producers_of(runner, again)[0].reused
    assert again.inputs.netlist == flow.inputs.netlist
    changed = runner.run_flow(Nextpnr, design, {"fpga": PART, "seed": 1})
    assert changed.succeeded and not changed.reused
    if not source_supplied:
        assert producers_of(runner, changed)[0].reused
    opened = {Path(p).resolve() for p in json.loads((changed.run_path / "opened.json").read_text())}
    assert {p for p in opened if p.is_relative_to(producer)} == {changed.inputs.netlist}


_CLI_STANDINS = """
from pathlib import Path
from xeda.flows import YosysFpga
from xeda.flows.nextpnr import NextpnrTool
from xeda.cli import cli
def synth(self):
    self.outputs.netlist = self.run_path / self.settings.netlist_json
    self.outputs.netlist.write_text('{}\\n')
def place(self, *args, env=None):
    for arg in map(str, args):
        name, _, value = arg.partition('=')
        if name == '--report':
            Path(value).write_text('{"fmax": {}, "utilization": {}, "critical_paths": []}\\n')
        elif name == '--textcfg':
            Path(value).write_text('config\\n')
YosysFpga.run = synth
YosysFpga.parse_reports = lambda self: True
NextpnrTool.run = place
cli()
"""


def test_two_unchanged_cli_processes_reuse_both_recorded_flows(tmp_path):
    import json
    import subprocess

    root = tmp_path / "d"
    root.mkdir()
    (root / "blink.v").write_text(BLINK)
    spec = root / "d.toml"
    spec.write_text(
        'name="blink"\n[rtl]\nsources=["blink.v"]\ntop="blink"\n[flows.nextpnr]\nfpga.part="'
        + PART
        + '"\n'
    )
    documents = []
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, "-c", _CLI_STANDINS, "run", "nextpnr", str(spec), "--json"],
            cwd=tmp_path,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        documents.append(json.loads(result.stdout))
    assert [node["state"] for node in documents[0]["nodes"]] == ["ran", "ran"]
    assert [node["state"] for node in documents[1]["nodes"]] == ["fresh", "fresh"]
    producer = Path(documents[1]["nodes"][0]["run_path"])
    record = json.loads((producer / "results.json").read_text())["outputs"]["netlist"]
    consumer = Path(documents[1]["run_path"])
    binding = json.loads((consumer / "trace.json").read_text())["declared_inputs"][0]
    assert binding["paths"] == [record["path"]]


@pytest.mark.parametrize("cli_leaf", ["fpga.part", "flows.yosys_fpga.fpga.part"])
def test_cli_device_conflict_names_both_files_and_override_wins(tmp_path, cli_leaf):
    import json
    import subprocess

    spec = _files(tmp_path, OTHER_PART, PART)
    command = [sys.executable, "-c", _CLI_STANDINS, "run", "nextpnr", str(spec), "--json"]
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    refused = subprocess.run(
        command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60
    )
    assert refused.returncode != 0
    message = json.loads(refused.stdout)["error"]["message"]
    for text in (PART, OTHER_PART, str(spec), str(tmp_path / PROJECT_FILE)):
        assert text in message
    assert not (tmp_path / "xeda_run").exists()
    ran = subprocess.run(
        command + ["-s", f"{cli_leaf}={PART}"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert ran.returncode == 0, ran.stderr
    document = json.loads(ran.stdout)
    assert document["success"]
    for node in document["nodes"]:
        settings = json.loads((Path(node["run_path"]) / "settings.json").read_text())[
            "flow_settings"
        ]
        assert settings["fpga"]["part"] == PART
