import csv
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path

import pytest

from xeda import Design
from xeda.flow import FPGA
from xeda.flow_runner import DefaultRunner
from xeda.flows import VivadoAltSynth, VivadoSim, VivadoSynth
from xeda.flows.vivado.vivado_synth import parse_hier_util, vivado_synth_generics

from .tool_utils import fake_calls, use_fake_tools

TESTS_DIR = Path(__file__).parent.absolute()
RESOURCES_DIR = TESTS_DIR / "resources"
EXAMPLES_DIR = TESTS_DIR.parent / "examples"


def test_vivado_synth_template() -> None:
    design = Design.from_toml(RESOURCES_DIR / "design0/design0.toml")
    settings = VivadoSynth.Settings(fpga=FPGA(part="abcd"), clock_period=5.5)  # type: ignore
    run_dir = Path.cwd() / "vivado_synth_run"
    run_dir.mkdir(exist_ok=True)
    flow = VivadoSynth(settings, design, run_dir)  # type: ignore
    tcl_file = flow.copy_from_template(
        "vivado_synth.tcl",
        xdc_files=[],
        tcl_files=[],
        generics=vivado_synth_generics(design.rtl.parameters),
        impl_to_step="route_design",
        run_status_file=run_dir / "reports" / "run_status.txt",
    )
    with open(run_dir / tcl_file) as f:
        vivado_tcl = f.read()
    expected_lines = [
        """set_property generic {G_IN_WIDTH=32 G_ITERATIVE=1'b1 G_STR=\\"abcd\\" G_BITVECTOR=7'b0101001} [current_fileset]""",
    ]
    for line in expected_lines:
        assert line in vivado_tcl


def test_vivado_synth_py() -> None:
    # Append to PATH so if the actual tool exists, would take precedences.
    os.environ["PATH"] = (
        os.path.join(TESTS_DIR, "fake_tools") + os.pathsep + os.environ.get("PATH", "")
    )
    design = Design.from_toml(EXAMPLES_DIR / "vhdl" / "sqrt" / "sqrt.toml")
    settings = dict(fpga=FPGA("xc7a12tcsg325-1"), clock_period=5.5)
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as run_dir:
        print("Xeda run dir: ", run_dir)
        xeda_runner = DefaultRunner(run_dir, debug=True)
        flow = xeda_runner.run_flow(VivadoSynth, design, settings)
        assert flow is not None, "run_flow returned None"
        settings_json = flow.run_path / "settings.json"
        results_json = flow.run_path / "results.json"
        assert settings_json.exists()
        assert results_json.exists()
        assert flow.succeeded
        assert 0.3 < flow.results.runtime  # type: ignore


# (slack, total datapath delay) of the 10 worst setup paths of a real single-clock
# design, in the order Vivado hands them back (ascending slack). Slack order and
# delay order disagree because slack also folds in clock skew and the setup
# requirement of the endpoint, which differ from path to path.
REAL_PATHS: Sequence[tuple[str, str]] = [
    ("0.041", "4.002"),
    ("0.044", "3.999"),
    ("0.047", "4.274"),
    ("0.050", "4.384"),
    ("0.055", "3.869"),
    ("0.062", "4.184"),
    ("0.076", "3.908"),
    ("0.088", "4.463"),
    ("0.147", "4.137"),
    ("0.149", "4.104"),
]


TCLSH = shutil.which("tclsh")
needs_tclsh = pytest.mark.skipif(not TCLSH, reason="tclsh is needed to run the TCL templates")


def _run_report(
    tmp_dir: Path,
    proc: str,
    args: str = "",
    paths: Sequence[tuple[str, str]] = REAL_PATHS,
) -> list[dict]:
    """Render util.tcl, run one of its report procedures under tclsh against fake
    timing paths, and return the rows of the CSV it produced. `args` are the
    procedure's arguments after the file name; it defaults to asking for every
    registered path."""
    assert TCLSH
    design = Design.from_toml(RESOURCES_DIR / "design0/design0.toml")
    settings = VivadoSynth.Settings(fpga=FPGA(part="abcd"), clock_period=5.5)  # type: ignore
    flow = VivadoSynth(settings, design, tmp_dir)  # type: ignore
    util_tcl = tmp_dir / flow.copy_from_template("util.tcl")

    out_csv = tmp_dir / "paths.csv"
    driver = tmp_dir / "driver.tcl"
    add_paths = "\n".join(
        f"addpath p{i} {slack if slack else '{}'} {delay if delay else '{}'}"
        for i, (slack, delay) in enumerate(paths)
    )
    driver.write_text(
        f"source {{{RESOURCES_DIR / 'vivado_report_critical_paths.tcl'}}}\n"
        f"source {{{util_tcl}}}\n"
        f"{add_paths}\n"
        f"{proc} {{{out_csv}}} {args or len(paths)}\n"
    )
    subprocess.run([TCLSH, str(driver)], check=True, cwd=tmp_dir)
    with open(out_csv, newline="") as f:
        return list(csv.DictReader(f))


