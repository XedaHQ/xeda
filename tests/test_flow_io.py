"""The declaration API (14-plan-2-spec section 2): a flow declares its files as nested `Inputs`
and `Outputs` models with `In(...)`/`Out(...)` fields; the annotation is the cardinality. A
malformed declaration is refused when the flow class is defined, and `xeda list-flows --json`
publishes every declaration."""

import json
from pathlib import Path
from typing import ClassVar

import pytest
from click.testing import CliRunner

from xeda import Design
from xeda.cli import cli
from xeda.dataclass import Field, ValidationError
from xeda.design import SourceType
from xeda.flow import Flow, In, Out, registered_flows
from xeda.flow.io import (
    InputDeclaration,
    OutputDeclaration,
    declared_inputs,
    declared_outputs,
    is_declared,
    output_enabled,
    switch_on,
)
from xeda.introspect import flow_info

from .io_flows import _Maker, _Place, _Taker, _Wrapper


@pytest.fixture(autouse=True)
def isolate_test_flow_registration():
    """Locally defined flows must not change another test's registry."""
    before = registered_flows.copy()
    yield
    registered_flows.clear()
    registered_flows.update(before)


def _flow_with_model(kind, annotation, declaration):
    base = Flow.Inputs if kind == "Inputs" else Flow.Outputs
    model = type(kind, (base,), {"__annotations__": {"file": annotation}, "file": declaration})
    return type(
        "_DeclarationProbe",
        (Flow,),
        {
            "__doc__": "A declaration validation probe.",
            "results_description": {},
            kind: model,
            "run": lambda self: None,
        },
    )


def test_the_declarations_are_read_from_the_nested_models():
    assert declared_outputs(_Maker) == {
        "made": OutputDeclaration(
            name="made",
            types=(SourceType.Data,),
            cardinality="optional",
            enabled_by="write",
            description="The file.",
        )
    }
    made = declared_inputs(_Taker)["made"]
    assert made == InputDeclaration(
        name="made",
        types=(SourceType.Data,),
        cardinality="one",
        producer="__maker",
        output="made",
        optional=False,
        description="What it reads.",
    )
    assert made.required
    assert declared_inputs(_Place)["netlist"].producer == "__synth"


def test_a_flow_with_no_declaration_is_undeclared():
    assert is_declared(_Maker) and is_declared(_Taker) and is_declared(_Place)
    assert not is_declared(_Wrapper)


def test_inputs_and_outputs_are_unset_until_set_and_validated_when_set(tmp_path):
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "t"})
    taker = _Taker({}, design, tmp_path / "run")
    assert taker.inputs.made is None
    taker.inputs.made = str(tmp_path / "x")
    assert taker.inputs.made == tmp_path / "x"
    assert _Maker({}, design, tmp_path / "run2").outputs.made is None


def test_a_field_not_made_with_in_is_refused():
    with pytest.raises(TypeError, match="not declared with In"):

        class _Unmarked(Flow):
            """Refused."""

            class Inputs(Flow.Inputs):
                netlist: Path = Field(None, description="x")

            def run(self) -> None:
                pass


def test_an_input_that_is_no_path_is_refused():
    with pytest.raises(TypeError, match=r"a `Path`, `Path \| None` or `list\[Path\]`"):

        class _Text(Flow):
            """Refused."""

            class Inputs(Flow.Inputs):
                netlist: str = In(SourceType.JsonNetlist, description="x")

            def run(self) -> None:
                pass


def test_an_output_switched_on_by_a_setting_it_does_not_have_is_refused():
    with pytest.raises(TypeError, match="names no setting"):

        class _Switchless(Flow):
            """Refused."""

            class Outputs(Flow.Outputs):
                netlist: Path | None = Out(
                    SourceType.JsonNetlist, enabled_by="write_netlist", description="x"
                )

            def run(self) -> None:
                pass


def test_an_output_switched_on_by_a_setting_must_be_allowed_to_be_absent():
    with pytest.raises(TypeError, match="may be absent"):

        class _Required(Flow):
            """Refused."""

            class Outputs(Flow.Outputs):
                netlist: Path = Out(SourceType.JsonNetlist, enabled_by="debug", description="x")

            def run(self) -> None:
                pass


def test_a_declaration_names_at_least_one_source_type():
    with pytest.raises(TypeError, match="one or more SourceType"):
        In([], description="x")


