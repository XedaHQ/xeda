"""Every artifact of a run has a documented key, and a declared output is never one.

An artifact is one file in the run directory that a run keeps about its process: a log, a report,
a waveform. Each flow documents its keys in `artifacts_description`. `log` is the output of the
main tool of the flow, and `<name>_log` that of every other program that the flow starts. The
tests run the flows under the process fakes of `tests/fake_tools` and compare each record with the
documentation of its flow.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import ClassVar

import pytest
from click.testing import CliRunner

from xeda import Design
from xeda.cli import cli
from xeda.flow import Flow, MetricSpec
from xeda.flow.flow import registered_flows
from xeda.flow.io import declared_outputs
from xeda.flow.metrics import (
    ARTIFACT_KEY,
    ArtifactSpec,
    describe_artifacts,
    log_artifact_key,
    program_logs,
)
from xeda.flow_runner import DefaultRunner
from xeda.introspect import all_flow_classes, results_info
from xeda.records import RunRecord, read_record
from xeda.tool import Tool

from . import tool_utils
from .flow_registry import preserved_flow_registry
from .test_artifacts import EXAMPLE, FAKE_TOOL_FLOWS, _sqrt_with_a_vhdl_testbench
from .tool_utils import use_fake_tools

FLOWS = all_flow_classes()
FLOW_IDS = [cls.name for cls in FLOWS]
#: The flows that start a program, which is every flow but the abstract bases.
FLOWS_WITH_PROGRAMS = [cls for cls in FLOWS if cls.executables]


# --------------------------------------------------------------------------- the documentation


@pytest.mark.parametrize("flow_class", FLOWS, ids=FLOW_IDS)
def test_every_artifact_key_is_documented_and_fits_the_grammar(flow_class):
    for key, spec in flow_class.artifacts_description.items():
        assert ARTIFACT_KEY.fullmatch(key), f"{flow_class.name}: {key!r}"
        assert isinstance(spec, ArtifactSpec), f"{flow_class.name}: {key}"
        assert spec.description.strip() and spec.when.strip(), f"{flow_class.name}: {key}"


@pytest.mark.parametrize(
    "flow_class", FLOWS_WITH_PROGRAMS, ids=[c.name for c in FLOWS_WITH_PROGRAMS]
)
def test_a_flow_that_starts_a_tool_documents_log(flow_class):
    assert "log" in flow_class.artifacts_description


@pytest.mark.parametrize(
    "flow_class", FLOWS_WITH_PROGRAMS, ids=[c.name for c in FLOWS_WITH_PROGRAMS]
)
def test_a_documented_program_log_belongs_to_a_program_of_the_flow(flow_class):
    """A key `<name>_log` names the log of a program in `executables`: a misspelled key would
    document a log that no run writes. `simulation_log` is the log of a program that the run
    builds."""
    programs = {log_artifact_key(program) for program in flow_class.executables}
    logs = {
        key
        for key in flow_class.artifacts_description
        if key.endswith("_log") and key != "simulation_log"
    }
    assert logs <= programs, f"{flow_class.name}: {sorted(logs - programs)}"


def test_a_flow_that_builds_a_simulation_documents_its_log():
    names = {"bsc_sim", "verilator", "vcs", "yosys_sim"}
    assert names <= {cls.name for cls in FLOWS}
    for cls in FLOWS:
        if cls.name in names:
            assert "simulation_log" in cls.artifacts_description, cls.name


def test_no_artifact_key_is_the_name_of_a_declared_output_of_its_flow():
    for cls in FLOWS:
        clash = set(cls.artifacts_description) & set(declared_outputs(cls))
        assert not clash, f"{cls.name}: {sorted(clash)}"


def test_the_key_of_a_program_log_is_lower_snake_case_without_its_suffix():
    assert log_artifact_key("klayout") == "klayout_log"
    assert log_artifact_key("c++") == "c_log"
    assert log_artifact_key("fpga-as") == "fpga_as_log"
    assert log_artifact_key("/opt/tools/bin/Vlogan.exe") == "vlogan_log"
    assert log_artifact_key("./simv") == "simv_log"


@pytest.mark.parametrize(
    "program, key",
    [
        ("7z", "tool_7z_log"),
        ("++", "tool_log"),
        ("c++", "c_log"),
        ("_x", "x_log"),
        ("3d.exe", "tool_3d_log"),
    ],
)
def test_the_key_of_a_program_log_always_fits_the_grammar(program, key):
    assert log_artifact_key(program) == key
    assert ARTIFACT_KEY.fullmatch(key)


def test_a_description_checks_its_keys_and_values():
    spec = ArtifactSpec("A thing.", "always")
    assert describe_artifacts("log", thing=spec)["thing"] is spec
    with pytest.raises(KeyError, match="No shared description"):
        describe_artifacts("nothing")
    with pytest.raises(ValueError, match="lower snake case"):
        describe_artifacts(**{"Bad-Key": spec})
    with pytest.raises(TypeError, match="ArtifactSpec"):
        describe_artifacts(thing="A thing.")  # type: ignore[arg-type]
    assert set(program_logs("g++", "klayout", when="never")) == {"g_log", "klayout_log"}


@pytest.mark.parametrize("flow_class", FLOWS, ids=FLOW_IDS)
def test_results_info_lists_each_artifact_with_its_condition(flow_class):
    listed = results_info(flow_class)["artifacts"]
    assert [item["key"] for item in listed] == list(flow_class.artifacts_description)
    assert all(set(item) == {"key", "description", "when"} for item in listed)


def test_list_results_shows_the_artifacts_as_a_table_and_as_json():
    runner = CliRunner()
    document = json.loads(runner.invoke(cli, ["list-results", "nextpnr", "--json"]).stdout)
    keys = [item["key"] for item in document["artifacts"]]
    assert keys[0] == "log" and {"report", "placed_svg", "routed_svg", "sdf"} <= set(keys)
    table = runner.invoke(cli, ["list-results", "nextpnr"]).output
    assert "nextpnr artifacts" in table and "placed_svg" in table


# --------------------------------------------------------------------------- the records of runs


def record_of_run(run_path: Path) -> RunRecord:
    return read_record(run_path, run_root=run_path.parent.parent)


def recorded_paths(record: RunRecord) -> dict[str, Path]:
    return {key: record.run_dir / name for key, name in record.artifacts.items()}


def check_record(flow_class: type[Flow], record: RunRecord) -> None:
    """The artifacts of `record` are documented, files, and no declared output of the flow."""
    documented = flow_class.artifacts_description
    assert set(record.artifacts) <= set(documented), (
        f"{flow_class.name} recorded undocumented artifacts: "
        f"{sorted(set(record.artifacts) - set(documented))}"
    )
    paths = recorded_paths(record)
    assert all(path.is_file() for path in paths.values()), paths
    outputs = {
        (record.run_dir / name).resolve()
        for output in record.outputs.values()
        for name in output.names
    }
    repeated = {key for key, path in paths.items() if path.resolve() in outputs}
    assert not repeated, f"{flow_class.name} repeats declared outputs as artifacts: {repeated}"
    if flow_class.executables:
        assert "log" in record.artifacts, f"{flow_class.name} recorded no log"
        assert record.artifacts["log"].startswith("logs/")


def _design_for(flow_name: str, tmp_path: Path) -> Design:
    if flow_name == "vivado_sim":
        return _sqrt_with_a_vhdl_testbench(tmp_path / "design")
    return Design.from_file(EXAMPLE)


#: Settings that switch the optional files of a flow on, for the fake runs.
FULL = {
    "vivado_synth": {
        "fpga": "xc7a12tcsg325-1",
        "clock": {"period": "10 ns"},
        "bitstream": "o/top.bit",
        "write_netlist": True,
        "write_timing_netlist": True,
        "write_checkpoint": True,
    },
    "vivado_alt_synth": {
        "fpga": "xc7a12tcsg325-1",
        "clock": {"period": "10 ns"},
        "write_netlist": True,
        "write_timing_netlist": True,
        "write_checkpoint": True,
    },
}


@pytest.mark.parametrize("flow_name", sorted(set(FAKE_TOOL_FLOWS) | set(FULL)))
def test_a_run_under_the_fake_tools_records_only_documented_artifacts(
    flow_name, tmp_path, monkeypatch
):
    use_fake_tools(monkeypatch)
    (tmp_path / "cells.db").write_text("")
    monkeypatch.chdir(tmp_path)
    settings = FULL.get(flow_name) or FAKE_TOOL_FLOWS[flow_name][0]
    runner = DefaultRunner(tmp_path / "run", display_results=False)

    try:  # the fakes write no reports, so a flow that reads them may fail
        runner.launch_flow(flow_name, _design_for(flow_name, tmp_path), settings)
    except Exception:  # noqa: BLE001 - the record of the failed run is what the test reads
        pass

    record = record_of_run(tmp_path / "run" / "sqrt" / flow_name)
    check_record(registered_flows[flow_name][1], record)


# The three open-source FPGA flows run in a chain under the fake yosys, nextpnr and packers.
FAMILIES = [
    ("LFE5U-25F-6BG381C", "nextpnr-ecp5", "ecppack"),
    ("iCE40HX1K-TQ144", "nextpnr-ice40", "icepack"),
]


@pytest.mark.parametrize("part, placer, packer", FAMILIES, ids=["ecp5", "ice40"])
def test_a_run_writes_only_documented_artifacts(part, placer, packer, tmp_path, monkeypatch):
    tool_utils.use_fake_fpga_tools(monkeypatch, tmp_path / "toolchain")
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "design"
    root.mkdir()
    (root / "top.v").write_text("module top(input clk, output q); assign q = clk; endmodule\n")
    design = Design(name="top", design_root=root, rtl={"sources": ["top.v"], "top": "top"})
    runner = DefaultRunner(tmp_path / "run", display_results=False)

    flow = runner.run(
        "fpga_pack",
        design,
        flow_settings={"fpga": part, "flows.nextpnr.placed_svg": "placed.svg"},
    )

    assert flow is not None and flow.succeeded
    records = {
        name: record_of_run(tmp_path / "run" / "top" / name)
        for name in ("yosys_fpga", "nextpnr", "fpga_pack")
    }
    for name, record in records.items():
        check_record(registered_flows[name][1], record)
    assert "netlist_json" not in records["yosys_fpga"].artifacts
    assert records["yosys_fpga"].artifacts["log"] == "logs/yosys.log"
    # the log of the main tool of nextpnr is the one of the program of the family
    assert records["nextpnr"].artifacts["log"] == f"logs/{placer}.log"
    assert records["nextpnr"].artifacts["report"] == "report.json"
    assert "nextpnr_log" not in records["nextpnr"].artifacts
    assert records["fpga_pack"].artifacts["log"] == f"logs/{packer}.log"
    # nextpnr's own `--log` file is a working file, the configuration is a declared output
    assert "nextpnr.log" not in records["nextpnr"].artifacts.values()
    assert not {"config", "textcfg", "asc"} & set(records["nextpnr"].artifacts)
    assert "bitstream" not in records["fpga_pack"].artifacts


def test_a_simulation_under_the_fake_vsim_records_the_log_of_its_main_tool(tmp_path, monkeypatch):
    from .test_modelsim import _design

    tool_utils.require_tclsh()
    use_fake_tools(monkeypatch)
    runner = DefaultRunner(tmp_path / "run", display_results=False)

    flow = runner.launch_flow("modelsim", _design(tmp_path / "design"), {})

    assert flow.succeeded
    check_record(type(flow), flow.record)
    assert flow.record.artifacts["log"] == "logs/vsim.log"


def test_the_log_of_a_failed_main_tool_is_the_log_that_the_error_names(tmp_path, monkeypatch):
    use_fake_tools(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "launch_runs")
    runner = DefaultRunner(tmp_path / "run", display_results=False)

    flow = runner.launch_flow("vivado_synth", Design.from_file(EXAMPLE), FULL["vivado_synth"])

    assert not flow.succeeded
    record = flow.record
    assert record.error is not None
    assert record.error.log == "log" and record.artifacts["log"] == "logs/vivado.log"
    assert (record.run_dir / record.artifacts["log"]).is_file()


def _program_flow(runs: int, passes: bool) -> type[Flow]:
    """A flow that starts its one program `runs` times, and then reports `passes`."""

    class ProgramFlow(Flow):
        """Starts a program, and fails from its checks and not from the program."""

        executables = (sys.executable,)
        results_description: ClassVar[dict[str, MetricSpec]] = {}
        artifacts_description = describe_artifacts("log")

        def run(self) -> None:
            for _ in range(runs):
                Tool(sys.executable, version_flag=None).run("-c", "print('done')")

        def parse_reports(self) -> bool:
            return passes

    return ProgramFlow


@pytest.mark.parametrize(
    "runs, log", [(1, "log"), (0, None)], ids=["the flow wrote a log", "the flow wrote none"]
)
def test_a_failure_without_a_process_names_the_log_when_the_flow_wrote_one(tmp_path, runs, log):
    design = Design(name="toy", design_root=tmp_path, rtl={"sources": [], "top": "t"})
    with preserved_flow_registry():
        flow_class = _program_flow(runs, passes=False)
        flow = DefaultRunner(tmp_path / "run", display_results=False).launch_flow(
            flow_class, design, {}
        )

    assert not flow.succeeded
    assert flow.record.error is not None and flow.record.error.code == "reported_failure"
    assert flow.record.error.log == log
    assert ("log" in flow.record.artifacts) is (runs > 0)


# --------------------------------------------------------------------------- a secondary tool


def test_a_failing_secondary_tool_points_at_its_own_log(tmp_path, monkeypatch):
    """The last OpenROAD stage starts `openroad` and then `klayout`. A failure of KLayout is to
    be read in the log of KLayout, not in the one of OpenROAD."""
    from .test_openroad_final import fake_klayout
    from .test_openroad_flows import launch, run_dir
    from .openroad_utils import register_openroad_flows

    tool_utils.require_tclsh()
    tool_utils.use_fake_openroad(monkeypatch)
    fake_klayout(tmp_path, monkeypatch)
    monkeypatch.setenv("KLAYOUT_FAKE_ERROR", "[ERROR] Found orphan cell 'X'")
    with register_openroad_flows():
        launch(tmp_path, monkeypatch, "fail_timing=false", "write_gds=true", layout=True)

    last = run_dir(tmp_path)
    record = read_record(last, run_root=tmp_path / "xeda_run")
    assert record.outcome.value == "failed"
    assert record.artifacts["log"] == "logs/openroad.log"
    assert record.artifacts["klayout_log"] == "logs/klayout.log"
    assert (last / "logs" / "klayout.log").read_text().strip().endswith("orphan cell 'X'")
    assert record.error is not None
    assert record.error.code == "nonzero_exit" and record.error.log == "klayout_log"


def test_the_run_document_names_the_log_of_the_secondary_tool_that_failed(tmp_path, monkeypatch):
    """`xeda run --json` reports the error of the node that caused the failure, with the key of
    the log to read in it."""
    from .openroad_utils import register_openroad_flows
    from .test_openroad_final import fake_klayout
    from .test_openroad_flows import xeda_run

    tool_utils.require_tclsh()
    tool_utils.use_fake_openroad(monkeypatch)
    fake_klayout(tmp_path, monkeypatch)
    monkeypatch.setenv("KLAYOUT_FAKE_ERROR", "[ERROR] Found orphan cell 'X'")
    with register_openroad_flows():
        result, document = xeda_run(tmp_path, "fail_timing=false", "write_gds=true", layout=True)

    assert result.exit_code == 1 and document["outcome"] == "failed"
    assert document["error"]["code"] == "nonzero_exit"
    assert document["error"]["log"] == "klayout_log"
    node = document["nodes"][-1]
    assert node["record"]["error"]["log"] == "klayout_log"
    assert node["record"]["artifacts"]["klayout_log"] == "logs/klayout.log"


def test_a_failing_main_tool_of_the_same_flow_points_at_the_main_log(tmp_path, monkeypatch):
    from .test_openroad_final import fake_klayout
    from .test_openroad_flows import launch, run_dir
    from .openroad_utils import register_openroad_flows

    tool_utils.require_tclsh()
    tool_utils.use_fake_openroad(monkeypatch)
    fake_klayout(tmp_path, monkeypatch)
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "detailed_route")
    with register_openroad_flows():
        launch(tmp_path, monkeypatch, "fail_timing=false", "write_gds=true", layout=True)

    record = read_record(run_dir(tmp_path), run_root=tmp_path / "xeda_run")
    assert record.outcome.value == "failed"
    assert record.error is not None and record.error.log == "log"
    assert "klayout_log" not in record.artifacts


# --------------------------------------------------------------------------- cleanup and reuse


def test_a_post_cleanup_run_keeps_every_artifact_so_the_logs_stay(tmp_path, monkeypatch):
    use_fake_tools(monkeypatch)
    monkeypatch.chdir(tmp_path)
    runner = DefaultRunner(tmp_path / "run", display_results=False, post_cleanup=True)

    flow = runner.launch_flow("vivado_synth", Design.from_file(EXAMPLE), FULL["vivado_synth"])

    assert flow.succeeded
    record = flow.record
    assert "log" in record.artifacts
    for name in record.artifacts.values():
        assert (record.run_dir / name).is_file(), name
    read_again = read_record(record.run_dir, run_root=record.run_root)
    assert dict(read_again.artifacts) == dict(record.artifacts)


def test_a_reused_run_keeps_the_artifacts_of_its_record(tmp_path, monkeypatch):
    use_fake_tools(monkeypatch)
    monkeypatch.chdir(tmp_path)
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    design = Design.from_file(EXAMPLE)

    first = runner.launch_flow(
        "quartus", design, {"fpga": "10CL016YU256C6G", "clock": {"period": "10 ns"}}
    )
    again = runner.launch_flow(
        "quartus", design, {"fpga": "10CL016YU256C6G", "clock": {"period": "10 ns"}}
    )

    assert first.succeeded and again.reused
    assert dict(again.record.artifacts) == dict(first.record.artifacts)
    assert "log" in again.record.artifacts


def test_the_report_keys_of_the_vivado_flows_hold_the_step_and_the_whole_file_name(
    tmp_path, monkeypatch
):
    """Several steps write a file of the same name, and `utilization.rpt` and `utilization.xml`
    share a stem, so a report key is the lower snake case of the step and the file name."""
    use_fake_tools(monkeypatch)
    monkeypatch.chdir(tmp_path)
    runner = DefaultRunner(tmp_path / "run", display_results=False)

    flow = runner.launch_flow("vivado_synth", Design.from_file(EXAMPLE), FULL["vivado_synth"])

    reports = {
        key: name for key, name in flow.record.artifacts.items() if name.startswith("reports/")
    }
    assert reports
    for key, name in reports.items():
        step = Path(name).parent.name
        assert key == re.sub(r"[^a-z0-9]+", "_", f"{step}_{Path(name).name}"), (key, name)
    assert "route_design_timing_summary_rpt" in reports
