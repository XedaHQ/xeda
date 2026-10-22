"""The last stage of a launch: show what a run produced, and record it.

The functions are free functions with explicit arguments. This module never imports
`default_runner`: the launcher passes what each function needs.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
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
from ..errors import RecordError
from ..flow import Flow
from ..records import RunRecord, encode_record, read_record, record_of
from ..utils import LOCATION_FORMS, dump_json, json_encodable, with_json_keys
from .launch_run import timestamp_text
from .outputs import recorded_outputs
from .trace import Freshness

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
    run_root: Path,
) -> None:
    """Show the results and, for a run that just executed (`record`), record the run.

    The record of the run (`records.record_of`) is then `flow.record`, and `results.json` holds
    it. The directory of a reused run is left exactly as it was."""
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

    if record:
        assert flow.run_id is not None, f"{flow.name} has no run id"
        flow.record = record_of(
            flow, run_root=run_root, run_id=flow.run_id, finished=datetime.now(timezone.utc)
        )
        if settings.dump_results_json:
            dump_json(encode_record(flow.record), results_json, backup=settings.backups)
            log.info("Results written to %s", results_json)

    if settings.display_results:
        print_results(
            flow,
            title=f"Results of flow:{flow.name} design:{design.name}",
            skip_if_false={"artifacts", "reports"},
        )


def adoptable_document(
    record: RunRecord,
    verdict: str,
    error: Optional[Mapping[str, Any]],
    run_id: Optional[str] = None,
) -> bool:
    """Return whether the record `record` of a run agrees with the trace of its run.

    `verdict` and `error` are those of the trace (`Trace.verdict`, `Trace.error`), and `run_id`
    is its run. The record agrees only in one of two cases:

    - The trace says `passed`, and the outcome of the record is `passed`.
    - The trace says `rejected`, and the outcome of the record is `rejected`, with an `error`
      that has the `code` and the `message` of the `error` of the trace, which is a mapping with
      a text `code` and a text `message`.

    Any other combination is False: an unknown verdict, or a trace and a record that disagree
    (one was edited, or a later run replaced the record without writing a trace). When `run_id`
    is given, the record must be the record of that run. The launch and the dry run apply this one
    rule, so neither reuses a result that the other would refuse."""
    if run_id is not None and record.run_id != run_id:
        return False
    if verdict == "passed":
        return record.outcome == "passed"
    if verdict != "rejected" or record.outcome != "rejected":
        return False
    if not isinstance(error, Mapping):
        return False
    if not all(isinstance(error.get(key), str) for key in ("code", "message")):
        return False
    return record.error is not None and (record.error.code, record.error.message) == (
        error["code"],
        error["message"],
    )


def results_of(record: RunRecord) -> dict[str, Any]:
    """Return what `record` puts in `Flow.results`: its metrics, flat by their dotted paths, and
    the verdict, the error, the runtime, the artifacts (as absolute paths in the run directory) and
    the declared outputs of the run."""
    from ..frozen import thaw

    results: dict[str, Any] = {path: thaw(record.metric(path)) for path in record.metric_info}
    results["success"] = record.passed
    results["runtime"] = record.runtime
    if record.error is not None:
        results["error"] = record.error.as_json_value()
    results["artifacts"] = {
        key: str(record.run_dir / name) for key, name in record.artifacts.items()
    }
    outputs = recorded_outputs(record)
    if outputs:
        results["outputs"] = outputs
    return results


def reuse_results(flow: Flow, run_root: Path, freshness: Freshness) -> bool:
    """Adopt the record of a run that is up to date.

    `freshness` is the result of the check of the trace. The record is adopted only if it agrees
    with its verdict and its run (`adoptable_document`). The flow then holds it (`Flow.record`) and
    its results are those of the record (`results_of`). For a rejected run, the flow holds the
    rejection (`Flow.rejection`) and is marked as rejected again (`Flow.rejected_again`). Its
    record holds no output, so a consumer is handed nothing.

    Return False if the record cannot be read or does not agree. A record that an older Xeda wrote
    is one of them: the run is stale, and the flow runs again."""
    try:
        record = read_record(flow.run_path, run_root=run_root)
    except RecordError as problem:
        log.info("%s", problem)
        return False
    if not adoptable_document(record, freshness.verdict, freshness.error, freshness.run_id):
        return False
    flow.record = record
    flow.timestamp = timestamp_text(record.started)
    flow.results.update(**results_of(record))
    flow.artifacts = Box(flow.results["artifacts"])
    flow.reused = True
    if freshness.verdict == "rejected":
        assert freshness.error is not None
        flow.rejected_again = True
        flow.outcome = "rejected"
        flow.rejection = freshness.error["message"]
    else:
        flow.outcome = "passed"
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
