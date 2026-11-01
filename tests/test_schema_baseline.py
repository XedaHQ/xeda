"""The document schemas: one for each document that Xeda writes or reads.

The tool `tools/api_baseline.py` derives them from the code, compares them with the newest release
baseline, and ships them in the package (`xeda/data/schemas/`). These tests check that every real
document validates against its schema, that a schema keeps what the baseline promised, and that the
classifier tells a break from an addition by the direction of each document.
"""

import copy
import json
from importlib import resources
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

from .test_cli_json_contract import SQRT, run_xeda
from .test_public_api import tool

pytestmark = pytest.mark.python_compat

DOCUMENTS = Path(__file__).parent / "resources" / "documents"
FLOW = "vivado_synth"
CLOCK = f"{FLOW}.clocks.main_clock.period=5.5ns"


def _schemas() -> dict[str, dict[str, Any]]:
    return tool.document_schemas()


def _classify(old: Any, new: Any) -> tuple[list[str], list[str]]:
    return tool.classify(old, new, kind="schema")


def test_every_schema_is_compatible_with_the_newest_baseline() -> None:
    baseline = json.loads((tool.newest_baseline() / "schemas.json").read_text(encoding="utf-8"))
    additive, breaking = _classify(baseline, _schemas())
    assert not breaking, "\n".join(breaking)


def test_every_document_has_a_schema_file_in_the_package() -> None:
    shipped = resources.files("xeda.data").joinpath("schemas")
    for name, schema in _schemas().items():
        text = shipped.joinpath(f"{name}.schema.json").read_text(encoding="utf-8")
        assert json.loads(text) == schema


@pytest.mark.parametrize("name", sorted(_schemas()))
def test_every_schema_is_a_json_schema(name: str) -> None:
    jsonschema.Draft202012Validator.check_schema(_schemas()[name])


# --------------------------------------------------------------------- real documents validate


