"""The last stage of a launch: show what a run produced, and record it.

The functions are free functions with explicit arguments. This module never imports
`default_runner`: the launcher passes what each function needs.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Protocol, Union

from box import Box
from rich import box
from rich.style import Style
from rich.table import Table
from rich.text import Text

from ..artifacts import drop_unwritten_artifacts, iter_artifact_paths
from ..console import console
from ..deliver import deliverable_setting_names
from ..design import Design
from ..flow import Flow
from ..utils import LOCATION_FORMS, dump_json, json_encodable, with_json_keys

log = logging.getLogger(__name__)


def print_results(
    flow: Optional[Flow] = None,
    results: Optional[Dict[str, Any]] = None,
    title: Optional[str] = None,
    subset: Optional[Iterable[str]] = None,
    skip_if_false: Union[bool, Iterable[str], None] = None,
) -> None:
    """Print the selected results of a flow in a table."""
    if results is None and flow:
        results = flow.results
    assert results is not None, "results is None"
    # The table shows what `results.json` holds: keys as that file writes them, so a key it can
    # write (a tuple, a `Path`) cannot crash the table printed after the run.
    results = with_json_keys(dict(results))
    console.print()
    table = Table(
        title=title,
        title_style=Style(frame=True, bold=True),
        show_header=False,
        box=box.ROUNDED,
        show_lines=True,
    )
    table.add_column(style="bold", min_width=8, no_wrap=True)
    table.add_column(justify="right", min_width=8)
    skip_fields = [
        "timestamp",
        "design",
        "design_hash",
        "flow",
        "flow_hash",
        "settings_hash",
        "tools",
        "run_path",
        "artifacts",
        "outputs",
    ]
    for k, v in results.items():
        skipable = skip_if_false and (isinstance(skip_if_false, bool) or k in skip_if_false)
        if skipable and not v:
            continue
        if v is not None and not str(k).startswith("_"):
            if k == "success":
                table.add_row("Status", "[green]OK[/green]" if v else "[red]FAILED[/red]")
                continue
            if (subset and k not in subset) or k in skip_fields:
                continue
            if k == "runtime" and isinstance(v, (float, int)):
                table.add_row(
                    "Run time",
                    str(timedelta(seconds=round(v))),
                    style=Style(dim=True),
                )
                continue
            if isinstance(v, (dict,)):
                table.add_row(f"{k}:", "", style=Style(bold=True))
                for xk, xv in v.items():
                    if isinstance(xv, dict):
                        xv = json.dumps(xv, indent=1, default=json_encodable)
                    else:
                        xv = str(xv)
                    table.add_row(Text(f" {xk}"), str(xv))
                continue
            if isinstance(v, float):
                v = f"{v:,.3f}"
            elif isinstance(v, int):
                v = f"{v:,}"
            table.add_row(str(k), str(v))
    console.print(table)


def artifact_rows(artifacts: Mapping[str, Any]) -> list[tuple[str, str, bool]]:
    """Flatten an artifacts mapping into the rows of the "Artifacts:" table.

    Each row is `(label, path, end_section)`. The paths of a label are found with
    `iter_artifact_paths`, so a value nested through any mix of mappings, lists and tuples is
    flattened the same way as everywhere else that artifacts are walked. The label is shown
    once, on the row of its first path. A label with no path leaves (`None`, `""`, an empty list
    or dict, ...) gets no rows at all.
    """
    rows: list[tuple[str, str, bool]] = []
    for label, value in artifacts.items():
        paths = [str(path) for path in iter_artifact_paths(value)]
        for i, path in enumerate(paths):
            rows.append((label if i == 0 else "", path, i == len(paths) - 1))
    return rows


def drop_unwritten(flow: Flow) -> None:
    """Keep only the artifacts that a failed run wrote (`drop_unwritten_artifacts`)."""
    flow.results.artifacts = Box(
        drop_unwritten_artifacts(
            flow.results.artifacts,
            flow.wrote_output,  # only a file whose recorded prior state changed
            flow.name,
        )
    )


class ReportSettings(Protocol):
    """The launcher settings `report` reads."""

    display_results: bool
    dump_results_json: bool
    backups: bool


def report(
    flow: Flow,
    design: Design,
    results_json: Path,
    settings: ReportSettings,
    *,
    record: bool,
) -> None:
    """Show the results and, for a run that just executed (`record`), write them to `results.json`.

    The directory of a reused run is left exactly as it was."""
    if settings.display_results and flow.artifacts and flow.succeeded:
        table = Table(
            box=box.SIMPLE,
            show_header=True,
            show_edge=False,
            show_footer=True,
            collapse_padding=True,
            pad_edge=False,
        )
        table.add_column(
            "Artifacts:", justify="left", style="cyan", header_style="blue", no_wrap=False
        )
        table.add_column("", justify="left", style="green", no_wrap=False)

        for label, artifact, end_section in artifact_rows(flow.artifacts):
            table.add_row(label, artifact, end_section=end_section)

        console.print("")
        console.print(table)
        console.print("")

    if record and settings.dump_results_json:
        dump_json(flow.results, results_json, backup=settings.backups)
        log.info("Results written to %s", results_json)

    if settings.display_results:
        print_results(
            flow,
            title=f"Results of flow:{flow.name} design:{design.name}",
            skip_if_false={"artifacts", "reports"},
        )


def reuse_results(flow: Flow, results_json: Path) -> bool:
    """Adopt the recorded results of a run that is up to date.

    Return False if they cannot be read."""
    try:
        previous = Box(json.loads(results_json.read_text()))
    except (OSError, ValueError):
        return False
    if not previous.get("success"):
        return False
    flow.results.update(**previous)
    flow.artifacts = previous.get("artifacts", Box())
    flow.reused = True
    return True


def warn_outputs_to_delivered_nothing(flow: Flow, outputs_to: Path) -> None:
    """Warn that `--outputs-to` delivered nothing for `flow`.

    `--outputs-to` copies only the artifacts that a successful, requested flow reported inside
    its run directory. A flow that reported none there, or that names no setting a deliverable,
    delivers nothing, and nothing else says so. The warning names the flow and, where it has
    any, its own deliverable settings that could be given a location instead (`vcd` for a
    simulator). A bare run is then not mistaken for Xeda silently dropping a file."""
    names = deliverable_setting_names(type(flow.settings))
    which = ", ".join(names) if names else "none"
    log.warning(
        "--outputs-to %s: %s delivered nothing there. Its deliverable settings (%s) are what "
        "--outputs-to copies. Give one a location (%s) for something to deliver",
        outputs_to,
        flow.name,
        which,
        LOCATION_FORMS,
    )
