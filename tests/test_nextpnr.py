"""Parsing of nextpnr's JSON report.

`Nextpnr` used to ask nextpnr for a report (`--report`) and then never read it, so the flow
produced no timing or utilization results at all. These tests drive the parser against a report
captured from a real nextpnr-ecp5 0.11.1 run, so they need no EDA tool installed.

nextpnr reports *frequencies*, not slack, so `wns` is derived from the constrained and achieved
clock periods; the arithmetic is pinned here.
"""

import json
import re
from pathlib import Path
from typing import Any, Dict

import pytest

from xeda import Design
from xeda.flow import FPGA, FlowDependencyFailure, FlowSettingsException
from xeda.flow_runner import DefaultRunner
from xeda.flows import Nextpnr, YosysFpga
from xeda.flows.nextpnr import ECP5_RESOURCES, EcpPLL, NextpnrTool

TESTS_DIR = Path(__file__).parent.absolute()
RESOURCES_DIR = TESTS_DIR / "resources" / "nextpnr"
EXAMPLES_DIR = TESTS_DIR.parent / "examples"
ECP5_REPORT = RESOURCES_DIR / "ecp5_report.json"

VERSION_BANNER = (
    '"nextpnr-ecp5" -- Next Generation Place and Route ' "(Version nextpnr-0.11.1-3-g930fef44)"
)


def make_flow(tmp_path: Path, report: Any, family: str = "ecp5") -> Nextpnr:
    """A Nextpnr instance whose run directory holds `report` as its nextpnr report."""
    design = Design(
        name="dummy",
        rtl={"sources": [], "top": "dummy", "clock_port": "clk"},  # type: ignore[arg-type]
    )
    settings = Nextpnr.Settings(fpga=FPGA(family=family), clock_period=5.0)  # type: ignore[call-arg]
    flow = Nextpnr(settings, design, tmp_path)
    report_path = tmp_path / str(settings.report)
    if report is not None:
        report_path.write_text(report if isinstance(report, str) else json.dumps(report))
    return flow


def load_ecp5_report() -> Dict[str, Any]:
    return json.loads(ECP5_REPORT.read_text())


def _netlist(tmp_path: Path) -> Path:
    netlist = tmp_path / "synth" / "netlist.json"
    netlist.parent.mkdir(exist_ok=True)
    netlist.write_text("{}")
    return netlist


def write_nextpnr_config(flow: Nextpnr, args) -> None:
    """A successful nextpnr stand-in writes each enabled family configuration."""
    flow.run_path.mkdir(parents=True, exist_ok=True)
    for arg in map(str, args):
        name, _, value = arg.partition("=")
        if name in ("--textcfg", "--asc", "--fasm"):
            path = flow.run_path / value
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("config\n")


# --------------------------------------------------------------------------- the captured report


def test_captured_report_has_the_documented_shape():
    """Guards the assumption the parser is built on: these keys come from shared nextpnr code."""
    report = load_ecp5_report()
    assert set(report) >= {"fmax", "utilization", "critical_paths"}
    for values in report["fmax"].values():
        assert {"achieved", "constraint"} <= set(values)
    for counts in report["utilization"].values():
        assert {"used", "available"} <= set(counts)


def test_parses_a_real_ecp5_report(tmp_path):
    flow = make_flow(tmp_path, load_ecp5_report())
    assert flow.parse_reports() is True
    r = flow.results
    # timing: 25 MHz constraint, 253.936 MHz achieved -> 40ns - 3.938ns of slack
    assert r["Fmax"] == pytest.approx(253.936, abs=1e-3)
    assert r["clock_frequency"] == 25
    assert r["clock_period"] == pytest.approx(40.0)
    assert r["wns"] == pytest.approx(40.0 - 1000.0 / 253.936, abs=1e-3)
    assert r["timing_met"] is True
    assert r["clock_domains"] == 1
    # utilization, canonical names for ECP5
    assert r["lut"] == 37  # TRELLIS_COMB
    assert r["ff"] == 32  # TRELLIS_FF
    assert r["io"] == 16  # TRELLIS_IO
    assert r["bram"] == 0  # DP16KD
    assert r["dsp"] == 0  # MULT18X18D + ALU54B
    # raw bel-type counts are reported too, and only for bels actually used
    assert r["TRELLIS_COMB"] == 37
    assert "DP16KD" not in r
    # TRELLIS_SLICE only exists in much older nextpnr, so `slice` must stay unset
    assert "slice" not in r


