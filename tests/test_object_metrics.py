"""The keys of an `object` metric: documented, in the schema, and the keys that the writer writes.

`tools/api_baseline.py` records the schema of each object metric in the release baseline, so a
change of a key is a visible change of the contract. These tests keep the schema, the description
and the writer of each object metric in step.
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import jsonschema
import pytest

from xeda._internal.flow.asic import AsicPlatformFlow
from xeda._internal.flow.metrics import PLATFORM_SCHEMA, SIM_EVIDENCE_SCHEMA, MetricSpec
from xeda._internal.flow.sim import SimEvent, SimEvidence
from xeda._internal.flows import __builtin_flows__
from xeda._internal.platforms.model import Located, PlatformModel

pytestmark = pytest.mark.python_compat

OBJECT_METRICS = [
    pytest.param(flow, path, spec, id=f"{flow.name}:{path}")
    for flow in sorted(__builtin_flows__, key=lambda c: c.name)
    for path, spec in flow.results_description.items()
    if spec.type == "object"
]


def _closed(schema: Any) -> Any:
    """The schema with every object that names its properties closed."""
    if isinstance(schema, list):
        return [_closed(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    closed = {key: _closed(value) for key, value in schema.items()}
    if "properties" in closed:
        closed["additionalProperties"] = False
    return closed


def _names(schema: dict[str, Any]) -> set[str]:
    """Every key that the schema of an object names, at every depth."""
    children = [
        *schema.get("properties", {}).values(),
        *([schema["items"]] if "items" in schema else []),
    ]
    return set(schema.get("properties", {})).union(*(_names(child) for child in children))


def test_the_built_in_flows_have_object_metrics() -> None:
    assert {param.values[1] for param in OBJECT_METRICS} == {
        "sim.evidence",
        "platform",
        "path_groups",
    }


@pytest.mark.parametrize(("flow", "path", "spec"), OBJECT_METRICS)
def test_an_object_metric_has_a_schema_of_an_object(
    flow: type, path: str, spec: MetricSpec
) -> None:
    assert spec.schema is not None
    assert spec.schema["type"] == "object"
    jsonschema.Draft202012Validator.check_schema(spec.schema)


def test_no_other_metric_has_a_schema() -> None:
    for flow in __builtin_flows__:
        for path, spec in flow.results_description.items():
            assert (spec.schema is not None) == (spec.type == "object"), f"{flow.name}.{path}"


@pytest.mark.parametrize(("flow", "path", "spec"), OBJECT_METRICS)
def test_the_description_names_every_key_of_the_schema(
    flow: type, path: str, spec: MetricSpec
) -> None:
    assert spec.schema is not None
    for key in _names(dict(spec.schema)):
        assert f"`{key}`" in spec.description, f"{path}: the description lacks `{key}`"


def test_the_schema_of_the_simulation_evidence_is_the_model() -> None:
    schema = SIM_EVIDENCE_SCHEMA
    assert set(schema["properties"]) == set(SimEvidence.model_fields)
    assert set(schema["required"]) == set(SimEvidence.model_fields)
    event = schema["properties"]["events"]["items"]
    assert set(event["properties"]) == set(SimEvent.model_fields)
    assert set(event["required"]) == set(SimEvent.model_fields)


def test_the_simulation_evidence_that_the_writer_writes_fits_the_schema() -> None:
    validator = jsonschema.Draft202012Validator(_closed(SIM_EVIDENCE_SCHEMA))
    end = SimEvidence(ended_by="unknown")
    full = SimEvidence(
        ended_by="finish",
        time=5,
        time_unit="1ps",
        cycles=3,
        events=[
            SimEvent(kind="error", time=4, location="a.v:3", message="bad"),
            SimEvent(kind="finish"),
        ],
        exit_code=0,
    )
    for evidence in (end, full):
        validator.validate(evidence.model_dump(mode="json"))


def test_the_platform_that_the_writer_writes_fits_the_schema(tmp_path: Path) -> None:
    model = PlatformModel(name="p", corner=None, sources={"name": "user", "clock": "upstream"})
    located = Located(
        provider="orfs",
        version="v1",
        root=tmp_path,
        directory=tmp_path,
        corner="tt",
        files={},
        upstream_files=[],
    )
    flow = SimpleNamespace(platform_model=model, platform_located=located)
    result = AsicPlatformFlow.platform_result(flow)  # type: ignore[arg-type]
    assert result is not None
    assert set(result) == set(PLATFORM_SCHEMA["properties"])
    jsonschema.validate(result, _closed(PLATFORM_SCHEMA))
