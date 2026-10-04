"""nextpnr on Xilinx 7-series: the nextpnr-himbaechel command line and what its reports mean.

Command construction runs against a stand-in for the tool; the reports and placement dumps under
`tests/resources/nextpnr/xilinx_*.json` were written by nextpnr-himbaechel 1.0.0 (openXC7) for an
xc7a100tcsg324-1: a counter (`blinky`), one `LUT6_2`, and a `RAM32X1D` with an `SRL16E` and a
counter (`ram_srl`). The launches at the end use the process fakes only.
"""

import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

import xeda.flows.nextpnr as nextpnr_module
from xeda import Design
from xeda.flow import FlowFatalError, FlowSettingsException
from xeda.flow_runner import DefaultRunner
from xeda.flows import Nextpnr
from xeda.flows.nextpnr import NextpnrTool

from . import tool_utils
from .test_xilinx_chipdb import PARTS, _binary
from .test_xilinx_chipdb import prefix as chipdb_prefix

# The fake installation, under the name the tests ask for it by.
prefix = chipdb_prefix

RESOURCES = Path(__file__).parent / "resources" / "nextpnr"
A100T = "xc7a100tcsg324-1"
REPORT = {"fmax": {}, "utilization": {}, "critical_paths": []}
PINS = "set_property LOC E3 [get_ports clk]\n"


def _resource(name: str):
    return json.loads((RESOURCES / f"xilinx_{name}.json").read_text())


class _Tool:
    """Stands in for nextpnr: records the call and writes what a successful run would."""

    def __init__(self, flow, report=None, placement=None, skip=()):
        self.flow, self.report, self.placement, self.skip = flow, report, placement, skip
        self.calls: list[tuple[str, list[str]]] = []

    def __call__(self, tool, *args, env=None):
        args = [str(arg) for arg in args]
        self.calls.append((tool.executable, args))
        run = self.flow.run_path
        run.mkdir(parents=True, exist_ok=True)
        for i, arg in enumerate(args):
            name, _, value = arg.partition("=")
            if name == "--report":
                (run / value).write_text(json.dumps(self.report or REPORT))
            elif args[i - 1] == "-o" and name in ("fasm", "placement") and name not in self.skip:
                path = run / value
                path.parent.mkdir(parents=True, exist_ok=True)
                content = {} if self.placement is None else self.placement
                path.write_text("TILE.FEATURE\n" if name == "fasm" else json.dumps(content))

    @property
    def args(self) -> list[str]:
        return self.calls[0][1]

    def vopts(self) -> list[str]:
        return [self.args[i + 1] for i, arg in enumerate(self.args) if arg == "-o"]


def _flow(tmp_path, monkeypatch, prefix, part=A100T, *, pins=PINS, sdc=None, **settings):
    """A Xilinx `Nextpnr` with an explicit chip database, prepared as the launcher would."""
    fabric = next(entry[3] for entry in PARTS if entry[0].lower() == part.lower())
    root = tmp_path / "design"
    root.mkdir(exist_ok=True)
    chipdb = root / "chip.bin"
    chipdb.write_bytes(_binary(fabric))
    netlist = root / "netlist.json"
    netlist.write_text('{"modules": {"d": {"ports": {"clk": {"bits": [2]}}, "netnames": {}}}}')
    design = Design(name="d", rtl={"sources": [], "top": "d"}, design_root=root)
    flow = Nextpnr(
        Nextpnr.Settings.from_input(
            {"fpga": part, "chipdb": chipdb, **settings}, design_root=root, runner_cwd=root
        ),
        design,
        tmp_path / "pnr",
    )
    flow.inputs.netlist = netlist
    if pins is not None:
        (root / "pins.xdc").write_text(pins)
        flow.inputs.constraints = [root / "pins.xdc"]
    if sdc is not None:
        (root / "timing.sdc").write_text(sdc)
        flow.inputs.sdc = [root / "timing.sdc"]
    monkeypatch.setattr(nextpnr_module, "which", lambda name: str(prefix / "bin" / name))
    tool = _Tool(flow)
    monkeypatch.setattr(NextpnrTool, "run", lambda self, *args, env=None: tool(self, *args))
    flow.prepare_inputs()
    return flow, tool