EXPECTED_COLUMNS = [
    "Startpoint",
    "StartClock",
    "Endpoint",
    "EndClock",
    "PathGroup",
    "Requirement",
    "Slack",
    "Levels",
    "MaxFanout",
    "LogicDelay",
    "NetDelay",
    "TotalDelay",
    "Skew",
    "Uncertainty",
]


@needs_tclsh
def test_report_critical_paths_keeps_vivado_slack_order() -> None:
    """`reportCriticalPaths` writes the paths exactly as Vivado returns them:
    worst (lowest) slack first, with no re-ordering of its own."""
    with tempfile.TemporaryDirectory() as tmp:
        rows = _run_report(Path(tmp).resolve(), "reportCriticalPaths")

    assert len(rows) == len(REAL_PATHS), "every requested path must be reported"
    assert [(r["Slack"], r["TotalDelay"]) for r in rows] == list(REAL_PATHS)
    assert [r["Endpoint"] for r in rows] == [f"end_p{i}" for i in range(len(REAL_PATHS))]


@needs_tclsh
def test_report_critical_paths_by_delay_is_sorted_by_descending_delay() -> None:
    """`reportCriticalPathsByDelay` reports the longest paths, longest first.

    Vivado has no delay sort (`-sort_by` takes only slack or group), so the
    procedure ranks a wider candidate set itself. The four longest paths here are
    *not* the four worst by slack, so slack-ordered output cannot pass this.
    """
    with tempfile.TemporaryDirectory() as tmp:
        rows = _run_report(Path(tmp).resolve(), "reportCriticalPathsByDelay")

    delays = [float(r["TotalDelay"]) for r in rows]
    assert delays == sorted(float(d) for _, d in REAL_PATHS)[::-1]
    assert delays[0] == 4.463, "the longest path must come first"
    # the longest paths are drawn from the whole candidate set, not just the
    # leading (worst-slack) entries of it
    assert [r["Endpoint"] for r in rows[:4]] == ["end_p7", "end_p3", "end_p2", "end_p5"]


@needs_tclsh
def test_report_critical_paths_by_delay_honors_num_paths_and_candidates() -> None:
    """num_paths caps the report; num_candidates caps how wide a set is ranked."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp).resolve()
        # 3 longest of the whole design (candidate default is 10x num_paths)
        widest = _run_report(tmp_dir, "reportCriticalPathsByDelay", "3")
        # 3 longest among only the 4 worst-by-slack paths
        narrow = _run_report(tmp_dir, "reportCriticalPathsByDelay", "3 4")

    assert [r["TotalDelay"] for r in widest] == ["4.463", "4.384", "4.274"]
    assert [r["TotalDelay"] for r in narrow] == ["4.384", "4.274", "4.002"]


@needs_tclsh
def test_reports_share_the_same_columns() -> None:
    """Both reports must carry an identical set of fields."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp).resolve()
        by_slack = _run_report(tmp_dir, "reportCriticalPaths")
        by_delay = _run_report(tmp_dir, "reportCriticalPathsByDelay")

    assert list(by_slack[0]) == EXPECTED_COLUMNS
    assert list(by_delay[0]) == EXPECTED_COLUMNS
    # the derived columns line up: logic + net == total
    for row in by_slack:
        assert float(row["LogicDelay"]) + float(row["NetDelay"]) == pytest.approx(
            float(row["TotalDelay"]), abs=1e-3
        )


