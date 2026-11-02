"""The artifacts of the Vivado flows: the logs of the runs and the reports of the steps.

The project mode flows start two child runs in Vivado, `synth_1` and `impl_1`. Each writes a log
`<design>.runs/<run>/runme.log`, and the error of a failed run is there. The key of that log is
`<run>_log`.

A report is a file in `<reports_dir>/<step>/`. Its artifact key is the lower snake case of the step
and the file name together, extension included: `route_design_timing_summary_rpt`,
`place_design_utilization_xml`. Several steps write a file of the same name, and one step can write
two files of the same stem (`utilization.rpt`, `utilization.xml`), so the key holds both.
"""

from __future__ import annotations

import re
from pathlib import Path

from ...flow import ArtifactSpec

__all__ = [
    "ALT_SYNTH_REPORTS",
    "PROJECT_REPORTS",
    "RUNS",
    "SCRIPT_REPORTS",
    "register_run_logs",
    "report_artifacts",
    "report_key",
    "register_reports",
    "run_log_artifacts",
    "run_log_key",
]

_ALWAYS = "when the run reaches the step"
_EXTRA = "when the run reaches the step and the setting `extra_reports` is on"
_QOR = "when the run reaches the step and the setting `qor_suggestions` is on"
_POWER = "when the run reaches the step and the setting `report_power` is on"
_CHECKPOINT = "when the run reaches the step and the setting `write_checkpoint` is on"
_POWER_OPT = "when the run reaches the step and the strategy of the step has `power_opt`"

#: Every report that a Vivado flow writes: the file name, what it holds and when a run has it.
FILES: dict[str, tuple[str, str]] = {
    "timing_summary.rpt": ("Timing summary.", _ALWAYS),
    "timing.rpt": ("The worst timing paths.", _ALWAYS),
    "utilization.rpt": ("Resource utilization, as text.", _ALWAYS),
    "utilization.xml": (
        "Resource utilization, as XML. The flow reads its results from the one of the last step.",
        _ALWAYS,
    ),
    "hierarchical_utilization.rpt": ("Resource utilization by module, as text.", _ALWAYS),
    "hierarchical_utilization.xml": ("Resource utilization by module, as XML.", _ALWAYS),
    "hierarchical_utilization.json": (
        "Resource utilization by module, as JSON. Xeda converts it from the XML report.",
        "when the run reaches the step and the XML report of the utilization by module is read",
    ),
    "critical_paths.csv": ("The critical paths, one for each endpoint.", _ALWAYS),
    "critical_paths_by_delay.csv": ("The critical paths, sorted by delay.", _ALWAYS),
    "critpath_report.csv": ("The critical paths, one for each endpoint.", _ALWAYS),
    "critpath_by_delay_report.csv": ("The critical paths, sorted by delay.", _ALWAYS),
    "drc.rpt": ("Design rule check.", _ALWAYS),
    "route_status.rpt": ("Routing status of the nets.", _ALWAYS),
    "datasheet.rpt": ("Timing of the ports of the design (datasheet).", _ALWAYS),
    "design_analysis.rpt": ("Complexity, congestion and timing analysis.", _ALWAYS),
    "design_analysis.json": ("Logic level distribution and QoR summary, as JSON.", _ALWAYS),
    "clock_utilization.rpt": ("Clock resource utilization.", _EXTRA),
    "methodology.rpt": ("Methodology checks of the design.", _EXTRA),
    "power.rpt": ("Power estimate, as text.", _EXTRA),
    "qor_suggestions.rpt": ("Suggestions to improve the quality of results.", _QOR),
    "qor_suggestions.rqs": ("Suggestions to improve the quality of results, for Vivado.", _QOR),
    "power.xml": ("Power estimate, as XML. The flow reads its power results from it.", _POWER),
    "power_optimization.rpt": ("Report of the power optimization after synthesis.", _POWER_OPT),
    "post_place_power_optimization.rpt": (
        "Report of the power optimization after placement.",
        _POWER_OPT,
    ),
}

_COMMON = (
    "timing_summary.rpt",
    "timing.rpt",
    "utilization.xml",
    "hierarchical_utilization.xml",
    "critical_paths.csv",
    "critical_paths_by_delay.csv",
)

