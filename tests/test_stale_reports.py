"""A report a previous run left is never taken for this run's.

A run directory is reused across runs, so the reports of the previous run are still there when a
flow runs again: if the tool fails before it writes new ones, or writes none, a flow that reads
its reports by path would be judged -- and pass -- by the previous run's. So a file whose mtime
predates the run's start (read from the file system's clock, like every time a trace compares)
is missing to the report helpers (`Flow.report_file`, which `parse_regex` and every flow's own
report reading go through): "not written by this run".

The sweep learns, for every registered flow, what its `parse_reports` reads in its run
directory; plants each of those files there as a previous run's (its mtime an hour before the
run); runs the flow again with a tool that writes nothing; and requires that none of them was
read, and that the run failed.
"""

import builtins
import io
import json
import os
import shutil
import time
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import Field

from xeda import Design
from xeda.flow import Flow, registered_flows
from xeda.flow_runner import DefaultRunner

from .settings_samples import flow_classes, minimal_settings
from .test_isolation import EXTRA_SETTINGS, FAKED, FPGA_FAKED, SQRT
from .tool_utils import use_fake_fpga_tools, use_fake_tools

#: the design's sources for a flow whose tool does not read VHDL
SOURCES = {"bsc": (["Top.bsv"], "mkTop"), "bsc_sim": (["Top.bsv"], "mkTop")}


def _design_directory(work: Path) -> Path:
    """The design's own directory: the sqrt design plus the bsc/bsc_sim sources."""
    work.mkdir(parents=True)
    for name in ("sqrt.vhdl", "sqrt.yaml", "tb_sqrt.py"):
        shutil.copy(SQRT / name, work / name)
    (work / "Top.bsv").write_text("module mkTop(Empty); endmodule\n")
    (work / "Tb.bsv").write_text("module mkTb(Empty); endmodule\n")
    return work


#: The flows the sweep brings to read their reports (a flow whose tool is stubbed and that fails
#: before its `parse_reports`, or reads no report of its own, is skipped).
READERS = {
    "dc",
    "diamond_synth",
    "ise_synth",
    "quartus",
    "vivado_alt_synth",
    "vivado_project",
    "vivado_synth",
    "yosys_fpga",
    "nextpnr",
}

#: settings with which each flow whose fake tool writes its reports runs through to success
SUCCEEDING = {
    "vivado_synth": {"fpga": {"part": "xc7a12tcsg325-1"}, "clock": {"period": 10.0}},
}


def _recording_parse_reports(flow_class, monkeypatch, reads: list, touched: list) -> None:
    """Wrap `flow_class.parse_reports`, and `check_results`, the checks a family of flows shares
    that the launcher runs after it (cocotb's verdict): while either runs, every file in the run
    directory it opens to read goes into `reads`, and every one it opens or looks up into
    `touched`."""
    for method in ("parse_reports", "check_results"):
        _record_reads(flow_class, method, monkeypatch, reads, touched)


def _record_reads(flow_class, method: str, monkeypatch, reads: list, touched: list) -> None:
    parse = getattr(flow_class, method)

    def parse_reports(self):
        run_dir = self.run_path.resolve()
        real_open, real_stat = builtins.open, os.stat

        def inside(file) -> Path | None:
            if isinstance(file, (str, os.PathLike)):
                path = Path(os.path.abspath(file))
                if path.is_relative_to(run_dir):
                    return path
            return None

        def recording_open(file, mode="r", *args, **kwargs):
            path = inside(file)
            if path is not None and not any(c in str(mode) for c in "wax+"):
                reads.append(path)
                touched.append(path)
            return real_open(file, mode, *args, **kwargs)

        def recording_stat(path, *args, **kwargs):
            found = inside(path)
            if found is not None:
                touched.append(found)
            return real_stat(path, *args, **kwargs)

        builtins.open = io.open = recording_open
        os.stat = recording_stat
        try:
            return parse(self)
        finally:
            builtins.open = io.open = real_open
            os.stat = real_stat

    monkeypatch.setattr(flow_class, method, parse_reports)