def test_list_flows_publishes_the_declarations():
    info = flow_info(_Taker)
    assert info["declared"] is True
    assert info["inputs"] == [
        {
            "name": "made",
            "types": ["Data"],
            "cardinality": "one",
            "producer": "__maker",
            "output": "made",
            "description": "What it reads.",
        }
    ]
    assert info["dependencies"] == ["__maker"]
    assert flow_info(_Maker)["outputs"] == [
        {
            "name": "made",
            "types": ["Data"],
            "cardinality": "optional",
            "enabled_by": "write",
            "description": "The file.",
        }
    ]
    assert flow_info(_Wrapper)["declared"] is False


@pytest.mark.parametrize(
    "types", [[], (), "Data", b"Data", ["Data"], [SourceType.Data, None], 1, None]
)
@pytest.mark.parametrize("declare", [In, Out])
def test_invalid_type_sequences_raise_type_error(types, declare):
    with pytest.raises(TypeError, match="SourceType"):
        declare(types, description="The file.")


@pytest.mark.parametrize("declare, kind", [(In, "Inputs"), (Out, "Outputs")])
@pytest.mark.parametrize("description", ["", "   "])
def test_blank_descriptions_are_refused(declare, kind, description):
    with pytest.raises(TypeError, match="description"):
        _flow_with_model(kind, Path, declare(SourceType.Data, description=description))


@pytest.mark.parametrize("annotation", [Path, Path | None])
def test_only_list_inputs_may_use_optional_true(annotation):
    with pytest.raises(TypeError, match=r"optional.*list"):
        _flow_with_model("Inputs", annotation, In(SourceType.Data, optional=True, description="x"))


def test_output_selection_requires_a_producer():
    with pytest.raises(TypeError, match=r"output.*producer"):
        _flow_with_model("Inputs", Path, In(SourceType.Data, output="made", description="x"))


def test_a_field_not_made_with_out_is_refused():
    with pytest.raises(TypeError, match="not declared with Out"):
        _flow_with_model("Outputs", Path, Field(None, description="x"))


def test_malformed_classes_are_not_registered():
    before = registered_flows.copy()
    with pytest.raises(TypeError):
        _flow_with_model("Inputs", str, In(SourceType.Data, description="x"))
    assert registered_flows == before


def test_inherited_declarations_keep_their_order_and_cardinality():
    class _Many(_Taker):
        """Extends a scalar input with an optional list and nullable input."""

        results_description: ClassVar[dict[str, str]] = {}

        class Inputs(_Taker.Inputs):
            files: list[Path] = In(
                (SourceType.Data, SourceType.C), optional=True, description="Files."
            )
            maybe: Path | None = In(SourceType.Data, description="A nullable file.")

    inputs = declared_inputs(_Many)
    assert list(inputs) == ["made", "files", "maybe"]
    assert inputs["made"] == declared_inputs(_Taker)["made"]
    assert inputs["files"].types == (SourceType.Data, SourceType.C)
    assert inputs["files"].cardinality == "many"
    assert not inputs["files"].required
    assert inputs["maybe"].cardinality == "optional"
    assert not inputs["maybe"].required
    assert list(declared_inputs(_Taker)) == ["made"]
    many_required = _flow_with_model(
        "Inputs", list[Path], In(SourceType.Data, description="Files.")
    )
    assert declared_inputs(many_required)["file"].required

    class _ManyOutputs(_Maker):
        """Extends an optional output with an ordered list output."""

        results_description: ClassVar[dict[str, str]] = {}

        class Outputs(_Maker.Outputs):
            files: list[Path] = Out((SourceType.Data, SourceType.C), description="Files.")

    outputs = declared_outputs(_ManyOutputs)
    assert list(outputs) == ["made", "files"]
    assert outputs["made"] == declared_outputs(_Maker)["made"]
    assert outputs["files"].cardinality == "many"
    assert outputs["files"].types == (SourceType.Data, SourceType.C)
    assert list(declared_outputs(_Maker)) == ["made"]


def test_list_flows_json_publishes_the_declarations():
    result = CliRunner().invoke(cli, ["list-flows", "--json"])
    assert result.exit_code == 0, result.output
    flows = {item["name"]: item for item in json.loads(result.stdout)}
    assert flows["__taker"]["inputs"] == flow_info(_Taker)["inputs"]
    assert flows["__maker"]["outputs"] == flow_info(_Maker)["outputs"]
    assert flows["__taker"]["dependencies"] == ["__maker"]
    assert flows["__wrapper"]["declared"] is False