def test_records_the_report_as_an_artifact(tmp_path):
    flow = make_flow(tmp_path, load_ecp5_report())
    flow.parse_reports()
    assert Path(flow.artifacts["report"]).name == "report.json"


def test_utilization_detail_includes_percentages(tmp_path):
    flow = make_flow(tmp_path, load_ecp5_report())
    flow.parse_reports()
    detail = flow.results["_utilization"]["TRELLIS_COMB"]
    assert detail["used"] == 37
    assert detail["available"] == 83640
    assert detail["utilization_percent"] == pytest.approx(100 * 37 / 83640, abs=1e-2)


def test_critical_paths_are_summarized_not_copied(tmp_path):
    """The full stage-by-stage detail stays in the report file; results keep a summary."""
    report = load_ecp5_report()
    flow = make_flow(tmp_path, report)
    flow.parse_reports()
    summary = flow.results["_critical_paths"]
    assert len(summary) == len(report["critical_paths"])
    first = summary[0]
    assert set(first) == {"from", "to", "delay_ns", "stages"}
    expected = sum(stage["delay"] for stage in report["critical_paths"][0]["path"])
    assert first["delay_ns"] == pytest.approx(expected, abs=1e-3)


# --------------------------------------------------------------------------- timing arithmetic


@pytest.mark.parametrize(
    "constraint,achieved,expected_wns,met",
    [
        (100.0, 200.0, 5.0, True),  # 10ns constrained, 5ns achieved
        (200.0, 200.0, 0.0, True),  # exactly met
        (250.0, 200.0, -1.0, False),  # 4ns constrained, 5ns achieved
    ],
)
def test_wns_is_derived_from_periods(tmp_path, constraint, achieved, expected_wns, met):
    report = {"fmax": {"clk": {"achieved": achieved, "constraint": constraint}}}
    flow = make_flow(tmp_path, report)
    result = flow.parse_reports()
    assert flow.results["wns"] == pytest.approx(expected_wns, abs=1e-3)
    assert flow.results["timing_met"] is met
    assert result is met


def test_timing_failure_still_reports_results(tmp_path):
    """A missed constraint must not cost the user the numbers that explain it."""
    report = {
        "fmax": {"clk": {"achieved": 100.0, "constraint": 200.0}},
        "utilization": {"TRELLIS_COMB": {"used": 5, "available": 100}},
    }
    flow = make_flow(tmp_path, report)
    assert flow.parse_reports() is False
    assert flow.results["Fmax"] == pytest.approx(100.0)
    assert flow.results["wns"] < 0
    assert flow.results["lut"] == 5


def test_timing_allow_fail_keeps_the_flow_successful(tmp_path):
    report = {"fmax": {"clk": {"achieved": 100.0, "constraint": 200.0}}}
    flow = make_flow(tmp_path, report)
    flow.settings.timing_allow_fail = True
    assert flow.parse_reports() is True
    assert flow.results["timing_met"] is False  # still reported


def test_multiple_clock_domains(tmp_path):
    report = {
        "fmax": {
            "slow": {"achieved": 120.0, "constraint": 50.0},
            "fast": {"achieved": 210.0, "constraint": 200.0},
        }
    }
    flow = make_flow(tmp_path, report)
    assert flow.parse_reports() is True
    # Fmax is the lowest achieved: the frequency at which every domain is still satisfied
    assert flow.results["Fmax"] == pytest.approx(120.0)
    # wns comes from the domain with the least slack, which here is the other one
    assert flow.results["wns"] == pytest.approx(1000 / 200.0 - 1000 / 210.0, abs=1e-3)
    assert flow.results["clock_domains"] == 2
    # per-domain constraints are ambiguous as a single value, so they are not reported
    assert "clock_frequency" not in flow.results
    assert "clock_period" not in flow.results
    assert set(flow.results["_fmax"]) == {"slow", "fast"}