def _launch(flow_class, work: Path, run_root: Path, **launcher):
    sources, top = SOURCES.get(flow_class.name, (["sqrt.vhdl"], "sqrt"))
    design = Design(
        name="sqrt",
        design_root=work,
        rtl={"sources": sources, "top": top, "clock": {"port": "clk"}},
        tb={"sources": ["tb_sqrt.py"], "cocotb": True},
        language={"vhdl": {"standard": "2008"}},
    )
    settings = SUCCEEDING.get(flow_class.name) or {
        **minimal_settings(flow_class),
        **EXTRA_SETTINGS.get(flow_class.name, {}),
    }
    try:
        return DefaultRunner(run_root, display_results=False, **launcher).run_flow(
            flow_class, design, settings
        )
    except Exception:  # noqa: BLE001 - a run that raises has failed, which is what is checked
        return None


def _writes_nothing(monkeypatch) -> None:
    """Every tool the flows start does nothing, and writes nothing."""
    monkeypatch.setattr("xeda.tool.run_process", lambda *args, **kwargs: "")
    monkeypatch.setattr("xeda.tool.Tool.version_gte", lambda self, *args: True)


@pytest.mark.parametrize("flow_class", [cls for cls, _ in flow_classes()], ids=lambda c: c.name)
def test_a_report_a_previous_run_left_is_never_read(flow_class, tmp_path, monkeypatch):
    use_fake_tools(monkeypatch)
    if flow_class.name in FPGA_FAKED:
        use_fake_fpga_tools(monkeypatch, tmp_path / "fake-toolchain")
    if flow_class.name not in FAKED:
        _writes_nothing(monkeypatch)
    reads: list[Path] = []
    touched: list[Path] = []
    _recording_parse_reports(flow_class, monkeypatch, reads, touched)
    work = _design_directory(tmp_path / "design")
    monkeypatch.chdir(work)
    run_root = tmp_path / "xeda_run"
    first = _launch(flow_class, work, run_root)
    if flow_class.name in FPGA_FAKED:
        assert first is not None and first.succeeded, "lost positive FPGA report coverage"
    looked_at = sorted({p for p in touched if not p.is_dir()})
    if not looked_at:
        assert flow_class.name not in READERS, "the sweep lost its teeth: no report was read"
        pytest.skip(f"{flow_class.name} reads no report in its run directory here")
    # the previous run's reports: what it left, or files by the names it looked for
    an_hour_ago = time.time() - 3600
    for path in looked_at:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("a previous run's report\n")
        os.utime(path, (an_hour_ago, an_hour_ago))
    reads.clear()
    _writes_nothing(monkeypatch)
    second = _launch(flow_class, work, run_root, rebuild_all=True)
    stale = sorted({p for p in reads if p in looked_at and p.stat().st_mtime < an_hour_ago + 1})
    assert not stale, f"{flow_class.name} read a previous run's {stale}"
    assert (
        second is None or not second.succeeded
    ), f"{flow_class.name} passed on the previous run's reports"


def test_a_report_this_run_wrote_is_read(tmp_path, monkeypatch):
    """The control: the same reports, written by this run (the fake Vivado's), are read."""
    from xeda.flows import VivadoSynth

    use_fake_tools(monkeypatch)
    work = _design_directory(tmp_path / "design")
    monkeypatch.chdir(work)
    flow = _launch(VivadoSynth, work, tmp_path / "xeda_run")
    assert flow is not None and flow.succeeded
    flow = _launch(VivadoSynth, work, tmp_path / "xeda_run", rebuild_all=True)
    assert flow is not None and flow.succeeded and "lut" in flow.results


def test_report_file_says_whether_this_run_wrote_it(tmp_path):
    """`Flow.report_file`: a report missing, or not written since the run started -- left as it
    was when the launcher snapshotted the run directory (`Flow.start_run`) -- is None."""

    class ProbeReports(Flow):
        """Reads reports."""

        results_description: dict = {}

        def run(self) -> None:
            pass

    try:
        flow = ProbeReports(
            {}, Design(name="d", design_root=tmp_path, rtl={"sources": []}), tmp_path
        )
        (tmp_path / "old.rpt").write_text("old\n")
        in_the_future = time.time() + 3600  # no clock decides: an earlier file is earlier
        os.utime(tmp_path / "old.rpt", (in_the_future, in_the_future))
        flow.start_run()
        (tmp_path / "new.rpt").write_text("new\n")
        assert flow.report_file(tmp_path / "new.rpt") == tmp_path / "new.rpt"
        assert flow.report_file(tmp_path / "old.rpt") is None
        assert flow.report_file(tmp_path / "missing.rpt") is None
        assert flow.parse_regex(tmp_path / "old.rpt", r"(?P<x>old)") is None
        assert flow.parse_regex(tmp_path / "new.rpt", r"(?P<x>new)") == {"x": "new"}
    finally:
        from xeda.flow import registered_flows

        for name in (ProbeReports.name, ProbeReports.__name__):
            registered_flows.pop(name, None)


