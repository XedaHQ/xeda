"""The artifacts of the Vivado flows: the logs of the tools and the reports of the routed design.

A report is a file in `<reports_dir>/route_design/`. Its artifact key is the lower snake case of
its file name, extension included: `timing_summary_rpt`, `utilization_xml`. Two reports can share
a stem (`utilization.rpt`, `utilization.xml`), so the extension stays in the key. Reports of the
earlier steps (`synth_design`, `place_design`, ...) are working files and no artifacts.
"""

from __future__ import annotations

import re
from pathlib import Path

from ...flow import ArtifactSpec

__all__ = ["REPORT_STEP", "report_artifacts", "report_key", "register_reports"]

#: The step whose reports are artifacts: the last step of the implementation.
REPORT_STEP = "route_design"

_ALWAYS = "always"
_EXTRA = "when the setting `extra_reports` is on"
_QOR = "when the setting `qor_suggestions` is on"
_POWER = "when the setting `report_power` is on"

#: Every report that a Vivado flow writes in the reports of the routed design: the file name, the
#: description and the condition of the report.
REPORTS: dict[str, tuple[str, str]] = {
    "timing_summary.rpt": ("Timing summary of the routed design.", _ALWAYS),
    "timing.rpt": ("The worst timing paths of the routed design.", _ALWAYS),
    "utilization.rpt": ("Resource utilization of the routed design, as text.", _ALWAYS),
    "utilization.xml": (
        "Resource utilization of the routed design, as XML. The flow reads its results from it.",
        _ALWAYS,
    ),
    "hierarchical_utilization.rpt": (
        "Resource utilization of the routed design by module, as text.",
        _ALWAYS,
    ),
    "hierarchical_utilization.xml": (
        "Resource utilization of the routed design by module, as XML.",
        _ALWAYS,
    ),
    "hierarchical_utilization.json": (
        "Resource utilization by module, as JSON. Xeda converts it from the XML report.",
        "when the XML report of the utilization by module has been read",
    ),
    "critical_paths.csv": ("The critical paths, one for each endpoint.", _ALWAYS),
    "critical_paths_by_delay.csv": ("The critical paths, sorted by delay.", _ALWAYS),
    "critpath_report.csv": ("The critical paths, one for each endpoint.", _ALWAYS),
    "critpath_by_delay_report.csv": ("The critical paths, sorted by delay.", _ALWAYS),
    "drc.rpt": ("Design rule check of the routed design.", _ALWAYS),
    "route_status.rpt": ("Routing status of the nets.", _ALWAYS),
    "datasheet.rpt": ("Timing of the ports of the design (datasheet).", _ALWAYS),
    "design_analysis.rpt": ("Complexity, congestion and timing analysis.", _ALWAYS),
    "design_analysis.json": ("Logic level distribution and QoR summary, as JSON.", _ALWAYS),
    "clock_utilization.rpt": ("Clock resource utilization.", _EXTRA),
    "methodology.rpt": ("Methodology checks of the design.", _EXTRA),
    "power.rpt": ("Power estimate of the routed design, as text.", _EXTRA),
    "qor_suggestions.rpt": ("Suggestions to improve the quality of results.", _QOR),
    "qor_suggestions.rqs": ("Suggestions to improve the quality of results, for Vivado.", _QOR),
    "power.xml": (
        "Power estimate of the routed design, as XML. The flow reads its power results from it.",
        _POWER,
    ),
}

#: The reports that the project mode flows write (`post_step_hook.tcl`).
PROJECT_REPORTS = (
    "timing_summary.rpt",
    "timing.rpt",
    "utilization.rpt",
    "utilization.xml",
    "hierarchical_utilization.rpt",
    "hierarchical_utilization.xml",
    "hierarchical_utilization.json",
    "critical_paths.csv",
    "critical_paths_by_delay.csv",
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
)

#: The reports that the flows without a project write (`implementation.tcl`).
SCRIPT_REPORTS = (
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
)


def report_key(name: str) -> str:
    """Return the artifact key of the report with the file name `name`."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def report_artifacts(names: tuple[str, ...]) -> dict[str, ArtifactSpec]:
    """Describe the reports with the file names `names`, by artifact key."""
    return {report_key(name): ArtifactSpec(REPORTS[name][0], REPORTS[name][1]) for name in names}


def register_reports(flow, reports_dir: Path, names: tuple[str, ...]) -> None:
    """Register as artifacts the reports with the file names `names` that this run of `flow` wrote.

    `reports_dir` is the reports directory of the flow. A report that an earlier run left is no
    artifact (`Flow.wrote_output`). This only registers a file: it reads none, so the trace of
    the run does not record it as a report that the run consumed."""
    for name in names:
        report = reports_dir / REPORT_STEP / name
        if flow.wrote_output(report):
            flow.artifacts[report_key(name)] = report