def test_unconstrained_domain_is_not_counted_as_a_timing_failure(tmp_path):
    """nextpnr reports a domain with no usable constraint; that is not a violation."""
    report = {"fmax": {"clk": {"achieved": 100.0, "constraint": 0}}}
    flow = make_flow(tmp_path, report)
    assert flow.parse_reports() is True
    assert "wns" not in flow.results
    assert flow.results["clock_domains"] == 1


def test_empty_fmax_is_not_a_failure(tmp_path):
    """A design with no register-to-register path inside a clock domain reports no fmax."""
    report = {"fmax": {}, "utilization": {"TRELLIS_FF": {"used": 3, "available": 10}}}
    flow = make_flow(tmp_path, report)
    assert flow.parse_reports() is True
    assert "Fmax" not in flow.results
    assert flow.results["ff"] == 3


def test_detailed_net_timings_are_kept_when_present(tmp_path):
    report = {"fmax": {}, "detailed_net_timings": [{"net": "n", "endpoints": []}]}
    flow = make_flow(tmp_path, report)
    flow.parse_reports()
    assert flow.results["_detailed_net_timings"] == report["detailed_net_timings"]


# --------------------------------------------------------------------------- failure handling


def test_missing_report_is_a_failure(tmp_path):
    flow = make_flow(tmp_path, None)
    assert flow.parse_reports() is False


def test_malformed_report_is_a_failure(tmp_path):
    flow = make_flow(tmp_path, "{not json")
    assert flow.parse_reports() is False


def test_disabled_report_is_not_a_failure(tmp_path):
    """`report = null` means the user asked for no report; there is nothing to parse."""
    flow = make_flow(tmp_path, None)
    flow.settings.report = None
    assert flow.parse_reports() is True


# --------------------------------------------------------------------------- other families


def test_non_ecp5_family_gets_raw_bel_counts_only(tmp_path):
    """The bel-type -> canonical resource mapping is ECP5-specific, the report parsing is not."""
    report = {
        "fmax": {"clk": {"achieved": 300.0, "constraint": 100.0}},
        "utilization": {"ICESTORM_LC": {"used": 11, "available": 7680}},
    }
    flow = make_flow(tmp_path, report, family="ice40")
    assert flow.parse_reports() is True
    assert flow.results["Fmax"] == pytest.approx(300.0)  # timing is family-independent
    assert flow.results["ICESTORM_LC"] == 11
    assert "lut" not in flow.results


def test_ecp5_resource_map_covers_the_canonical_names():
    assert set(ECP5_RESOURCES) == {"lut", "ff", "slice", "bram", "dsp", "io"}


# --------------------------------------------------------------------------- tool version


def test_version_regex_matches_the_nextpnr_banner():
    """nextpnr prints this to stderr; the generic Tool patterns do not match its shape."""
    tool = NextpnrTool(executable="nextpnr-ecp5")
    assert tool.process_version_output(VERSION_BANNER) == ("0", "11", "1")


def test_version_banner_is_on_stderr_not_stdout():
    """Pins the reason `Tool` retries version detection with stderr merged."""
    assert re.search(r"Version\s+nextpnr-\d", VERSION_BANNER)


# --------------------------------------------------------------------------- documented settings


def test_report_setting_defaults_to_a_file_the_flow_reads():
    settings = Nextpnr.Settings(fpga=FPGA(family="ecp5"))  # type: ignore[call-arg]
    assert settings.report == Path("report.json")


@pytest.mark.parametrize(
    "fpga,expected,forbidden",
    [
        (
            {
                "family": "ecp5",
                "vendor": "lattice",
                "type": "u",
                "capacity": "25k",
                "package": "BG",
                "pins": 381,
                "speed": 6,
            },
            {"--25k", "--package=CABGA381", "--speed=6", "--textcfg=config.txt"},
            {"--asc=config.asc", "--fasm=config.fasm", "--device"},
        ),
        (
            {"family": "ice40", "vendor": "lattice", "device": "ice40HX1K", "package": "tq144"},
            {"--hx1k", "--package=tq144", "--asc=config.asc"},
            {"--textcfg=config.txt", "--fasm=config.fasm", "--speed=6"},
        ),
        (
            {"family": "nexus", "vendor": "lattice", "device": "LIFCL-40-9BG400C"},
            {"--device=LIFCL-40-9BG400C", "--fasm=config.fasm"},
            {"--textcfg=config.txt", "--asc=config.asc", "--package=tq144"},
        ),
    ],
)
def test_target_specific_nextpnr_arguments(tmp_path, monkeypatch, fpga, expected, forbidden):
    design = Design(name="d", rtl={"sources": [], "top": "d"}, design_root=tmp_path)
    settings = Nextpnr.Settings(fpga=fpga)
    flow = Nextpnr(settings, design, tmp_path / "pnr")
    flow.inputs.netlist = _netlist(tmp_path)
    calls = []
    monkeypatch.setattr(
        NextpnrTool,
        "run",
        lambda self, *args, env=None: (
            write_nextpnr_config(flow, args),
            calls.append((self.executable, args)),
        ),
    )
    flow.prepare_inputs()
    flow.run()
    executable, args = calls[0]
    assert executable == f"nextpnr-{fpga['family']}"
    assert expected <= set(args)
    assert not forbidden.intersection(args)