# ----------------------------------------------------------------------------- the command line


@pytest.mark.parametrize(
    "part,device",
    [
        (A100T, A100T),
        ("XC7A35TCSG324-1", "xc7a35tcsg324-1"),
        ("xc7k420tffg1156-2", "xc7k420tffg1156-2"),
        ("xc7s75fgga676-1", "xc7s75fgga676-1"),
        # nextpnr matches the device case-sensitively: the database's own spelling, `2L`
        ("xc7z035ffg676-2l", "xc7z035ffg676-2L"),
        ("XC7Z035FFG676-2L", "xc7z035ffg676-2L"),
        ("xc7vx485tffg1761-3", "xc7vx485tffg1761-3"),
    ],
)
def test_every_7_series_family_runs_nextpnr_himbaechel(tmp_path, monkeypatch, prefix, part, device):
    flow, tool = _flow(tmp_path, monkeypatch, prefix, part)
    assert Nextpnr.target_for_settings(flow.settings)[0] == "xilinx"
    flow.run()
    executable, args = tool.calls[0]
    assert executable == "nextpnr-himbaechel"
    assert args[:10] == [
        "--device",
        device,
        "--chipdb",
        str(tmp_path / "design/chip.bin"),
        "--json",
        str(tmp_path / "design/netlist.json"),
        "-o",
        f"xdc={flow.run_path / 'constraints.xdc'}",
        "-o",
        "fasm=config.fasm",
    ]
    assert tool.vopts()[2:] == ["placement=placement.json"]
    assert not [a for a in args if re.match(r"--(xdc|fasm|freq|lpf|pcf|pdc|json=|device=)", a)]
    assert "--report=report.json" in args and "--log=nextpnr.log" in args
    assert flow.outputs.config == flow.run_path / "config.fasm"


def test_xilinx_options_are_passed_to_the_backend(tmp_path, monkeypatch, prefix):
    flow, tool = _flow(
        tmp_path,
        monkeypatch,
        prefix,
        delay_matrix="off",
        hold_fix=3,
        hold_detour_max=0.5,
        placement="where.json",
        write="routed.json",
        sdf="d.sdf",
        seed=7,
        placer="heap",
        router="router2",
    )
    flow.run()
    assert tool.vopts()[2:] == [
        "placement=where.json",
        "delay-matrix=off",
        "hold-fix=3",
        "hold-detour-max=0.5",
    ]
    for arg in ("--write=routed.json", "--sdf=d.sdf", "--seed=7", "--placer=heap"):
        assert arg in tool.args
    assert flow.artifacts["placement"] == flow.run_path / "where.json"


@pytest.mark.parametrize(
    "value,options", [(False, []), (True, ["hold-fix"]), (1, ["hold-fix=1"]), (8, ["hold-fix=8"])]
)
def test_hold_fix_is_a_switch_or_a_pass_limit(tmp_path, monkeypatch, prefix, value, options):
    flow, tool = _flow(tmp_path, monkeypatch, prefix, hold_fix=value)
    assert flow.settings.hold_fix is value or flow.settings.hold_fix == value
    assert type(flow.settings.hold_fix) is type(value)
    flow.run()
    assert tool.vopts()[3:] == options


@pytest.mark.parametrize("value", [0, -1, 2.5, "x"])
def test_hold_fix_rejects_what_is_neither(value):
    with pytest.raises(ValidationError):
        Nextpnr.Settings(fpga=A100T, hold_fix=value)
    settings = Nextpnr.Settings(fpga=A100T)
    with pytest.raises(ValidationError):
        settings.hold_fix = value


