"""The last stage of a launch: record what a run produced.

The functions are free functions with explicit arguments. This module never imports
`default_runner`: the launcher passes what each function needs.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Protocol

from box import Box

from ..artifacts import drop_unwritten_artifacts
from ..deliver import deliverable_setting_names
from ..design import Design
from ..errors import RecordError
from ..flow import Flow
from ..records import RunRecord, encode_record, read_record, record_of
from ..utils import LOCATION_FORMS, dump_json
from .launch_run import timestamp_text
from .outputs import recorded_outputs
from .trace import Freshness, output_equivalences

log = logging.getLogger(__name__)


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
    """For a run that just executed (`record`), record the run.

    The record of the run (`records.record_of`) is then `flow.record`, and `results.json` holds
    it. If `results.json` cannot be written, the error propagates and `flow.record` stays unset.
    The directory of a reused run is left exactly as it was."""
    if record:
        assert flow.run_id is not None, f"{flow.name} has no run id"
        stored = record_of(
            flow, run_root=run_root, run_id=flow.run_id, finished=datetime.now(timezone.utc)
        )
        if settings.dump_results_json:
            dump_json(encode_record(stored), results_json, backup=settings.backups)
            log.info("Results written to %s", results_json)
        # Only now: a record that could not be stored is no record of the flow.
        flow.record = stored


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


def results_of(record: RunRecord, equivalences: Mapping[str, str] | None = None) -> dict[str, Any]:
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
    outputs = recorded_outputs(record, equivalences)
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
    flow.results.update(**results_of(record, output_equivalences(flow.run_path, flow.name)))
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