#: The reports that the project mode flows write, by step (`post_step_hook.tcl`).
PROJECT_REPORTS: dict[str, tuple[str, ...]] = {
    "synth_design": _COMMON,
    "place_design": _COMMON,
    "phys_opt_design": _COMMON,
    "route_design": (
        *_COMMON,
        "hierarchical_utilization.json",
        "utilization.rpt",
        "hierarchical_utilization.rpt",
        "drc.rpt",
        "route_status.rpt",
        "datasheet.rpt",
        "design_analysis.rpt",
        "design_analysis.json",
        "clock_utilization.rpt",
        "methodology.rpt",
        "qor_suggestions.rpt",
        "qor_suggestions.rqs",
        "power.xml",
    ),
}

#: The reports that the flows without a project write, by step (`implementation.tcl`).
SCRIPT_REPORTS: dict[str, tuple[str, ...]] = {
    "post_place": (
        "timing_summary.rpt",
        "hierarchical_utilization.rpt",
        "post_place_power_optimization.rpt",
    ),
    "route_design": (
        "timing_summary.rpt",
        "timing.rpt",
        "utilization.rpt",
        "utilization.xml",
        "hierarchical_utilization.xml",
        "hierarchical_utilization.json",
        "critpath_report.csv",
        "critpath_by_delay_report.csv",
        "clock_utilization.rpt",
        "power.rpt",
        "drc.rpt",
        "methodology.rpt",
        "qor_suggestions.rpt",
        "power.xml",
    ),
}

#: The reports of `vivado_alt_synth`: those of `SCRIPT_REPORTS` and the ones after synthesis.
ALT_SYNTH_REPORTS: dict[str, tuple[str, ...]] = {
    "post_synth": (
        "timing_summary.rpt",
        "hierarchical_utilization.rpt",
        "power_optimization.rpt",
    ),
    **SCRIPT_REPORTS,
}


def report_key(step: str, name: str) -> str:
    """Return the artifact key of the report with the file name `name` of the step `step`."""
    return re.sub(r"[^a-z0-9]+", "_", f"{step}_{name}".lower()).strip("_")


def _condition(step: str, name: str) -> str:
    """Return when a run has the report `name` of `step`. The checkpoint reports of the steps
    before routing are the ones that a setting switches on."""
    when = FILES[name][1]
    if name in ("timing_summary.rpt", "hierarchical_utilization.rpt") and step in (
        "post_place",
        "post_synth",
    ):
        when = _CHECKPOINT if step == "post_place" else _ALWAYS
    return when


def report_artifacts(reports: dict[str, tuple[str, ...]]) -> dict[str, ArtifactSpec]:
    """Describe the reports of `reports` (step to file names), by artifact key."""
    return {
        report_key(step, name): ArtifactSpec(
            f"{FILES[name][0]} Step `{step}`.", _condition(step, name)
        )
        for step, names in reports.items()
        for name in names
    }


def register_reports(flow, reports_dir: Path, reports: dict[str, tuple[str, ...]]) -> None:
    """Register as artifacts the reports of `reports` (step to file names) that this run of `flow`
    wrote.

    `reports_dir` is the reports directory of the flow. A report that an earlier run left is no
    artifact (`Flow.wrote_output`). This only registers a file: it reads none, so the trace of
    the run does not record it as a report that the run consumed."""
    for step, names in reports.items():
        for name in names:
            report = reports_dir / step / name
            if flow.wrote_output(report):
                flow.artifacts[report_key(step, name)] = report


#: The child runs that a project mode flow starts in Vivado, in order.
RUNS = ("synth_1", "impl_1")


def run_log_key(run: str) -> str:
    """Return the artifact key of the log of the child run `run`."""
    return f"{re.sub(r'[^a-z0-9]+', '_', run.lower()).strip('_')}_log"


def run_log_artifacts() -> dict[str, ArtifactSpec]:
    """Describe the log of each child run, by artifact key."""
    return {
        run_log_key(run): ArtifactSpec(
            f"Log of the Vivado run `{run}` (`<design>.runs/{run}/runme.log`). The error of a "
            "failed run is there.",
            f"when Vivado starts the run `{run}`",
        )
        for run in RUNS
    }


def register_run_logs(flow) -> None:
    """Register as artifacts the logs of the child runs that this run of `flow` wrote."""
    for run in RUNS:
        log_file = Path(f"{flow.design.name}.runs") / run / "runme.log"
        if flow.wrote_output(log_file):
            flow.artifacts[run_log_key(run)] = log_file
