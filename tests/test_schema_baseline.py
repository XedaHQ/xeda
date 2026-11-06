"""The document schemas: one for each document that Xeda writes or reads.

The tool `tools/api_baseline.py` derives them from the code, compares them with the newest release
baseline, and ships them in the package (`xeda/data/schemas/`). These tests check that every real
document validates against its schema, that a schema keeps what the baseline promised, and that the
classifier tells a break from an addition by the direction of each document.
"""

import copy
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

from .test_cli_json_contract import SQRT, run_xeda
from .source_layout import SCHEMAS_DIR
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
    shipped = SCHEMAS_DIR
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
        "dse", FLOW, str(SQRT), "--optimizer", "fmax_optimizer", "--initial-frequency-low", "100 MHz",
        "--initial-frequency-high", "200 MHz", "--max-workers", "1", cwd=tmp_path, fake_tools=True,
    )  # fmt: skip


def _failure() -> Any:
    proc = run_xeda("list-settings", "no_such_flow", "--json")
    assert proc.returncode == 2, proc.stderr
    return json.loads(proc.stdout)


PRODUCERS = {
    "list-flows": lambda tmp_path: _json("list-flows"),
    "list-settings": lambda tmp_path: _json("list-settings", FLOW),
    "list-results": lambda tmp_path: _json("list-results", FLOW),
    "list-boards": lambda tmp_path: _json("list-boards"),
    "list-platforms": lambda tmp_path: _json("list-platforms"),
    "list-optimizers": lambda tmp_path: _json("list-optimizers"),
    "failure": lambda tmp_path: _failure(),
    "run": lambda tmp_path: _run(tmp_path),
    "dry-run": lambda tmp_path: _run(tmp_path, "--dry-run"),
    "scrub": _scrub,
    "dse": _dse,
    # The design schema validates a design file, not the output of a command.
    "design": lambda tmp_path: yaml.safe_load(SQRT.read_text(encoding="utf-8")),
}


#: The documents that another test validates: the record of a run (below), the PDK documents
#: (`test_pdk_cli.py`) and the remote documents (`test_remote_commands.py`,
#: `test_remote_keep_running.py`).
VALIDATED_ELSEWHERE = {
    "results",
    "pdk-list",
    "pdk-path",
    "pdk-install",
    "remote-check",
    "remote-clean",
    "remote-cancel",
}


def test_every_document_has_a_producer() -> None:
    assert sorted(PRODUCERS) == sorted(set(_schemas()) - VALIDATED_ELSEWHERE)


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
        # an output document: any other type or enum breaks a reader
        ("run", STRING, {"type": "integer"}, "breaks"),
        ("run", STRING, NULLABLE, "breaks"),
        ("run", NULLABLE, STRING, "breaks"),
        ("run", {"enum": ["a", "b"]}, {"enum": ["a"]}, "breaks"),
        ("run", {"enum": ["a"]}, {"enum": ["a", "b"]}, "breaks"),
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
            {"type": "array", "prefixItems": [EITHER, STRING]},
            {"type": "array", "prefixItems": [STRING, STRING]},
            "breaks",
        ),
        (
            "design",
            {"type": "array", "prefixItems": [STRING, STRING]},
            {"type": "array", "prefixItems": [EITHER, STRING]},
            "adds",
        ),
        (
            "run",
            {"type": "array", "prefixItems": [STRING]},
            {"type": "array", "prefixItems": [STRING, STRING]},
            "breaks",
        ),
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


def _source_entry(types: list[str], keys: tuple[str, ...] = ("file", "path", "type")) -> dict:
    """A design-file entry like a source: `oneOf` beside its own `properties`."""
    properties = {key: STRING for key in keys}
    if "type" in properties:
        properties["type"] = {"type": "string", "enum": types}
    return {
        "type": "object",
        "oneOf": [{"required": ["file"]}, {"required": ["path"]}],
        "properties": properties,
    }


@pytest.mark.parametrize(
    "new",
    [_source_entry(["Verilog"]), _source_entry(["Verilog", "VHDL"], keys=("file", "type"))],
    ids=["a narrower enum", "a removed key"],
)
def test_the_keywords_beside_one_of_are_compared(new: dict) -> None:
    old = {"design": _source_entry(["Verilog", "VHDL"])}
    assert tool.classify(old, {"design": new}, kind="schema")[1]


def _effect(document: str, old: Any, new: Any) -> str:
    additive, found = _classify({document: _property(old)}, {document: _property(new)})
    return "breaks" if found else "adds" if additive else "same"