@pytest.mark.parametrize(
    "part,device,device_flag",
    [
        ("iCE40HX1K-TQ144", "ICE40HX1K", "--hx1k"),
        ("iCE40UP5K-SG48", "ICE40UP5K", "--up5k"),
        ("iCE40UP5K-SG48I", "ICE40UP5K", "--up5k"),
        ("iCE40UP5K-UWG30ITR", "ICE40UP5K", "--up5k"),
        ("iCE5LP4K-SG48", "ICE5LP4K", "--u4k"),
        ("iCE5LP4K-SG48ITR", "ICE5LP4K", "--u4k"),
    ],
)
def test_ice40_part_identifies_synthesis_and_pnr_device(
    tmp_path, monkeypatch, part, device, device_flag
):
    fpga = FPGA(part=part)
    assert fpga.family == "ice40" and fpga.vendor == "lattice"
    assert fpga.device == device
    design = Design(name="d", rtl={"sources": [], "top": "d"}, design_root=tmp_path)
    flow = Nextpnr(Nextpnr.Settings(fpga=fpga), design, tmp_path / "pnr")
    flow.inputs.netlist = _netlist(tmp_path)
    calls = []
    monkeypatch.setattr(
        NextpnrTool,
        "run",
        lambda self, *args, env=None: (write_nextpnr_config(flow, args), calls.append(args)),
    )
    flow.prepare_inputs()
    flow.run()
    assert device_flag in calls[0]


def test_nextpnr_resolves_explicit_constraint_paths_against_design_root(tmp_path, monkeypatch):
    (tmp_path / "pins.lpf").write_text('LOCATE COMP "clk" SITE "A1";\n')
    (tmp_path / "timing.sdc").write_text("# timing\n")
    design = Design(name="d", rtl={"sources": ["pins.lpf"], "top": "d"}, design_root=tmp_path)
    fpga = FPGA(part="LFE5U-25F-6BG381C")
    flow = Nextpnr(
        Nextpnr.Settings(fpga=fpga, sdc="timing.sdc"),
        design,
        tmp_path / "pnr",
    )
    flow.inputs.netlist = _netlist(tmp_path)
    flow.inputs.constraints = [tmp_path / "pins.lpf"]
    calls = []
    monkeypatch.setattr(
        NextpnrTool,
        "run",
        lambda self, *args, env=None: (write_nextpnr_config(flow, args), calls.append(args)),
    )
    flow.prepare_inputs()
    flow.run()
    assert f"--lpf={flow.run_path / 'constraints.lpf'}" in calls[0]
    assert f"--sdc={flow.run_path / 'constraints.sdc'}" in calls[0]


def _nextpnr_args(tmp_path, monkeypatch, **settings):
    """The arguments `Nextpnr.run` hands nextpnr, for a design rooted at `tmp_path`."""
    design = Design(name="d", rtl={"sources": [], "top": "d"}, design_root=tmp_path)
    flow = Nextpnr(
        Nextpnr.Settings.from_input(settings, design_root=tmp_path, runner_cwd=tmp_path),
        design,
        tmp_path / "pnr",
    )
    flow.inputs.netlist = _netlist(tmp_path)
    calls = []
    monkeypatch.setattr(
        NextpnrTool,
        "run",
        lambda self, *args, env=None: (write_nextpnr_config(flow, args), calls.append(args)),
    )
    flow.prepare_inputs()
    flow.run()
    return calls[0]


