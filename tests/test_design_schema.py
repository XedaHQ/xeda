"""JSON Schema and source-type serialization of design descriptions.

`Design.model_json_schema()` used to raise on undeclarable types, leaving
editors and coding agents with no machine-readable description of a design file.
"""

import json
import pathlib

import pytest

from xeda.design import Design, DesignSource, SourceType
from xeda.introspect import JSON_SCHEMA_DIALECT, design_schema

jsonschema = pytest.importorskip("jsonschema")


def test_design_schema_is_generatable():
    schema = Design.model_json_schema()
    assert schema["properties"]
    for required in ("name", "rtl"):
        assert required in schema["properties"]


def test_design_source_schema_documents_the_object_form():
    sources = Design.model_json_schema()["$defs"]["RtlSettings"]["properties"]["sources"]
    item = sources["items"]
    assert item["description"]
    string_form, object_form = item["anyOf"]
    assert string_form["type"] == "string"
    props = object_form["properties"]
    assert set(props) >= {"file", "path", "type", "standard", "variant"}
    assert props["type"]["enum"] == [t.name for t in SourceType]


def test_source_type_values_are_names_not_ordinals():
    """Serialized settings/results must be self-describing: "Vhdl", never "5"."""
    for member in SourceType:
        assert member.value == member.name


def test_source_type_from_str():
    assert SourceType.from_str("Vhdl") is SourceType.Vhdl
    assert SourceType.from_str("vhdl") is SourceType.Vhdl
    assert SourceType.from_str("SystemVerilog") is SourceType.SystemVerilog
    assert SourceType.from_str("nonesuch") is None


def test_source_type_from_str_accepts_legacy_ordinals():
    """settings.json written by older xeda versions recorded 1-based `auto()` ordinals."""
    members = list(SourceType)
    for ordinal, member in enumerate(members, start=1):
        assert SourceType.from_str(str(ordinal)) is member
    assert SourceType.from_str(str(len(members) + 1)) is None


def test_design_source_type_inferred_from_suffix(tmp_path):
    src = tmp_path / "foo.vhdl"
    src.write_text("entity foo is end;\n")
    assert DesignSource(src).type is SourceType.Vhdl


# --------------------------------------------------------------- input syntax vs. the loader

EXAMPLES_DIR = pathlib.Path(__file__).parent.parent / "examples"

#: Design files shipped as examples. Everything the loader accepts, the published schema must
#: accept too, or `xeda design-schema` misleads anyone generating or validating a design file.
EXAMPLE_DESIGNS = sorted(
    p
    for p in EXAMPLES_DIR.rglob("*")
    if p.suffix in (".toml", ".yaml", ".yml")
    and p.name != "xedaproject.toml"
    and "xeda_run" not in p.parts
)

#: Shorthands the model's validators accept, which a schema built from the model alone rejects.
INPUT_SHORTHANDS = {
    "tb.top as a bare string": {
        "name": "d",
        "rtl": {"sources": [], "top": "t"},
        "tb": {"sources": [], "top": "tb"},
    },
    "tb.cocotb = true": {
        "name": "d",
        "rtl": {"sources": [], "top": "t"},
        "tb": {"sources": [], "cocotb": True},
    },
    "language (field name)": {
        "name": "d",
        "rtl": {"sources": [], "top": "t"},
        "language": {"vhdl": {"standard": "2008"}},
    },
    "hdl (alias)": {
        "name": "d",
        "rtl": {"sources": [], "top": "t"},
        "hdl": {"vhdl": {"standard": "2008"}},
    },
    "language.vhdl as a string": {
        "name": "d",
        "rtl": {"sources": [], "top": "t"},
        "language": {"vhdl": "2008"},
    },
    "language.vhdl as an int": {
        "name": "d",
        "rtl": {"sources": [], "top": "t"},
        "language": {"vhdl": 2008},
    },
    "authors as a string": {
        "name": "d",
        "rtl": {"sources": [], "top": "t"},
        "authors": "Jane Doe <jane@example.com>",
    },
    "flat top-level form": {"name": "d", "sources": [], "top": "t", "clock": {"port": "clk"}},
    "test as a tb alias": {"name": "d", "sources": [], "top": "t", "test": {"sources": []}},
}