RESULTS_XML = """<testsuites><testsuite name="t" tests="1" errors="0" failures="0" skipped="0"
time="1" sim_time_ns="1"><testcase name="c" classname="t" time="1" sim_time_ns="1"/></testsuite>
</testsuites>
"""
NEXTPNR_REPORT = '{"fmax": {}, "utilization": {}, "critical_paths": []}\n'
UTILIZATION_JSON = '{"design": {"num_cells": 3, "num_cells_by_type": {}}}\n'


def _yosys_report(flow: Flow) -> Path:
    flow.artifacts.utilization_report = flow.run_path / "reports" / "utilization.json"
    return flow.artifacts.utilization_report


NEXTPNR_XILINX_REPORT = (
    '{"fmax": {}, "utilization": {"SLICE_LUTX": {"used": 1, "available": 2}}, '
    '"critical_paths": []}\n'
)
XILINX_PLACEMENT = (
    '{"a": {"tile": "CLBLL_L_X2Y56", "site": "SLICE_X0Y56", "bel": "A6LUT", '
    '"type": "SLICE_LUTX"}}\n'
)


#: The flows the sweep cannot bring to their `parse_reports` with their tools stubbed, by case:
#: the flow, the settings to construct it, and every file its `parse_reports` reads (placed as
#: its run would) with a content that is a passing report.
DIRECT = {
    "ghdl_sim": ("ghdl_sim", {}, lambda flow: {flow.run_path / "results.xml": RESULTS_XML}),
    "nvc": ("nvc", {}, lambda flow: {flow.run_path / "results.xml": RESULTS_XML}),
    "yosys": ("yosys", {"platform": "asap7"}, lambda f: {_yosys_report(f): UTILIZATION_JSON}),
    "nextpnr": (
        "nextpnr",
        {"fpga": {"part": "LFE5U-25F-6BG381C"}},
        lambda flow: {flow.run_path / "report.json": NEXTPNR_REPORT},
    ),
    # 7-series: the report, and the placement dump the LUT count is read from
    "nextpnr_xilinx": (
        "nextpnr",
        {"fpga": {"part": "xc7a100tcsg324-1"}},
        lambda flow: {
            flow.run_path / "report.json": NEXTPNR_XILINX_REPORT,
            flow.run_path / "placement.json": XILINX_PLACEMENT,
        },
    ),
}


@pytest.mark.parametrize("case", sorted(DIRECT))
def test_a_flow_the_sweep_cannot_run_never_reads_a_previous_run_s_report(
    case, tmp_path, monkeypatch
):
    """The reports such a flow reads, passing ones: written by this run they are read and it
    passes; left by a previous run -- there, unchanged, since the run started -- none is read,
    and the run does not pass."""
    from xeda.flow_runner import get_flow_class

    flow_name, settings, files_of = DIRECT[case]
    flow_class = get_flow_class(flow_name)
    design = Design(
        name="sqrt",
        design_root=tmp_path,
        rtl={"sources": [], "top": "sqrt"},
        tb={"sources": [], "cocotb": True, "top": "tb"},
    )
    outcomes = {}
    for age in ("this run's", "a previous run's"):
        run_dir = tmp_path / age.replace(" ", "_").replace("'", "")
        run_dir.mkdir()
        monkeypatch.chdir(run_dir)
        flow = flow_class(settings, design, run_dir)
        files = files_of(flow)
        for report in files:
            report.parent.mkdir(parents=True, exist_ok=True)
        if age == "a previous run's":
            for report, content in files.items():
                report.write_text(content)
        flow.start_run()
        if age == "this run's":
            for report, content in files.items():
                report.write_text(content)
        reads: list[Path] = []
        _recording_parse_reports(flow_class, monkeypatch, reads, [])
        passed = bool(flow.parse_reports()) & bool(flow.check_results())  # as the launcher does
        outcomes[age] = (passed, sorted(r for r in files if r.resolve() in reads))
        if case == "nextpnr_xilinx" and age == "this run's":
            assert flow.results["lut"] == 1
    assert outcomes["a previous run's"] == (False, [])
    passed, read = outcomes["this run's"]
    assert passed and len(read) == len(files)