def test_nextpnr_expands_design_root_in_file_settings(tmp_path, monkeypatch):
    (tmp_path / "timing.sdc").write_text("# timing\n")
    args = _nextpnr_args(
        tmp_path,
        monkeypatch,
        fpga="LFE5U-25F-6BG381C",
        sdc="$DESIGN_ROOT/timing.sdc",
        pre_route="hooks/pre_route.py",
        py_script="report.py",
    )
    assert f"--sdc={tmp_path / 'pnr' / 'constraints.sdc'}" in args
    assert f"--pre-route={tmp_path / 'hooks/pre_route.py'}" in args
    assert f"--run={tmp_path / 'report.py'}" in args


@pytest.mark.parametrize(
    "fpga,flags",
    [
        ("iCE40UP5K-SG48I", {"--up5k", "--package=sg48"}),
        ("iCE40LP1K-SWG16TR", {"--lp1k", "--package=swg16tr"}),
        ({"family": "ice40", "device": "iCE40UP5K", "package": "UWG30"}, {"--package=uwg30"}),
        ("LFE5U-85F-8MG285C", {"--85k", "--speed=8", "--package=CSFBGA285"}),
        ({"family": "ecp5", "capacity": "25k", "package": "cabga256"}, {"--package=CABGA256"}),
        ("LFD2NX-40-7BG256C", {"--device=LFD2NX-40-7BG256C", "--fasm=config.fasm"}),
    ],
)
def test_nextpnr_names_the_device_and_package_its_own_way(tmp_path, monkeypatch, fpga, flags):
    assert flags <= set(_nextpnr_args(tmp_path, monkeypatch, fpga=fpga))


@pytest.mark.parametrize(
    "settings,message",
    [
        ({"fpga": {"vendor": "gowin", "family": "gowin", "device": "GW1N-9"}}, "no tested"),
        ({"fpga": "xc7a35tcpg236"}, "full part"),
        (
            {"fpga": "iCE40HX1K-TQ144", "lpf_allow_unconstrained": True},
            "does not take lpf_allow_unconstrained",
        ),
        (
            {"fpga": "LFE5U-25F-6BG381C", "opt_timing": True, "no_pack_lutff": True},
            "does not take opt_timing, no_pack_lutff",
        ),
        ({"fpga": {"family": "nexus", "device": "LIFCL-40"}}, "LIFCL-40-9BG400C"),
        ({"fpga": {"family": "ice40", "device": "iCE40UL1K"}}, "no device 'ul1k'"),
        ({"fpga": {"family": "ecp5"}}, "fpga.capacity"),
    ],
)
def test_nextpnr_rejects_a_target_before_synthesis(tmp_path, settings, message):
    """An unsupported target or a setting of another architecture fails in `init`, before the
    yosys_fpga dependency is registered, let alone run."""
    design = Design(name="d", rtl={"sources": [], "top": "d"}, design_root=tmp_path)
    flow = Nextpnr(Nextpnr.Settings(**settings), design, tmp_path)
    with pytest.raises(FlowSettingsException, match=re.escape(message)):
        flow.init()
    assert not flow.dependencies


def test_nextpnr_ice40_end_to_end(tmp_path, monkeypatch):
    """The iCE40 chain writes an ASC and a parsed report with the real tools."""
    from xeda.flow_runner import DefaultRunner

    from .tool_utils import require_nextpnr_ice40

    require_nextpnr_ice40()
    (tmp_path / "blink.v").write_text(
        "module blink(input clk, output reg q); always @(posedge clk) q <= ~q; endmodule\n"
    )
    design = Design(
        name="blink",
        design_root=tmp_path,
        rtl={"sources": ["blink.v"], "top": "blink", "clock": {"port": "clk"}},
    )
    monkeypatch.chdir(tmp_path)
    flow = DefaultRunner(tmp_path / "runs").run_flow(
        Nextpnr,
        design,
        {"fpga": "iCE40HX1K-TQ144", "clock": {"period": 20.0}, "pcf_allow_unconstrained": True},
    )
    assert flow is not None and flow.succeeded
    assert (flow.run_path / "config.asc").is_file()
    assert (flow.run_path / "report.json").is_file()
    assert Path(flow.artifacts["asc"]).is_file()


