"""YAML files use the 1.2 core schema without silent key overwrites."""

import math

import pytest
import yaml

from xeda.design import Design, DesignFileParseError, DesignValidationError
from xeda.xedaproject import XedaProject

SCALARS = [
    *[(s, s) for s in ("NO", "yes", "no", "on", "off", "y", "n", "Y", "N")],
    ("010", 10),
    ("0o10", 8),
    ("0o17", 15),
    ("0x1F", 31),
    ("1_000", "1_000"),
    ("1e-9", 1e-9),
    ("1e3", 1e3),
    ("1.0e-9", 1e-9),
    ("~", None),
    ("null", None),
    ("1:30", "1:30"),
    ("1:20", "1:20"),
    ("2026-10-03", "2026-10-03"),
    ('"off"', "off"),
    ("true", True),
    ("False", False),
    ("+010", 10),
    ("-010", -10),
    (".5", 0.5),
    ("1.", 1.0),
    ("-.Inf", -math.inf),
    (".NAN", math.nan),
    ('"010"', "010"),
    ('"0x1F"', "0x1F"),
    ('"1e3"', "1e3"),
    ("+0o17", "+0o17"),
    ("0b11", "0b11"),
    ("-.nan", "-.nan"),
]


@pytest.mark.parametrize("token,expected", SCALARS, ids=[s for s, _ in SCALARS])
@pytest.mark.parametrize("project", [False, True], ids=["design", "project"])
def test_core_scalars_in_real_files(tmp_path, token, expected, project):
    body = f"name: probe\nrtl:\n  sources: []\n  parameters: {{P: {token}}}\n"
    path = tmp_path / ("xedaproject.yaml" if project else "probe.yaml")
    path.write_text(
        "designs:\n- " + body.replace("\n", "\n  ").rstrip() + "\n" if project else body
    )
    design = XedaProject.from_file(path).get_design(0) if project else Design.from_file(path)
    actual = design.rtl.parameters["P"]
    assert type(actual) is type(expected)
    if isinstance(expected, float) and math.isnan(expected):
        assert math.isnan(actual)
    else:
        assert actual == expected


@pytest.mark.parametrize("project", [False, True], ids=["design", "project"])
def test_duplicate_keys_name_the_file_line_and_key(tmp_path, project):
    path = tmp_path / ("xedaproject.yaml" if project else "probe.yaml")
    body = (
        "flows:\n  vivado_synth:\n    timing_allow_fail: false\n    timing_allow_fail: true\n"
        if project
        else "name: probe\nrtl:\n  sources: []\n  parameters:\n    WIDTH: 8\n    WIDTH: 16\n"
    )
    path.write_text(body)
    with pytest.raises(yaml.MarkedYAMLError if project else DesignFileParseError) as raised:
        (XedaProject.from_file if project else Design.from_file)(path)
    message = str(raised.value)
    assert str(path) in message
    assert "duplicate" in message.lower()
    assert ("timing_allow_fail" if project else "WIDTH") in message
    assert f"line {4 if project else 6}" in message
    assert f"line {3 if project else 5}" in message


@pytest.mark.parametrize("key", ["true", "010", "null", "[a, b]", "{a: b}"])
@pytest.mark.parametrize("project", [False, True], ids=["design", "project"])
def test_non_string_keys_are_rejected_before_model_processing(tmp_path, key, project):
    path = tmp_path / ("xedaproject.yml" if project else "probe.yml")
    path.write_text(f"flows: {{nextpnr: {{{key}: 1}}}}\n")
    with pytest.raises(yaml.MarkedYAMLError if project else DesignFileParseError) as raised:
        (XedaProject.from_file if project else Design.from_file)(path)
    assert "mapping keys must be strings" in str(raised.value)
    assert str(path) in str(raised.value)


def test_legacy_boolean_keys_remain_distinct_strings(tmp_path):
    path = tmp_path / "probe.yaml"
    path.write_text("name: probe\nrtl: {sources: [], parameters: {ON: 1, yes: 2}}\n")
    assert Design.from_file(path).rtl.parameters == {"ON": 1, "yes": 2}


@pytest.mark.parametrize(
    "value",
    [
        "!!int 1:30",
        "!!int 1_000",
        "!!bool yes",
        "!!float 1:20",
        "!!null nope",
        "!!timestamp 2026-10-03",
        "!!python/object:builtins.object {}",
        "{<<: {a: 1}}",
        "&cycle {self: *cycle}",
        "&cycle [*cycle]",
    ],
)
def test_tags_merges_and_recursive_aliases_fail_clearly(tmp_path, value):
    path = tmp_path / "probe.yaml"
    path.write_text(f"name: probe\nrtl: {{sources: [], parameters: {{P: {value}}}}}\n")
    with pytest.raises(DesignFileParseError) as raised:
        Design.from_file(path)
    assert str(path) in str(raised.value)
    assert raised.value.line == 2


def test_explicit_core_tags_and_ordinary_aliases(tmp_path):
    path = tmp_path / "probe.yaml"
    path.write_text(
        "name: probe\nrtl:\n  sources: []\n  parameters:\n"
        "    A: &a [!!int 010, !!str off, !!float 1e3, !!bool true]\n"
        '    B: *a\n    "<<": text\n'
    )
    design = Design.from_file(path)
    assert design.rtl.parameters == {
        "A": [10, "off", 1000.0, True],
        "B": [10, "off", 1000.0, True],
        "<<": "text",
    }


def test_loader_does_not_change_pyyaml_globals(tmp_path):
    path = tmp_path / "probe.yaml"
    path.write_text("name: probe\nrtl: {sources: [], parameters: {P: off}}\n")
    Design.from_file(path)
    assert yaml.safe_load("off") is False


@pytest.mark.parametrize(
    "sources,error",
    [
        ("    - a.sv\n      - b.sv\n", DesignValidationError),
        ("    - a.sv\n   - b.sv\n", DesignFileParseError),
    ],
)
def test_malformed_source_indentation_has_a_clear_error(tmp_path, sources, error):
    (tmp_path / "a.sv").write_text("module a; endmodule\n")
    (tmp_path / "b.sv").write_text("module b; endmodule\n")
    path = tmp_path / "probe.yaml"
    path.write_text("name: probe\nrtl:\n  top: a\n  sources:\n" + sources)
    with pytest.raises(error) as raised:
        Design.from_file(path)
    assert str(path) in str(raised.value)


@pytest.mark.parametrize("project", [False, True])
def test_multiple_yaml_documents_are_rejected(tmp_path, project):
    path = tmp_path / ("xedaproject.yaml" if project else "probe.yaml")
    path.write_text("name: first\n---\nname: second\n")
    with pytest.raises(yaml.MarkedYAMLError if project else DesignFileParseError):
        (XedaProject.from_file if project else Design.from_file)(path)