NUM = {"type": "number"}
OBJ = {"type": "object"}
ARR = {"type": "array"}
# old, new, effect on the design file (input), effect on a written document (output)
RULES = {
    "integer to number": ({"type": "integer"}, NUM, "adds", "breaks"),
    "number to integer": (NUM, {"type": "integer"}, "breaks", "breaks"),
    "anyOf to oneOf": ({"anyOf": [NUM]}, {"oneOf": [NUM]}, "breaks", "breaks"),
    "oneOf to anyOf": ({"oneOf": [NUM, STRING]}, {"anyOf": [NUM, STRING]}, "breaks", "breaks"),
    "minimum looser": (NUM | {"minimum": 2}, NUM | {"minimum": 1}, "adds", "breaks"),
    "minimum tighter": (NUM | {"minimum": 1}, NUM | {"minimum": 2}, "breaks", "adds"),
    "minimum removed": (NUM | {"minimum": 1}, NUM, "adds", "breaks"),
    "minimum added": (NUM, NUM | {"minimum": 1}, "breaks", "adds"),
    "maximum looser": (NUM | {"maximum": 1}, NUM | {"maximum": 2}, "adds", "breaks"),
    "maximum tighter": (NUM | {"maximum": 2}, NUM | {"maximum": 1}, "breaks", "adds"),
    "maximum equal": (NUM | {"maximum": 2}, NUM | {"maximum": 2.0}, "same", "same"),
    "minimum made exclusive": (NUM | {"minimum": 1}, NUM | {"exclusiveMinimum": 1}, "breaks", "adds"),
    "exclusive minimum made inclusive": (
        NUM | {"exclusiveMinimum": 1}, NUM | {"minimum": 1}, "adds", "breaks",
    ),
    "exclusive minimum tighter": (
        NUM | {"exclusiveMinimum": 1}, NUM | {"exclusiveMinimum": 2}, "breaks", "adds",
    ),
    "maximum made exclusive": (NUM | {"maximum": 1}, NUM | {"exclusiveMaximum": 1}, "breaks", "adds"),
    "exclusive maximum looser": (
        NUM | {"exclusiveMaximum": 1}, NUM | {"exclusiveMaximum": 2}, "adds", "breaks",
    ),
    "minLength looser": (STRING | {"minLength": 2}, STRING | {"minLength": 1}, "adds", "breaks"),
    "maxLength tighter": (STRING | {"maxLength": 2}, STRING | {"maxLength": 1}, "breaks", "adds"),
    "minItems looser": (ARR | {"minItems": 2}, ARR | {"minItems": 1}, "adds", "breaks"),
    "maxItems added": (ARR, ARR | {"maxItems": 1}, "breaks", "adds"),
    "minProperties removed": (OBJ | {"minProperties": 1}, OBJ, "adds", "breaks"),
    "maxProperties tighter": (
        OBJ | {"maxProperties": 2}, OBJ | {"maxProperties": 1}, "breaks", "adds",
    ),
    "multipleOf divisor": (NUM | {"multipleOf": 4}, NUM | {"multipleOf": 2}, "adds", "breaks"),
    "multipleOf multiple": (NUM | {"multipleOf": 2}, NUM | {"multipleOf": 4}, "breaks", "adds"),
    "multipleOf fraction": (NUM | {"multipleOf": 0.5}, NUM | {"multipleOf": 0.25}, "adds", "breaks"),
    "multipleOf of decimals": (NUM | {"multipleOf": 0.3}, NUM | {"multipleOf": 0.1}, "adds", "breaks"),
    "multipleOf unrelated": (NUM | {"multipleOf": 2}, NUM | {"multipleOf": 3}, "breaks", "breaks"),
    "multipleOf removed": (NUM | {"multipleOf": 2}, NUM, "adds", "breaks"),
    "multipleOf added": (NUM, NUM | {"multipleOf": 2}, "breaks", "adds"),
    "uniqueItems added": (ARR, ARR | {"uniqueItems": True}, "breaks", "adds"),
    "uniqueItems removed": (ARR | {"uniqueItems": True}, ARR, "adds", "breaks"),
    "uniqueItems false added": (ARR, ARR | {"uniqueItems": False}, "same", "same"),
    "const to enum": ({"const": "a"}, {"enum": ["a", "b"]}, "adds", "breaks"),
    "enum to const": ({"enum": ["a", "b"]}, {"const": "a"}, "breaks", "breaks"),
    "const changed": ({"const": "a"}, {"const": "b"}, "breaks", "breaks"),
    "const equals one-value enum": ({"const": "a"}, {"enum": ["a"]}, "same", "same"),
    "const removed": ({"const": "a"}, {}, "adds", "breaks"),
    "pattern changed": (STRING | {"pattern": "a"}, STRING | {"pattern": "b"}, "breaks", "breaks"),
    "pattern removed": (STRING | {"pattern": "a"}, STRING, "adds", "breaks"),
    "pattern added": (STRING, STRING | {"pattern": "a"}, "breaks", "adds"),
    "format changed": (STRING | {"format": "uri"}, STRING | {"format": "date"}, "breaks", "breaks"),
    "format removed": (STRING | {"format": "uri"}, STRING, "adds", "breaks"),
    "format added": (STRING, STRING | {"format": "uri"}, "breaks", "adds"),
    "default changed": (STRING | {"default": "a"}, STRING | {"default": "b"}, "breaks", "breaks"),
    "default added": (STRING, STRING | {"default": "a"}, "same", "same"),
    "default removed": (STRING | {"default": "a"}, STRING, "same", "same"),
    "not changed": (
        {"not": {"type": "null"}}, {"not": {"type": "string"}}, "breaks", "breaks",
    ),
    "propertyNames tighter": (
        OBJ, OBJ | {"propertyNames": {"pattern": "^a"}}, "breaks", "adds",
    ),
    "propertyNames looser": (
        OBJ | {"propertyNames": {"maxLength": 1}}, OBJ | {"propertyNames": {"maxLength": 3}},
        "adds", "breaks",
    ),
    "pattern property removed": (
        OBJ | {"patternProperties": {"^a": STRING}}, OBJ, "breaks", "breaks",
    ),
    "pattern property added": (OBJ, OBJ | {"patternProperties": {"^a": STRING}}, "adds", "adds"),
    "pattern property type": (
        OBJ | {"patternProperties": {"^a": STRING}},
        OBJ | {"patternProperties": {"^a": NUM}},
        "breaks", "breaks",
    ),
    "pattern property widened": (
        OBJ | {"patternProperties": {"^a": STRING}},
        OBJ | {"patternProperties": {"^a": NULLABLE}},
        "adds", "breaks",
    ),
    "open to closed": (OBJ, OBJ | {"additionalProperties": False}, "breaks", "adds"),
    "true to closed": (
        OBJ | {"additionalProperties": True}, OBJ | {"additionalProperties": False}, "breaks", "adds",
    ),
    "closed to open": (OBJ | {"additionalProperties": False}, OBJ, "adds", "adds"),
    "closed to true": (
        OBJ | {"additionalProperties": False}, OBJ | {"additionalProperties": True}, "adds", "adds",
    ),
    "open to true": (OBJ, OBJ | {"additionalProperties": True}, "same", "same"),
    "open to schema": (OBJ, OBJ | {"additionalProperties": STRING}, "breaks", "adds"),
    "schema to open": (OBJ | {"additionalProperties": STRING}, OBJ, "adds", "breaks"),
    "schema to closed": (
        OBJ | {"additionalProperties": STRING}, OBJ | {"additionalProperties": False}, "breaks", "adds",
    ),
    "closed to schema": (
        OBJ | {"additionalProperties": False}, OBJ | {"additionalProperties": STRING}, "adds", "adds",
    ),
    "schema to schema": (
        OBJ | {"additionalProperties": STRING}, OBJ | {"additionalProperties": NULLABLE},
        "adds", "breaks",
    ),
    "bound beside a branch": (
        {"anyOf": [STRING, NUM], "minLength": 1}, {"anyOf": [STRING, NUM], "minLength": 2},
        "breaks", "adds",
    ),
}  # fmt: skip


@pytest.mark.parametrize("name", sorted(RULES))
def test_the_classifier_judges_every_keyword_by_the_direction_of_the_document(name: str) -> None:
    old, new, as_input, as_output = RULES[name]
    assert (_effect("design", old, new), _effect("run", old, new)) == (as_input, as_output)


@pytest.mark.parametrize(
    "keyword",
    ["title", "description", "examples", "$comment", "deprecated", "readOnly", "writeOnly", "x-a"],
)
def test_a_metadata_keyword_is_not_judged(keyword: str) -> None:
    assert _effect("design", STRING, STRING | {keyword: "text"}) == "same"
    assert _effect("run", STRING | {keyword: "a"}, STRING | {keyword: "b"}) == "same"


@pytest.mark.parametrize("keyword", ["allOf", "if", "contains", "dependentRequired", "unknown"])
def test_a_keyword_that_the_classifier_does_not_judge_is_an_error(keyword: str) -> None:
    with pytest.raises(ValueError, match=keyword):
        _effect("design", STRING, STRING | {keyword: []})