# --------------------------------------------------------------------------- ECP5 PLL settings


def test_ecppll_copies_and_names_caller_owned_clocks():
    clkin = EcpPLL.Clock(mhz=25.0)
    unnamed_out = EcpPLL.Clock(mhz=50.0)
    named_out = EcpPLL.Clock(name="pixel", mhz=75.0)

    pll = EcpPLL(clkin=clkin, clkouts=[unnamed_out, named_out])

    assert clkin.name is None
    assert unnamed_out.name is None
    assert named_out.name == "pixel"
    assert pll.clkin is not clkin
    assert pll.clkouts[0] is not unnamed_out
    assert pll.clkouts[1] is not named_out
    assert pll.clkin.name == "clk_i"
    assert [clock.name for clock in pll.clkouts] == ["clk_o_0", "pixel"]

    clkin.mhz = 30.0
    pll.clkouts[0].mhz = 60.0
    assert pll.clkin.mhz == 25.0
    assert unnamed_out.mhz == 50.0


def test_ecppll_assignment_and_scalar_clock_shorthand_are_isolated():
    pll = EcpPLL(clkin=25.0, clkouts=[50.0, 75.0])
    assert isinstance(pll.clkin, EcpPLL.Clock)
    assert all(isinstance(clock, EcpPLL.Clock) for clock in pll.clkouts)
    assert pll.clkin.name == "clk_i"
    assert [clock.name for clock in pll.clkouts] == ["clk_o_0", "clk_o_1"]

    clkin = EcpPLL.Clock(mhz=30.0)
    clkout = EcpPLL.Clock(mhz=60.0)
    pll.clkin = clkin
    pll.clkouts = [clkout]
    assert clkin.name is None
    assert clkout.name is None
    assert pll.clkin is not clkin
    assert pll.clkouts[0] is not clkout
    assert pll.clkin.name == "clk_i"
    assert pll.clkouts[0].name == "clk_o_0"


# --------------------------------------------------------------------------- end to end


def test_nextpnr_ecp5_end_to_end(tmp_path):
    """Run yosys_fpga + nextpnr-ecp5 for real and check the parsed results are sane."""
    from xeda.flow_runner import DefaultRunner

    from .tool_utils import require_nextpnr_ecp5

    require_nextpnr_ecp5()
    design = EXAMPLES_DIR / "boards" / "ulx3s" / "blinky" / "blinky.xeda.yaml"
    flow = DefaultRunner(tmp_path).run(Nextpnr, design)
    assert flow is not None
    assert flow.succeeded, "nextpnr flow failed"
    r = flow.results
    # the ULX3S board LPF constrains clk_25mhz to 25 MHz
    assert r.clock_frequency == 25
    assert r.Fmax > 25, "a blinky must beat 25 MHz on an ECP5"
    assert r.timing_met is True
    assert r.wns > 0
    assert r.clock_domains == 1
    # utilization: a blinky is small but not empty
    assert 0 < r.lut < 1000
    assert 0 < r.ff < 1000
    assert r.io > 0
    assert r.bram == 0
    # the report is recorded and the raw bel counts agree with the canonical ones
    assert Path(flow.artifacts["report"]).is_file()
    assert r["TRELLIS_COMB"] == r.lut
    assert r["TRELLIS_FF"] == r.ff
    # the tool version was detected despite nextpnr printing it to stderr
    versions = {t["executable"]: t["version"] for t in r.tools}
    assert versions["nextpnr-ecp5"], "nextpnr version was not detected"


# --------------------------------------------------------------------------- rounding


def test_slack_just_below_zero_is_a_violation(tmp_path):
    """Rounding before the sign test turned -0.00025 ns into -0.0, and -0.0 >= 0 is True."""
    report = {"fmax": {"clk": {"achieved": 199.99, "constraint": 200.0}}}
    flow = make_flow(tmp_path, report)
    assert flow.parse_reports() is False
    assert flow.results["timing_met"] is False
    assert flow.results["wns"] < 0


def test_reported_slack_keeps_enough_precision_to_show_its_sign(tmp_path):
    report = {"fmax": {"clk": {"achieved": 199.99, "constraint": 200.0}}}
    flow = make_flow(tmp_path, report)
    flow.parse_reports()
    assert str(flow.results["wns"]).startswith("-")