@pytest.mark.parametrize(
    "settings,message",
    [
        ({"fpga": A100T, "lpf_allow_unconstrained": True}, "does not take lpf_allow_unconstrained"),
        ({"fpga": A100T, "opt_timing": True}, "nextpnr-himbaechel does not take opt_timing"),
        ({"fpga": "LFE5U-25F-6BG381C", "hold_fix": True}, "nextpnr-ecp5 does not take hold_fix"),
        ({"fpga": "iCE40HX1K-TQ144", "delay_matrix": "off"}, "does not take delay_matrix"),
        ({"fpga": "LFE5U-25F-6BG381C", "chipdb": "chip.bin"}, "does not take chipdb"),
        ({"fpga": "LIFCL-40-9BG400C", "placement": "p.json"}, "does not take placement"),
        ({"fpga": "xc7a35tcsg324"}, "full part"),
        ({"fpga": {"family": "artix-7", "device": "xc7a35t"}}, "full part"),
        ({"fpga": {"vendor": "gowin", "family": "gowin", "device": "GW1N-9"}}, "xilinx"),
    ],
)
def test_a_setting_of_another_backend_or_a_partial_part_is_rejected(settings, message):
    with pytest.raises(FlowSettingsException, match=re.escape(message)):
        Nextpnr.check_settings_supported(Nextpnr.Settings(**settings))


def test_the_xilinx_defaults_are_no_setting_of_another_family():
    for part in ("LFE5U-25F-6BG381C", "iCE40HX1K-TQ144", "LIFCL-40-9BG400C"):
        Nextpnr.check_settings_supported(Nextpnr.Settings(fpga=part))


@pytest.mark.parametrize(
    "banner,version",
    [
        (
            '"nextpnr-himbaechel" -- Next Generation Place and Route (Version 1.0.0-7-g334d1b18)',
            "1.0.0",
        ),
        (
            '"nextpnr-ecp5" -- Next Generation Place and Route (Version nextpnr-0.11.1-3-g930fef44)',
            "0.11.1",
        ),
        (
            '"nextpnr-himbaechel" -- Next Generation Place and Route (Version nextpnr-1.0.0)',
            "1.0.0",
        ),
    ],
)
def test_the_version_banner_of_either_spelling_is_read(banner, version):
    (pattern,) = NextpnrTool(executable="nextpnr-himbaechel").version_regexps
    match = re.search(pattern, banner)
    assert match.group("version") == version


def test_an_enabled_fasm_the_tool_did_not_write_fails_the_run(tmp_path, monkeypatch, prefix):
    flow, tool = _flow(tmp_path, monkeypatch, prefix)
    tool.skip = ("fasm",)
    with pytest.raises(FlowFatalError, match="fasm"):
        flow.run()


# ------------------------------------------------------------------------------------- clocks


def _xdc(flow) -> str:
    return (flow.run_path / "constraints.xdc").read_text()


def test_a_clock_setting_becomes_a_create_clock_and_never_a_freq(tmp_path, monkeypatch, prefix):
    flow, tool = _flow(
        tmp_path, monkeypatch, prefix, clocks={"main": {"port": "clk", "period": 10}}
    )
    flow.run()
    assert "create_clock -period 10.0 [get_ports {clk}]" in _xdc(flow)
    assert not [a for a in tool.args if a.startswith(("--freq", "--sdc"))]


def test_clocks_only_in_a_typed_sdc_reach_sdc_and_generate_nothing(tmp_path, monkeypatch, prefix):
    sdc = "create_clock -period 8 [get_ports clk]\n"
    flow, tool = _flow(tmp_path, monkeypatch, prefix, sdc=sdc)
    flow.run()
    assert f"--sdc={flow.run_path / 'constraints.sdc'}" in tool.args
    assert (flow.run_path / "constraints.sdc").read_text() == sdc
    assert _xdc(flow) == PINS
    assert not [a for a in tool.args if a.startswith("--freq")]


def test_a_clock_given_twice_names_the_sdc_source_and_line(tmp_path, monkeypatch, prefix):
    flow, tool = _flow(
        tmp_path,
        monkeypatch,
        prefix,
        sdc="# timing\ncreate_clock -period 8 [get_ports clk]\n",
        clocks={"main": {"port": "clk", "period": 10}},
    )
    with pytest.raises(FlowFatalError, match="Duplicate clock") as error:
        flow.run()
    assert f"{tmp_path / 'design/timing.sdc'}:2" in str(error.value)
    assert "clocks.main" in str(error.value)
    assert not tool.calls