def load_design_file(path: pathlib.Path):
    import tomllib

    import yaml

    if path.suffix in (".yaml", ".yml"):
        return yaml.safe_load(path.read_text())
    return tomllib.loads(path.read_bytes().decode())


def validator():
    return jsonschema.Draft202012Validator(design_schema())


def test_published_schema_is_a_valid_json_schema():
    """The declared dialect must be the one pydantic actually emitted.

    pydantic v2 produces draft 2020-12 shapes (`$defs`, `prefixItems`). Stamping any other
    draft on the document would let a validator silently apply the wrong rules.
    """
    schema = design_schema()
    assert schema["$schema"] == JSON_SCHEMA_DIALECT
    jsonschema.Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize("path", EXAMPLE_DESIGNS, ids=lambda p: p.name)
def test_example_designs_validate_against_the_published_schema(path):
    """Guards against the schema drifting from what the loader actually accepts."""
    Design.from_file(path)  # the loader accepts it...
    errors = sorted(validator().iter_errors(load_design_file(path)), key=lambda e: list(e.path))
    assert not errors, f"{path.name} loads but fails the published schema: " + "; ".join(
        f"{'.'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errors[:3]
    )


@pytest.mark.parametrize("label", sorted(INPUT_SHORTHANDS))
def test_documented_shorthands_are_accepted_by_both(label):
    data = INPUT_SHORTHANDS[label]
    Design(**data)  # the loader accepts it...
    errors = sorted(validator().iter_errors(data), key=lambda e: list(e.path))
    assert not errors, f"schema rejects {label}: {[e.message for e in errors[:2]]}"


def test_source_object_must_name_a_file_or_a_path():
    """FileResource rejects an object giving neither, and so must the schema."""
    bad = {"name": "d", "rtl": {"sources": [{"type": "Verilog"}], "top": "t"}}
    assert not validator().is_valid(bad)
    for good in ({"file": "a.v"}, {"path": "generated/a.v"}, "a.v"):
        ok = {"name": "d", "rtl": {"sources": [good], "top": "t"}}
        assert validator().is_valid(ok), good


def test_source_object_may_not_name_both_a_file_and_a_path():
    """`FileResource` treats 'file' and 'path' as mutually exclusive, and so must the schema.

    The object branch used to be an `anyOf` of the two `required` clauses, which an object
    carrying both satisfies -- so `xeda design-schema` validated a design that
    `Design.from_file` rejects with "'file' and 'path' are mutually exclusive."
    """
    both = {"file": "a.v", "path": "generated/a.v"}
    with pytest.raises(ValueError, match="mutually exclusive"):
        DesignSource(both)
    assert not validator().is_valid({"name": "d", "rtl": {"sources": [both], "top": "t"}})


SOURCE_OBJECT_FORMS = {
    "file": {"file": "design0.sv"},
    "file+type": {"file": "design0.sv", "type": "SystemVerilog"},
    "unknown key ignored": {"file": "design0.sv", "bogus": 1},
}


@pytest.mark.parametrize("label", sorted(SOURCE_OBJECT_FORMS))
def test_source_object_form_loads_and_validates(label, tmp_path):
    """The object form the schema advertises must actually load.

    `_sources_to_files` deduplicates its input with `unique()`, which hashed every element --
    so a source given as an object failed with `unhashable type: 'dict'` before the validator
    ever looked at it, even though `DesignSource` accepts a dict. The schema documented a form
    the loader could not read.
    """
    (tmp_path / "design0.sv").write_text("module design0; endmodule\n")
    design_file = tmp_path / "d.json"
    data = {"name": "d", "rtl": {"top": "design0", "sources": [SOURCE_OBJECT_FORMS[label]]}}
    design_file.write_text(json.dumps(data))

    design = Design.from_file(design_file)  # the loader accepts it...
    assert [p.file.name for p in design.rtl.sources] == ["design0.sv"]
    assert validator().is_valid(data), "schema rejects a form the loader accepts"


def test_model_schema_is_still_available():
    """`input_syntax=False` describes the validated object rather than the accepted input."""
    model = design_schema(input_syntax=False)
    assert "sources" not in model["properties"]  # no flat form
    assert "rtl" in model.get("required", [])