def test_num_critical_paths_setting_reaches_the_templates() -> None:
    """`num_critical_paths` drives how many paths every critical path report asks
    for, in both the project-based and the alternate synthesis flow."""
    from xeda.flows import VivadoAltSynth
    from xeda.flows.vivado.vivado_alt_synth import flatten_options

    design = Design.from_toml(RESOURCES_DIR / "design0/design0.toml")
    assert VivadoSynth.Settings(fpga=FPGA(part="abcd"), clock_period=5.5).num_critical_paths == 100  # type: ignore

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp).resolve()
        settings = VivadoSynth.Settings(  # type: ignore
            fpga=FPGA(part="abcd"), clock_period=5.5, num_critical_paths=7
        )
        flow = VivadoSynth(settings, design, tmp_dir)  # type: ignore
        hook = tmp_dir / flow.copy_from_template(
            "post_step_hook.tcl", run_dir=tmp_dir, user_hooks=[], step="route_design", outputs={}
        )
        hook_tcl = hook.read_text()
        reports = "${xeda_reports_dir}"
        assert f"reportCriticalPaths [file join {reports} critical_paths.csv] 7" in hook_tcl
        assert (
            f"reportCriticalPathsByDelay [file join {reports} critical_paths_by_delay.csv] 7"
            in hook_tcl
        )

        alt_settings = VivadoAltSynth.Settings(  # type: ignore
            fpga=FPGA(part="abcd"), clock_period=5.5, num_critical_paths=7
        )
        alt_flow = VivadoAltSynth(alt_settings, design, tmp_dir)  # type: ignore
        alt_flow.init()  # registers the vivado_generics / vivado_defines filters
        alt_flow.add_template_filter("flatten_options", flatten_options)
        alt = tmp_dir / alt_flow.copy_from_template(
            "vivado_alt_synth.tcl", xdc_files=[], tcl_files=[], generics=[]
        )
        assert "set num_max_paths 7" in alt.read_text()


@needs_tclsh
def test_report_critical_paths_by_delay_tolerates_unconstrained_paths() -> None:
    """Unconstrained paths come back with an empty SLACK and can have no delay at
    all (seen in real post-synth reports); they must not abort the ranking."""
    paths = [("", "5.528"), ("", ""), *REAL_PATHS]
    with tempfile.TemporaryDirectory() as tmp:
        rows = _run_report(Path(tmp).resolve(), "reportCriticalPathsByDelay", paths=paths)

    assert len(rows) == len(paths)
    assert rows[0]["TotalDelay"] == "5.528", "the longest path must come first"
    assert rows[-1]["TotalDelay"] == "", "a path with no delay ranks last"
    delays = [float(r["TotalDelay"]) for r in rows if r["TotalDelay"]]
    assert delays == sorted(delays, reverse=True), f"not in descending delay order: {delays}"


ALT_SYNTH_OUTPUTS = (
    "checkpoint_synth",
    "checkpoint_place",
    "checkpoint_route",
    "netlist",
    "netlist_timing",
    "sdf",
    "xdc_exported",
)


@needs_tclsh
@pytest.mark.parametrize("enabled", [True, False], ids=["written", "not_written"])
def test_vivado_alt_synth_records_the_checkpoints_and_netlists_it_writes(
    enabled, tmp_path, monkeypatch
) -> None:
    """`vivado_alt_synth.tcl` writes three checkpoints under `write_checkpoint`, and the
    functional and timing netlists, SDF and XDC under `write_netlist`; `VivadoAltSynth` declared
    none of them. Each is recorded exactly when the script writes it."""
    use_fake_tools(monkeypatch)
    design = Design.from_toml(RESOURCES_DIR / "design0/design0.toml")
    settings = {
        "fpga": "xc7a12tcsg325-1",
        "clock": {"period": 5.5},
        "write_checkpoint": enabled,
        "write_netlist": enabled,
    }
    flow = DefaultRunner(tmp_path / "run").run_flow(VivadoAltSynth, design, settings)
    assert flow is not None and flow.succeeded
    ss = flow.settings
    assert isinstance(ss, VivadoAltSynth.Settings)
    checkpoints, outputs = ss.checkpoints_dir, ss.outputs_dir
    written = {
        # Vivado's `write_checkpoint` adds the `.dcp` suffix the script leaves off
        f"checkpoint_{step}": (
            checkpoints / f"post_{step}.dcp",
            ["write_checkpoint", "-force", f"{checkpoints}/post_{step}"],
        )
        for step in ("synth", "place", "route")
    } | {
        "netlist": (
            outputs / "impl_funcsim.v",
            ["write_verilog", "-mode", "funcsim", "-force", f"{outputs}/impl_funcsim.v"],
        ),
        "netlist_timing": (
            outputs / "impl_timesim.v",
            ["write_verilog", "-mode", "timesim", "-sdf_anno", "false", "-force", "-file"]
            + [f"{outputs}/impl_timesim.v"],
        ),
        "sdf": (
            outputs / "impl_timesim.sdf",
            ["write_sdf", "-mode", "timesim", "-process_corner", "slow", "-force", "-file"]
            + [f"{outputs}/impl_timesim.sdf"],
        ),
        "xdc_exported": (
            outputs / "impl.xdc",
            ["write_xdc", "-no_fixed_only", "-force", f"{outputs}/impl.xdc"],
        ),
    }
    assert set(written) == set(ALT_SYNTH_OUTPUTS)
    calls = fake_calls(flow.run_path)
    for _, call in written.values():
        assert (call in calls) == enabled, call
    recorded = {
        label: flow.artifacts[label] for label in ALT_SYNTH_OUTPUTS if label in flow.artifacts
    }
    assert recorded == ({label: path for label, (path, _) in written.items()} if enabled else {})