# ------------------------------------------------------------------------------------ results


def _parsed(tmp_path, monkeypatch, prefix, report, placement, part=A100T, **settings):
    flow, tool = _flow(tmp_path, monkeypatch, prefix, part, **settings)
    tool.report, tool.placement = report, placement
    if placement is None:
        tool.skip = ("placement",)
    flow.run()
    ok = flow.parse_reports()
    return flow, ok


def test_the_measured_counter_reports_occupied_luts_not_positions(tmp_path, monkeypatch, prefix):
    flow, ok = _parsed(
        tmp_path,
        monkeypatch,
        prefix,
        _resource("blinky_report"),
        _resource("blinky_placement"),
        clocks={"main": {"port": "clk", "period": 10}},
    )
    r = flow.results
    assert ok is True
    assert r["lut"] == 29  # physical LUT locations: (tile, site, A-D)
    assert r["SLICE_LUTX"] == 57  # nextpnr's own count: 5LUT/6LUT positions
    assert r["LUT:STAGE"] == "placed and routed"
    assert "placement dump" in r["LUT:METHOD"]
    assert r["ff"] == 25 and r["SLICE_FFX"] == 25
    assert r["CARRY4"] == 7  # raw, never folded into another resource
    assert r["bram"] == 0 and r["dsp"] == 0 and r["io"] == 2
    assert r["Fmax"] == pytest.approx(325.839, abs=1e-3)
    assert r["clock_frequency"] == 100 and r["timing_met"] is True
    assert r["wns"] == pytest.approx(10 - 1000 / 325.8390197753906, abs=1e-6)
    assert r["device"] == A100T and r["fabric"] == "xc7a100t"
    assert r["_utilization"]["SLICE_LUTX"]["available"] == 126800
    # The domain is the BUFG's output net, not a port: that one clock was constrained, on port
    # `clk`, does not prove the domain is that port's (logic on an MMCM's output is not).
    assert list(r["_fmax"]) == ["$abc$2027$aiger$o71"]
    assert "clock_port" not in r
    assert flow.artifacts["placement"] == flow.run_path / "placement.json"


def test_the_two_outputs_of_a_lut6_2_are_two_locations(tmp_path, monkeypatch, prefix):
    flow, ok = _parsed(
        tmp_path, monkeypatch, prefix, _resource("lut6_2_report"), _resource("lut6_2_placement")
    )
    assert ok is True
    assert flow.results["lut"] == 2 and flow.results["SLICE_LUTX"] == 2
    assert "Fmax" not in flow.results and "clock_port" not in flow.results
    assert flow.results["ff"] == 0


def test_ram_and_shift_register_luts_count_as_the_locations_they_occupy(
    tmp_path, monkeypatch, prefix
):
    flow, ok = _parsed(
        tmp_path, monkeypatch, prefix, _resource("ram_srl_report"), _resource("ram_srl_placement")
    )
    assert ok is True
    assert flow.results["lut"] == 12 and flow.results["SLICE_LUTX"] == 20
    assert flow.results["ff"] == 8
    # one domain, named for a net inside the RAM, and no single clock xeda knows: no port
    assert "clock_port" not in flow.results


def _lut(site, bel, tile="CLBLL_L_X2Y56", kind="SLICE_LUTX"):
    return {"tile": tile, "site": site, "bel": bel, "type": kind}


UTILIZATION = {"SLICE_LUTX": {"used": 3, "available": 126800}}


def test_a_fractured_pair_is_one_lut_and_other_sites_are_their_own(tmp_path, monkeypatch, prefix):
    placement = {
        "a": _lut("SLICE_X0Y56", "A5LUT"),
        "b": _lut("SLICE_X0Y56", "A6LUT"),
        "c": _lut("SLICE_X1Y56", "A6LUT"),
        "ff": _lut("SLICE_X0Y56", "AFF", kind="SLICE_FFX"),
        "carry": _lut("SLICE_X0Y56", "CARRY4", kind="CARRY4"),
    }
    flow, _ = _parsed(
        tmp_path, monkeypatch, prefix, {**REPORT, "utilization": UTILIZATION}, placement
    )
    assert flow.results["lut"] == 2