def _closed(schema: Any) -> Any:
    """The schema with every object closed: a key that no `properties` names is an error. A
    document that has a key without a schema fails, as a document that lacks a required key does."""
    if isinstance(schema, list):
        return [_closed(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    closed = {key: _closed(value) for key, value in schema.items()}
    if "properties" in closed and "additionalProperties" not in closed:
        closed["additionalProperties"] = False
    return closed


def _validate(document: Any, name: str) -> None:
    schema = _schemas()[name]
    jsonschema.validate(document, schema if name == "design" else _closed(schema))


def _json(*arguments: str, cwd: Path | None = None, fake_tools: bool = False) -> Any:
    proc = run_xeda(*arguments, "--json", cwd=cwd, fake_tools=fake_tools)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _run(tmp_path: Path, *arguments: str) -> Any:
    return _json("run", FLOW, str(SQRT), "-s", CLOCK, *arguments, cwd=tmp_path, fake_tools=True)


def _scrub(tmp_path: Path) -> Any:
    _run(tmp_path)
    return _json("scrub", FLOW, "sqrt", "--yes", cwd=tmp_path, fake_tools=True)


def _dse(tmp_path: Path) -> Any:
    return _json(
        "dse", FLOW, str(SQRT), "--optimizer", "fmax_optimizer", "--init-freq-low", "100",
        "--init-freq-high", "200", "--max-workers", "1", cwd=tmp_path, fake_tools=True,
    )  # fmt: skip


PRODUCERS = {
    "list-flows": lambda tmp_path: _json("list-flows"),
    "list-settings": lambda tmp_path: _json("list-settings", FLOW),
    "list-results": lambda tmp_path: _json("list-results", FLOW),
    "list-boards": lambda tmp_path: _json("list-boards"),
    "list-platforms": lambda tmp_path: _json("list-platforms"),
    "list-optimizers": lambda tmp_path: _json("list-optimizers"),
    "run": lambda tmp_path: _run(tmp_path),
    "dry-run": lambda tmp_path: _run(tmp_path, "--dry-run"),
    "scrub": _scrub,
    "dse": _dse,
    # The design schema validates a design file, not the output of a command.
    "design": lambda tmp_path: yaml.safe_load(SQRT.read_text(encoding="utf-8")),
}


def test_every_document_has_a_producer() -> None:
    assert sorted(PRODUCERS) == sorted(set(_schemas()) - {"results"})


@pytest.mark.parametrize("name", sorted(PRODUCERS))
def test_every_document_validates_against_its_schema(name: str, tmp_path: Path) -> None:
    _validate(PRODUCERS[name](tmp_path), name)


def test_the_record_of_a_run_validates_against_the_results_schema(tmp_path: Path) -> None:
    document = _run(tmp_path)
    (node,) = document["nodes"]
    _validate(json.loads((Path(node["run_dir"]) / "results.json").read_text("utf-8")), "results")


FIXTURES = {
    "dry-run-1.0.json": "dry-run",
    "list-platforms-1.0.json": "list-platforms",
    "run-1.0.json": "run",
    "results-1.0.json": "results",
}


@pytest.mark.parametrize("fixture", sorted(FIXTURES))
def test_a_fixture_of_release_1_0_validates_against_the_schema(fixture: str) -> None:
    _validate(json.loads((DOCUMENTS / fixture).read_text(encoding="utf-8")), FIXTURES[fixture])


# ----------------------------------------------------------------------------- the classifier


def test_a_removed_document_field_is_breaking() -> None:
    old = _schemas()
    new = copy.deepcopy(old)
    del new["list-platforms"]["properties"]["platforms"]["items"]["properties"]["directory"]
    additive, breaking = _classify(old, new)
    assert breaking == ["list-platforms.platforms.items.directory: property removed"]
    assert not additive


def test_a_newly_required_design_key_is_breaking() -> None:
    old = _schemas()
    new = copy.deepcopy(old)
    assert "rtl" in new["design"]["properties"] and "rtl" not in new["design"].get("required", [])
    new["design"]["required"] = [*new["design"].get("required", []), "rtl"]
    additive, breaking = _classify(old, new)
    assert breaking == ["design.rtl: now required"]
    assert not additive


def test_a_new_optional_output_field_is_additive() -> None:
    old = _schemas()
    new = copy.deepcopy(old)
    node = new["run"]["properties"]["nodes"]["items"]
    node["properties"]["seed"] = {"type": "integer"}
    additive, breaking = _classify(old, new)
    assert additive == ["run.nodes.items.seed: new property"]
    assert not breaking


def test_a_new_optional_design_key_is_additive() -> None:
    old = _schemas()
    new = copy.deepcopy(old)
    new["design"]["properties"]["seed"] = {"type": "integer"}
    additive, breaking = _classify(old, new)
    assert additive == ["design.seed: new property"]
    assert not breaking


def test_a_removed_document_is_breaking_and_a_new_one_is_additive() -> None:
    schemas = _schemas()
    assert _classify(schemas, {k: v for k, v in schemas.items() if k != "run"})[1]
    assert _classify({k: v for k, v in schemas.items() if k != "run"}, schemas)[0]


STRING = {"type": "string"}
NULLABLE = {"type": ["string", "null"]}
EITHER = {"anyOf": [STRING, {"type": "integer"}]}


def _property(schema: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"type": "object", "properties": {"x": schema}, **extra}


@pytest.mark.parametrize(
    "document, old, new, effect",
    [
        # an output document: any other type, and a narrower enum, break a reader
        ("run", STRING, {"type": "integer"}, "breaks"),
        ("run", STRING, NULLABLE, "breaks"),
        ("run", NULLABLE, STRING, "breaks"),
        ("run", {"enum": ["a", "b"]}, {"enum": ["a"]}, "breaks"),
        ("run", {"enum": ["a"]}, {"enum": ["a", "b"]}, "adds"),
        ("run", {"type": "array", "items": STRING}, {"type": "array", "items": NULLABLE}, "breaks"),
        ("run", STRING, STRING | {"description": "text"}, "same"),
        # the design file: what it accepts may widen, and may not narrow
        ("design", STRING, {"type": "integer"}, "breaks"),
        ("design", STRING, NULLABLE, "adds"),
        ("design", NULLABLE, STRING, "breaks"),
        ("design", STRING, EITHER, "adds"),
        ("design", EITHER, STRING, "breaks"),
        ("design", {"enum": ["a", "b"]}, {"enum": ["a"]}, "breaks"),
        ("design", {"enum": ["a"]}, {"enum": ["a", "b"]}, "adds"),
        (
            "design",
            {"type": "array", "items": STRING},
            {"type": "array", "items": NULLABLE},
            "adds",
        ),
        (
            "design",
            {"type": "array", "items": NULLABLE},
            {"type": "array", "items": STRING},
            "breaks",
        ),
        ("design", STRING, {}, "adds"),
        ("design", {}, STRING, "breaks"),
    ],
)
def test_the_classifier_follows_the_direction_of_the_document(
    document: str, old: Any, new: Any, effect: str
) -> None:
    additive, found = _classify({document: _property(old)}, {document: _property(new)})
    assert (bool(additive), bool(found)) == {
        "breaks": (False, True),
        "adds": (True, False),
        "same": (False, False),
    }[effect], (additive, found)


@pytest.mark.parametrize(
    "document, old_required, new_required, breaking",
    [
        ("run", ["x"], [], True),
        ("run", [], ["x"], False),
        ("design", [], ["x"], True),
        ("design", ["x"], [], False),
    ],
)
def test_a_change_of_a_required_key_follows_the_direction_of_the_document(
    document: str, old_required: list[str], new_required: list[str], breaking: bool
) -> None:
    old = {document: _property(STRING, required=old_required)}
    new = {document: _property(STRING, required=new_required)}
    additive, found = _classify(old, new)
    assert bool(found) is breaking and bool(additive) is not breaking


def test_a_new_required_key_of_an_output_document_is_additive() -> None:
    old = {"run": _property(STRING)}
    new = {"run": {**old["run"], "properties": {"x": STRING, "y": STRING}, "required": ["y"]}}
    additive, breaking = _classify(old, new)
    assert additive and not breaking


def test_a_reference_is_compared_by_what_it_names() -> None:
    def schema(kind: str) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"x": {"$ref": "#/$defs/a"}},
            "$defs": {"a": {"type": kind}},
        }

    assert not any(_classify({"run": schema("string")}, {"run": schema("string")}))
    assert _classify({"run": schema("string")}, {"run": schema("integer")})[1]
