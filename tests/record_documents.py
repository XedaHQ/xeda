"""A `results.json` document for a test that needs a record and no run."""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from xeda.frozen import thaw
from xeda.records import RECORD_SCHEMA_VERSION, read_record

#: A digest that no file has, for a test that never checks one.
NO_DIGEST = "0" * 32


def record_document(
    outcome: str = "passed",
    *,
    flow: str = "flow",
    design: str = "d",
    artifacts: Mapping[str, str] | None = None,
    outputs: Mapping[str, Sequence[str]] | None = None,
    metrics: Mapping[str, Any] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Return a valid record document.

    `artifacts` maps a key to a name in the run directory. `outputs` maps the name of a declared
    output to the names of the files it holds. A record that did not pass has an error. Any other
    field replaces the one of the same name."""
    document: dict[str, Any] = {
        "schema_version": RECORD_SCHEMA_VERSION,
        "run_id": "0123456789abcdef0123456789abcdef",
        "xeda_version": "0",
        "flow": flow,
        "design": design,
        "target": None,
        "identity": "0" * 32,
        "configuration": {},
        "outcome": outcome,
        "error": (
            None
            if outcome == "passed"
            else {
                "code": "reported_failure",
                "message": "it failed",
                "node": flow,
                "exit_status": None,
                "log": None,
            }
        ),
        "metrics": dict(metrics or {}),
        "metric_info": {},
        "outputs": {
            name: {
                "kind": "Data",
                "cardinality": "many",
                "names": list(names),
                "digests": [NO_DIGEST] * len(names),
                "design": None,
            }
            for name, names in (outputs or {}).items()
        },
        "artifacts": dict(artifacts or {}),
        "settings": {},
        "inputs": [],
        "started": "2026-01-01T00:00:00+00:00",
        "finished": "2026-01-01T00:00:01+00:00",
        "runtime": 1.0,
        "dependencies": [],
        "inherited_variables": [],
    }
    document.update(fields)
    return document


def assert_metrics_recorded(flow: Any) -> None:
    """Assert that the `results.json` of `flow` holds each described metric of its results, with
    the value that the run reported: a metric that the record lost fails here, though the results
    in memory still have it."""
    record = read_record(flow.run_path, run_root=flow.record.run_root)
    for key in type(flow).results_description:
        value = flow.results.get(key)
        if value is None:
            continue
        assert key in record.metric_info, f"the record of {flow.name} has no metric {key}"
        assert thaw(record.metric(key)) == json.loads(json.dumps(value)), key