class _ReadsReport(Flow):
    """Writes its report only when told to, and succeeds only when it can read one."""

    results_description: ClassVar[dict[str, str]] = {}

    class Settings(Flow.Settings):
        write: bool = Field(True, description="Whether the run writes its report.")

    def run(self) -> None:
        if self.settings.write:
            (self.run_path / "reports").mkdir(exist_ok=True)
            (self.run_path / "reports" / "r.txt").write_text("ok\n")

    def parse_reports(self) -> bool:
        return self.report_file(self.run_path / "reports" / "r.txt") is not None


def test_the_reports_a_run_read_are_removed_before_the_next_run(tmp_path):
    """A report whose mtime was moved forward passes the check that a run wrote it; removing the
    reports the last run read before the next one runs does not let it through."""
    design = Design(name="d", design_root=tmp_path, rtl={"sources": []})
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    try:
        first = runner.launch_flow(_ReadsReport, design, {"write": True})
        trace = json.loads((first.run_path / "trace.json").read_text())
        assert first.succeeded and trace["reports"] == ["reports/r.txt"]
        future = time.time_ns() + 3600 * 10**9
        os.utime(first.run_path / "reports" / "r.txt", ns=(future, future))
        second = runner.launch_flow(_ReadsReport, design, {"write": False})
        assert not second.succeeded
        assert not (second.run_path / "reports" / "r.txt").exists()
    finally:
        for name in (_ReadsReport.name, _ReadsReport.__name__):
            registered_flows.pop(name, None)


#: while set, `_ReportsWhenTold` writes its report
WRITE_REPORT = "XEDA_TEST_WRITE_REPORT"


class _ReportsWhenTold(Flow):
    """Writes its report only while `WRITE_REPORT` is set (the environment is no input of a
    run), and succeeds only when it can read one."""

    results_description: ClassVar[dict[str, str]] = {}

    class Settings(Flow.Settings):
        tag: str = Field("a", description="Any text: another tag is other settings.")
        always: bool = Field(False, description="Whether the run can never be reused.")

    def always_runs(self) -> str | None:
        return "it was told to" if self.settings.always else super().always_runs()

    def run(self) -> None:
        if os.environ.get(WRITE_REPORT):
            (self.run_path / "reports").mkdir(exist_ok=True)
            (self.run_path / "reports" / "r.txt").write_text("ok\n")

    def parse_reports(self) -> bool:
        return self.report_file(self.run_path / "reports" / "r.txt") is not None


@pytest.mark.parametrize(
    "why", ["its settings changed", "an input changed", "rebuild_all", "it always runs"]
)
def test_a_previous_trace_s_reports_are_removed_before_every_run(tmp_path, monkeypatch, why):
    """Whatever makes the flow run again -- other settings, an edited input, `rebuild_all`, a
    flow that can never be reused -- the report its last traced run read is removed before it
    runs: left with a future mtime, it passes the check that a run wrote it, and must never be
    read as this run's (every run directory is xeda's: no exception)."""
    (tmp_path / "a.v").write_text("module a; endmodule\n")
    design = Design(name="d", design_root=tmp_path, rtl={"sources": ["a.v"], "top": "a"})
    monkeypatch.setenv(WRITE_REPORT, "1")
    try:
        first = DefaultRunner(tmp_path / "xeda_run", display_results=False).launch_flow(
            _ReportsWhenTold, design, {}
        )
        report = first.run_path / "reports" / "r.txt"
        assert first.succeeded and report.exists()
        future = time.time_ns() + 3600 * 10**9
        os.utime(report, ns=(future, future))
        monkeypatch.delenv(WRITE_REPORT)
        settings: dict = {}
        if why == "its settings changed":
            settings = {"tag": "b"}
        elif why == "an input changed":
            (tmp_path / "a.v").write_text("module a(); endmodule\n")
            design = Design(name="d", design_root=tmp_path, rtl={"sources": ["a.v"], "top": "a"})
        elif why == "it always runs":
            settings = {"always": True}
        runner = DefaultRunner(
            tmp_path / "xeda_run", display_results=False, rebuild_all=why == "rebuild_all"
        )
        second = runner.launch_flow(_ReportsWhenTold, design, settings)
        assert second.run_path == first.run_path and not second.reused
        assert not second.succeeded, "a previous run's report was read as this run's"
        assert not report.exists()
    finally:
        for name in (_ReportsWhenTold.name, _ReportsWhenTold.__name__):
            registered_flows.pop(name, None)