@pytest.mark.parametrize(
    "placement",
    [
        None,
        [],
        {"a": _lut("SLICE_X0Y56", "LUTX")},
        {"a": _lut("", "A6LUT")},
        {"a": {"type": "SLICE_LUTX", "bel": "A6LUT"}},
        {"a": "SLICE_X0Y56/A6LUT"},
        # fewer placed LUT cells than the report counts as used
        {"a": _lut("SLICE_X0Y56", "A6LUT")},
    ],
    ids=["missing", "not-a-mapping", "bel", "site", "fields", "entry", "disagrees"],
)
def test_an_unusable_placement_dump_gives_no_lut_count(tmp_path, monkeypatch, prefix, placement):
    flow, ok = _parsed(
        tmp_path, monkeypatch, prefix, {**REPORT, "utilization": UTILIZATION}, placement
    )
    assert ok is True
    assert "lut" not in flow.results and "LUT:METHOD" not in flow.results
    assert flow.results["SLICE_LUTX"] == 3


def test_a_previous_run_s_placement_dump_is_not_counted(tmp_path, monkeypatch, prefix):
    flow, tool = _flow(tmp_path, monkeypatch, prefix)
    flow.run_path.mkdir(parents=True)
    (flow.run_path / "placement.json").write_text(json.dumps({"a": _lut("SLICE_X0Y56", "A6LUT")}))
    flow.start_run()
    tool.report = {**REPORT, "utilization": {"SLICE_LUTX": {"used": 1, "available": 2}}}
    tool.skip = ("placement",)
    flow.run()
    assert flow.parse_reports() is True
    assert "lut" not in flow.results


def test_files_a_previous_run_left_are_not_this_run_s_artifacts(tmp_path, monkeypatch, prefix):
    """An artifact is a file this run wrote, the rule every other artifact follows: an earlier
    run's SDF, routed netlist or placement dump that the tool did not write again is not one."""
    flow, tool = _flow(tmp_path, monkeypatch, prefix, sdf="routed.sdf", write="routed.json")
    flow.run_path.mkdir(parents=True)
    for name in ("routed.sdf", "routed.json", "placement.json"):
        (flow.run_path / name).write_text("{}\n")
    flow.start_run()
    tool.skip = ("placement",)
    flow.run()
    assert not {"sdf", "write", "placement"} & set(flow.artifacts)


def test_totals_are_the_routed_fabric_s_not_the_marketed_device_s(tmp_path, monkeypatch, prefix):
    report = {
        **REPORT,
        "utilization": {
            name: {"used": 0, "available": 65200} for name in ("SLICE_LUTX", "SLICE_FFX")
        },
    }
    flow, _ = _parsed(tmp_path, monkeypatch, prefix, report, {}, part="xc7a35tcsg324-1")
    assert flow.results["device"] == "xc7a35tcsg324-1"
    assert flow.results["fabric"] == "xc7a50t"
    assert flow.results["lut"] == 0 and flow.results["ff"] == 0


def test_a_missed_constraint_fails_unless_allowed(tmp_path, monkeypatch, prefix):
    report = {**REPORT, "fmax": {"clk": {"achieved": 90.0, "constraint": 100.0}}}
    flow, ok = _parsed(tmp_path, monkeypatch, prefix, report, {})
    assert ok is False and flow.results["timing_met"] is False
    assert flow.results["clock_port"] == "clk"  # the domain is itself a port of the top module


def test_two_domains_keep_their_raw_names_only(tmp_path, monkeypatch, prefix):
    report = {
        **REPORT,
        "fmax": {
            "$glb$a": {"achieved": 200.0, "constraint": 100.0},
            "$glb$b": {"achieved": 150.0, "constraint": 100.0},
        },
    }
    flow, ok = _parsed(
        tmp_path, monkeypatch, prefix, report, {}, clocks={"main": {"port": "clk", "period": 10}}
    )
    assert ok is True and flow.results["Fmax"] == 150.0
    assert "clock_port" not in flow.results and set(flow.results["_fmax"]) == {"$glb$a", "$glb$b"}