def test_staged_models_validate_scalar_and_list_assignments_and_are_private(tmp_path):
    flow_cls = _flow_with_model(
        "Inputs", list[Path], In(SourceType.Data, optional=True, description="x")
    )
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "t"})
    first = flow_cls({}, design, tmp_path / "first")
    second = flow_cls({}, design, tmp_path / "second")
    assert first.inputs.file is None
    first.inputs.file = []  # The resolver resolves an absent optional list this way before run.
    assert first.inputs.file == []
    first.inputs.file = [str(tmp_path / "a"), tmp_path / "b"]
    assert first.inputs.file == [tmp_path / "a", tmp_path / "b"]
    assert second.inputs.file is None
    with pytest.raises(ValidationError):
        first.inputs.file = [False]
    with pytest.raises(ValidationError):
        first.inputs.extra = tmp_path / "extra"
    taker = _Taker({}, design, tmp_path / "taker")
    for value in (None, False, [], 42):
        with pytest.raises(ValidationError):
            taker.inputs.made = value
    maker = _Maker({}, design, tmp_path / "maker")
    maker.outputs.made = str(tmp_path / "made")
    assert maker.outputs.made == tmp_path / "made"
    with pytest.raises(ValidationError):
        maker.outputs.made = False
    maker.outputs.made = None


def test_declaration_metadata_is_json_schema_safe():
    schema = _Taker.Inputs.model_json_schema()
    assert schema["properties"]["made"]["x-xeda-io"] == {
        "kind": "input",
        "types": ["Data"],
        "producer": "__maker",
        "output": "made",
        "optional": False,
    }


class _Switches(Flow):
    """Outputs enabled by Boolean, text and path settings."""

    results_description: ClassVar[dict[str, str]] = {}

    class Settings(Flow.Settings):
        flag: bool | None = Field(None, description="A nullable flag.")
        path: Path | None = Field(None, description="A path with no default.")
        empty: str = Field("", description="Empty text.")
        text: str = Field("default.txt", description="Default text.")
        destination: Path = Field(Path("default.txt"), description="Default path.")
        files: list[Path] = Field(
            default_factory=lambda: [Path("default.txt")], description="Files."
        )

    class Outputs(Flow.Outputs):
        flag: Path | None = Out(SourceType.Data, enabled_by="flag", description="Flag output.")
        path: Path | None = Out(SourceType.Data, enabled_by="path", description="Path output.")
        empty: Path | None = Out(SourceType.Data, enabled_by="empty", description="Empty output.")
        text: Path | None = Out(SourceType.Data, enabled_by="text", description="Text output.")
        destination: Path | None = Out(
            SourceType.Data, enabled_by="destination", description="Path."
        )
        files: list[Path] = Out(SourceType.Data, enabled_by="files", description="File outputs.")
        always: Path = Out(SourceType.Data, description="Always enabled.")

    def run(self) -> None:
        pass


def test_output_enablement_uses_settings():
    settings = _Switches.Settings()
    outputs = declared_outputs(_Switches)
    assert not output_enabled(settings, outputs["flag"])
    assert not output_enabled(settings, outputs["path"])
    assert not output_enabled(settings, outputs["empty"])
    assert all(
        output_enabled(settings, outputs[name])
        for name in ("text", "destination", "files", "always")
    )
    settings.flag = False
    assert not output_enabled(settings, outputs["flag"])
    settings.flag = True
    settings.path = Path("named.txt")
    assert output_enabled(settings, outputs["flag"])
    assert output_enabled(settings, outputs["path"])
    settings.files = []
    assert not output_enabled(settings, outputs["files"])


def test_switch_on_uses_the_annotation_for_a_nullable_boolean():
    settings = _Switches.Settings()
    switch_on(settings, declared_outputs(_Switches)["flag"])
    assert settings.flag is True
    settings.flag = False
    switch_on(settings, declared_outputs(_Switches)["flag"])
    assert settings.flag is True
    maker_settings = _Maker.Settings(write=False)
    switch_on(maker_settings, declared_outputs(_Maker)["made"])
    assert maker_settings.write is True


@pytest.mark.parametrize("name", ["path", "empty"])
def test_unset_non_boolean_defaults_need_an_explicit_value(name):
    settings = _Switches.Settings()
    before = settings.model_dump()
    with pytest.raises(ValueError, match=f"give `{name}` a value"):
        switch_on(settings, declared_outputs(_Switches)[name])
    assert settings.model_dump() == before


@pytest.mark.parametrize("name", ["text", "destination", "files"])
def test_non_boolean_switches_restore_a_non_empty_default(name):
    settings = _Switches.Settings()
    if name == "text":
        settings.text = ""
    elif name == "files":
        settings.files = []
    else:
        settings.destination = Path("different.txt")
    switch_on(settings, declared_outputs(_Switches)[name])
    assert getattr(settings, name) == getattr(_Switches.Settings(), name)
    assert output_enabled(settings, declared_outputs(_Switches)[name])