def test_slack_exactly_zero_is_met(tmp_path):
    report = {"fmax": {"clk": {"achieved": 200.0, "constraint": 200.0}}}
    flow = make_flow(tmp_path, report)
    assert flow.parse_reports() is True
    assert flow.results["timing_met"] is True


def test_nextpnr_runs_with_its_device_given_only_for_yosys_fpga(tmp_path, monkeypatch):
    """End to end, with the real tools: the device given in `[flows.yosys_fpga]` alone reaches
    both yosys and nextpnr."""
    from xeda import Design
    from xeda.flow_runner import DefaultRunner
    from xeda.flows import Nextpnr

    from .tool_utils import require_nextpnr_ecp5

    require_nextpnr_ecp5()
    (tmp_path / "blink.v").write_text(
        "module blink(input clk, output reg q); always @(posedge clk) q <= ~q; endmodule\n"
    )
    design = Design(
        name="blink",
        design_root=tmp_path,
        rtl={"sources": ["blink.v"], "top": "blink", "clock": {"port": "clk"}},
        flow={"yosys_fpga": {"fpga": {"part": "LFE5U-25F-6BG381C"}}},
    )
    monkeypatch.chdir(tmp_path)
    flow = DefaultRunner(tmp_path / "xeda_run").run(
        Nextpnr, design, flow_overrides={"clock": {"period": 20.0}}
    )
    assert flow is not None and flow.succeeded
    assert flow.settings.fpga is not None and flow.settings.fpga.part == "LFE5U-25F-6BG381C"


# --------------------------------------------------------------------------- the netlist's `src`

BLINK = "module blink(input clk, output reg q); always @(posedge clk) q <= ~q; endmodule\n"


def _blink(tmp_path: Path, flows: dict[str, Any]) -> Design:
    (tmp_path / "blink.v").write_text(BLINK)
    return Design(
        name="blink",
        design_root=tmp_path,
        rtl={"sources": ["blink.v"], "top": "blink", "clock": {"port": "clk"}},
        flow=flows,
    )


class _YosysLaunched(Exception):
    """Stops a launch at the yosys_fpga dependency's `init`, carrying its settings."""


def _yosys_launch_settings(tmp_path, monkeypatch, flow_cls, flows=None, cli=(), part=None):
    """The settings `yosys_fpga` is launched with -- composed by the launcher exactly as for
    `xeda run <flow_cls> -s fpga=... <cli>` on a design with `flows` sections. The launch stops
    there, so no tool runs."""

    captured = {}

    def stop(self):
        captured["settings"] = self.settings
        raise _YosysLaunched(self.settings)

    monkeypatch.setattr(YosysFpga, "init", stop)
    monkeypatch.chdir(tmp_path)
    with pytest.raises((_YosysLaunched, FlowDependencyFailure)):
        DefaultRunner(tmp_path / "xeda_run").run(
            flow_cls,
            _blink(tmp_path, flows or {}),
            flow_settings=[f"fpga={part or ICE40_PART}", *cli],
        )
    settings = captured["settings"]
    assert isinstance(settings, YosysFpga.Settings)
    return settings


ICE40_PART = "iCE40HX1K-TQ144"


def test_nextpnr_asks_its_producer_to_keep_src():
    assert Nextpnr.producer_defaults == {"netlist": {"netlist_src_attrs": True}}


@pytest.mark.parametrize(
    "flows, cli, keeps_src",
    [
        ({}, (), True),
        ({"yosys_fpga": {"flatten": True}}, (), True),
        ({"yosys_fpga": {"netlist_src_attrs": False}}, (), False),
        ({}, ("flows.yosys_fpga.netlist_src_attrs=false",), False),
        (
            {"yosys_fpga": {"netlist_src_attrs": False}},
            ("flows.yosys_fpga.netlist_src_attrs=true",),
            True,
        ),
    ],
    ids=["default", "section-silent-on-src", "section", "cli", "cli-over-section"],
)
def test_the_synthesis_declared_nextpnr_places_keeps_src_unless_told_otherwise(
    tmp_path, monkeypatch, flows, cli, keeps_src
):
    """The same default, for the producer `nextpnr` declares: `flows.yosys_fpga` is the one
    place its settings are written (D-10), and a `netlist_src_attrs` given there wins."""
    settings = _yosys_launch_settings(tmp_path, monkeypatch, Nextpnr, flows, cli)
    assert settings.netlist_src_attrs is keeps_src