def _vivado_sim_design(root: Path) -> Design:
    """A minimal VHDL design with a plain (non-cocotb) testbench, for `vivado_sim`."""
    (root / "rtl").mkdir(parents=True)
    (root / "tb").mkdir(parents=True)
    (root / "rtl" / "top.vhd").write_text(
        "library ieee; use ieee.std_logic_1164.all;\n"
        "entity top is port(a: in std_logic; y: out std_logic); end;\n"
        "architecture rtl of top is begin y <= not a; end;\n"
    )
    (root / "tb" / "tb_top.vhd").write_text(
        "library ieee; use ieee.std_logic_1164.all;\n"
        "entity tb_top is end;\n"
        "architecture sim of tb_top is\n"
        "  signal a, y: std_logic := '0';\n"
        "begin\n"
        "  uut: entity work.top port map(a, y);\n"
        "end;\n"
    )
    return Design(
        name="simdesign",
        design_root=root,
        rtl={"sources": ["rtl/top.vhd"], "top": "top"},
        tb={"sources": ["tb/tb_top.vhd"], "top": "tb_top", "uut": "uut"},
    )


@needs_tclsh
def test_vivado_sim_records_vcd_and_saif_as_artifacts(tmp_path, monkeypatch) -> None:
    """`vivado_sim.tcl` writes VCD/SAIF outputs whenever `vcd`/`saif` is set, but
    `VivadoSim.run()` declared neither -- the direct cause of `vivado_power` having to guess the
    SAIF path (`postsynth_sim_flow.run_path / settings.saif`) instead of reading a declared
    output."""
    use_fake_tools(monkeypatch)
    design = _vivado_sim_design(tmp_path / "design")
    settings = {"vcd": True, "saif": "switching.saif"}
    run_dir = tmp_path / "run"
    flow = DefaultRunner(run_dir).run_flow(VivadoSim, design, settings)
    assert flow is not None and flow.succeeded
    assert flow.artifacts.vcd == "dump.vcd"
    assert flow.artifacts.saif == "switching.saif"

    calls = fake_calls(flow.run_path)
    assert ["open_vcd", "dump.vcd"] in calls
    assert ["open_saif", "switching.saif"] in calls


@needs_tclsh
def test_vivado_sim_does_not_record_vcd_or_saif_when_disabled(tmp_path, monkeypatch) -> None:
    """Neither artifact is declared when `vcd`/`saif` are unset, matching what the template
    actually writes."""
    use_fake_tools(monkeypatch)
    design = _vivado_sim_design(tmp_path / "design")
    run_dir = tmp_path / "run"
    flow = DefaultRunner(run_dir).run_flow(VivadoSim, design, {})
    assert flow is not None and flow.succeeded
    assert "vcd" not in flow.artifacts
    assert "saif" not in flow.artifacts


def test_parse_hier_util() -> None:
    d = parse_hier_util("tests/resources/vivado_synth/hierarchical_utilization.xml")
    # print(json.dumps(d, indent=2))
    with open("hier.json", "w") as f:
        json.dump(d, f, indent=4)
    assert d


if __name__ == "__main__":
    # test_vivado_synth_py()
    test_parse_hier_util()