# ------------------------------------------------------------------------------------ launches


def _calls(run_path: Path) -> list[dict]:
    lines = (run_path / "fake_fpga.calls.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def _project(tmp_path, monkeypatch, sdc=None):
    tool_utils.use_fake_fpga_tools(monkeypatch, tmp_path / "toolchain")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "top.v").write_text("module top(input clk, output q); assign q = clk; endmodule\n")
    (tmp_path / "pins.xdc").write_text(PINS)
    sources = ["top.v", "pins.xdc"]
    if sdc is not None:
        (tmp_path / "timing.sdc").write_text(sdc)
        sources.append("timing.sdc")
    design = Design(name="top", design_root=tmp_path, rtl={"sources": sources, "top": "top"})
    return design, DefaultRunner(tmp_path / "run", display_results=False)


def test_a_launch_places_a_7_series_design_and_the_next_reuses_it(tmp_path, monkeypatch):
    design, runner = _project(tmp_path, monkeypatch, sdc="create_clock -period 8 [get_ports clk]\n")
    first = runner.run("nextpnr", design, flow_settings={"fpga": A100T})
    assert first is not None and first.succeeded
    (call,) = _calls(first.run_path)
    argv = call["argv"]
    chipdb = argv[argv.index("--chipdb") + 1]
    assert call["tool"] == "nextpnr-himbaechel"
    assert Path(chipdb).is_relative_to(tmp_path / "run/.cache/xilinx-chipdb")
    assert argv[:2] == ["--device", A100T] and argv[4:6] == ["--json", str(first.inputs.netlist)]
    assert f"--sdc={first.run_path / 'constraints.sdc'}" in argv
    assert not [a for a in argv if a.startswith("--freq")]
    assert call["environment"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert set(call["inputs"]) == {
        str(first.inputs.netlist),
        chipdb,
        str(first.run_path / "constraints.xdc"),
        str(first.run_path / "constraints.sdc"),
    }
    assert first.outputs.config == first.run_path / "config.fasm"
    assert first.results["outputs"]["config"]["path"] == str(first.outputs.config)
    assert first.results["lut"] == 1 and first.results["ff"] == 1
    assert first.results["fabric"] == "xc7a100t"
    again = runner.run("nextpnr", design, flow_settings={"fpga": A100T})
    assert again is not None and again.reused and again.completed_dependencies[0].reused
    assert len(_calls(first.run_path)) == 1


def test_a_launch_with_a_clock_in_the_sdc_and_the_settings_names_both(tmp_path, monkeypatch):
    design, runner = _project(
        tmp_path, monkeypatch, sdc="\ncreate_clock -period 8 [get_ports clk]\n"
    )
    settings = {"fpga": A100T, "clocks": {"main": {"port": "clk", "period": 10}}}
    with pytest.raises(FlowFatalError, match="Duplicate clock") as error:
        runner.run("nextpnr", design, flow_settings=settings)
    assert f"{tmp_path / 'timing.sdc'}:2" in str(error.value)
    assert not (tmp_path / "run/top/nextpnr/fake_fpga.calls.jsonl").exists()


def test_a_board_supplies_the_part_and_its_pin_file(tmp_path, monkeypatch):
    """No typed XDC source: the bundled Arty A7-100 pin file is the fallback, with the clock."""
    design, runner = _project(tmp_path, monkeypatch)
    design = Design(name="top", design_root=tmp_path, rtl={"sources": ["top.v"], "top": "top"})
    settings = {"board": "ARTY_A7_100T", "clocks": {"main": {"port": "CLK100MHZ", "period": 10}}}
    flow = runner.run("nextpnr", design, flow_settings=settings)
    assert flow is not None and flow.succeeded
    merged = (flow.run_path / "constraints.xdc").read_text()
    assert "PACKAGE_PIN E3" in merged
    assert merged.rstrip().endswith("create_clock -period 10.0 [get_ports {CLK100MHZ}]")
    assert _calls(flow.run_path)[0]["argv"][:2] == ["--device", "xc7a100tcsg324-1"]