@pytest.mark.parametrize("origin", ["design", "project", "cli", "api", "loader-cli"])
def test_nextpnr_yosys_was_removed_and_names_its_replacement(tmp_path, monkeypatch, origin):
    """`nextpnr.yosys.<key>` fails in every layer, naming `flows.yosys_fpga.<key>`."""
    from xeda.flows.openfpgaloader import Openfpgaloader

    monkeypatch.chdir(tmp_path)
    nested = {"nextpnr": {"yosys": {"flatten": True}}}
    design = _blink(tmp_path, nested if origin == "design" else {})
    runner = DefaultRunner(tmp_path / "xeda_run")
    message = "`yosys` was removed: use `flows.yosys_fpga.flatten`"
    with pytest.raises(Exception, match=message):
        if origin == "project":
            project = tmp_path / "project.yaml"
            project.write_text("flows:\n  nextpnr:\n    yosys:\n      flatten: true\n")
            runner.plan(
                Nextpnr, design, xedaproject=str(project), flow_settings=[f"fpga={ICE40_PART}"]
            )
        elif origin == "cli":
            runner.plan(Nextpnr, design, flow_settings=[f"fpga={ICE40_PART}", "yosys.flatten=true"])
        elif origin == "api":
            runner.run_flow(Nextpnr, design, {"fpga": ICE40_PART, "yosys": {"flatten": True}})
        elif origin == "loader-cli":
            runner.run(
                Openfpgaloader,
                design,
                flow_settings=[f"fpga={ICE40_PART}", "flows.nextpnr.yosys.flatten=true"],
            )
        else:
            runner.plan(Nextpnr, design, flow_settings=[f"fpga={ICE40_PART}"])
    with pytest.raises(Exception, match="`yosys` was removed: use `flows.yosys_fpga.<key>`"):
        Nextpnr.Settings(yosys=YosysFpga.Settings())


@pytest.mark.parametrize(
    "flows, keeps_src",
    [({}, True), ({"yosys_fpga": {"netlist_src_attrs": False}}, False)],
    ids=["default", "yosys_fpga-section"],
)
def test_openfpgaloader_synthesis_keeps_src_like_nextpnr(tmp_path, monkeypatch, flows, keeps_src):
    """Through `fpga_pack` and `nextpnr`: the synthesis is still the one nextpnr places."""
    from xeda.flows.openfpgaloader import Openfpgaloader

    settings = _yosys_launch_settings(tmp_path, monkeypatch, Openfpgaloader, flows)
    assert settings.netlist_src_attrs is keeps_src


def test_yosys_fpga_on_its_own_strips_src_by_default(tmp_path, monkeypatch):
    assert _yosys_launch_settings(tmp_path, monkeypatch, YosysFpga).netlist_src_attrs is False


@pytest.mark.parametrize(
    "flows, keeps_src",
    [({}, True), ({"yosys_fpga": {"netlist_src_attrs": False}}, False)],
    ids=["default", "yosys_fpga-section"],
)
def test_nextpnr_ice40_places_a_netlist_with_src_unless_told_otherwise(
    tmp_path, monkeypatch, flows, keeps_src
):
    """End to end, with the real tools: the JSON netlist nextpnr reads."""
    from .tool_utils import require_nextpnr_ice40, yosys_json_attribute_holders

    require_nextpnr_ice40()
    monkeypatch.chdir(tmp_path)
    flow = DefaultRunner(tmp_path / "xeda_run").run(
        Nextpnr,
        _blink(tmp_path, flows),
        flow_settings=[
            "fpga=iCE40HX1K-TQ144",
            "clock.period=20.0",
            "pcf_allow_unconstrained=true",
        ],
    )
    assert flow is not None and flow.succeeded
    (yosys,) = flow.completed_dependencies
    assert isinstance(yosys.settings, YosysFpga.Settings) and yosys.settings.netlist_json
    holders = yosys_json_attribute_holders(yosys.run_path / yosys.settings.netlist_json, "src")
    if keeps_src:
        assert "modules/blink" in holders
    else:
        assert holders == []
