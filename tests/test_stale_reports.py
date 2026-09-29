"""A report a previous run left is never taken for this run's (ruling R49).

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
import os
import time
from pathlib import Path

import pytest

from xeda import Design
from xeda.flow import Flow
from xeda.flow_runner import DefaultRunner

from .settings_samples import flow_classes, minimal_settings
from .test_run_dir_ownership import EXTRA_SETTINGS, FAKED, SOURCES, _design_directory
from .tool_utils import use_fake_tools

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
    if flow_class.name not in FAKED:
        _writes_nothing(monkeypatch)
    reads: list[Path] = []
    touched: list[Path] = []
    _recording_parse_reports(flow_class, monkeypatch, reads, touched)
    work = _design_directory(tmp_path / "design")
    monkeypatch.chdir(work)
    run_root = tmp_path / "xeda_run"
    _launch(flow_class, work, run_root)
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


#: The flows the sweep cannot bring to their `parse_reports` with their tools stubbed, each
#: with the settings to construct it, the report it reads (placed as its run would), and a
#: content that is a passing report.
DIRECT = {
    "ghdl_sim": ({}, lambda flow: flow.run_path / "results.xml", RESULTS_XML),
    "nvc": ({}, lambda flow: flow.run_path / "results.xml", RESULTS_XML),
    "yosys": ({"platform": "asap7"}, _yosys_report, UTILIZATION_JSON),
    "nextpnr": (
        {"fpga": {"part": "LFE5U-25F-6BG381C"}},
        lambda flow: flow.run_path / "report.json",
        NEXTPNR_REPORT,
    ),
}


@pytest.mark.parametrize("flow_name", sorted(DIRECT))
def test_a_flow_the_sweep_cannot_run_never_reads_a_previous_run_s_report(
    flow_name, tmp_path, monkeypatch
):
    """The report such a flow reads, a passing one: written by this run it is read and passes;
    left by a previous run -- there, unchanged, since the run started -- it is not read, and the
    run does not pass."""
    from xeda.flow_runner import get_flow_class

    flow_class = get_flow_class(flow_name)
    settings, report_of, content = DIRECT[flow_name]
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
        report = report_of(flow)
        report.parent.mkdir(parents=True, exist_ok=True)
        if age == "a previous run's":
            report.write_text(content)
        flow.start_run()
        if age == "this run's":
            report.write_text(content)
        reads: list[Path] = []
        _recording_parse_reports(flow_class, monkeypatch, reads, [])
        passed = bool(flow.parse_reports()) & bool(flow.check_results())  # as the launcher does
        outcomes[age] = (passed, report.resolve() in reads)
    assert outcomes == {"this run's": (True, True), "a previous run's": (False, False)}
