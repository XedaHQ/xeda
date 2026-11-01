"""The declaration API (14-plan-2-spec section 2): a flow declares its files as nested `Inputs`
and `Outputs` models with `In(...)`/`Out(...)` fields; the annotation is the cardinality. A
malformed declaration is refused when the flow class is defined, and `xeda list-flows --json`
publishes every declaration."""

import json
from pathlib import Path
from typing import ClassVar

import pytest
from click.testing import CliRunner

from xeda.flow import MetricSpec
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
    output_enabled,
    selected_types,
    switch_on,
)
from xeda.introspect import flow_class_info

from .flow_registry import preserved_flow_registry
from .io_flows import TChainUndeclared, TMaker, TPlace, TTaker


@pytest.fixture(autouse=True)
def isolate_test_flow_registration():
    """Locally defined flows must not change another test's registry."""
    with preserved_flow_registry():
        yield


def _flow_with_model(kind, annotation, declaration):
    base = Flow.Inputs if kind == "Inputs" else Flow.Outputs
    model = type(kind, (base,), {"__annotations__": {"file": annotation}, "file": declaration})
    return type(
        "DeclarationProbe",
        (Flow,),
        {
            "__doc__": "A declaration validation probe.",
            "results_description": {},
            kind: model,
            "run": lambda self: None,
        },
    )


def test_the_declarations_are_read_from_the_nested_models():
    assert declared_outputs(TMaker) == {
        "made": OutputDeclaration(
            name="made",
            types=(SourceType.Data,),
            cardinality="optional",
            enabled_by="write",
            description="The file.",
        )
    }
    made = declared_inputs(TTaker)["made"]
    assert made == InputDeclaration(
        name="made",
        types=(SourceType.Data,),
        cardinality="one",
        producer="t_maker",
        output="made",
        optional=False,
        description="What it reads.",
    )
    assert made.required
    assert declared_inputs(TPlace)["netlist"].producer == "t_synth"


def test_the_declarations_are_kept_per_model_and_a_caller_cannot_change_them():
    """Planning asks for the declarations thousands of times, so they are built once per
    `Inputs` or `Outputs` model. A result is the caller's own dict, and a flow that gets another
    model gets the declarations of that model."""
    for declare in (declared_inputs, declared_outputs):
        for flow in (TMaker, TTaker, TPlace):
            first = declare(flow)
            before = dict(first)
            first.clear()
            first["other"] = None
            assert declare(flow) == before

    class Other(TMaker):
        class Outputs(TMaker.Outputs):
            extra: Path | None = Out(SourceType.Data, description="Another file.")

    assert list(declared_outputs(Other)) == ["made", "extra"]
    assert list(declared_outputs(TMaker)) == ["made"]
    Other.Outputs = TMaker.Outputs
    assert list(declared_outputs(Other)) == ["made"]


def test_a_flow_with_no_declaration_declares_nothing():
    assert declared_outputs(TMaker) and declared_inputs(TTaker) and declared_inputs(TPlace)
    assert not declared_inputs(TChainUndeclared) and not declared_outputs(TChainUndeclared)


def test_default_pure_hooks_preserve_the_declared_types_and_enablement():
    settings = TMaker.Settings(write=False)
    assert TMaker.output_types(settings, "made") == (SourceType.Data,)
    assert TTaker.input_types(TTaker.Settings(), "made") == (SourceType.Data,)
    TMaker.enable_output(settings, "made", design_name="d")
    assert settings.write is True
    assert selected_types(TMaker, settings, "made", output=True) == (SourceType.Data,)


def test_default_hook_refuses_an_optional_output_without_a_switch():
    flow_cls = _flow_with_model("Outputs", Path | None, Out(SourceType.Data, description="File."))
    with pytest.raises(ValueError, match="cannot be switched on"):
        flow_cls.enable_output(flow_cls.Settings(), "file", design_name="d")


def test_a_specialization_cannot_expand_the_declared_vocabulary():
    class TExpanded(TMaker):
        """An invalid output specialization."""

        results_description = {}

        @classmethod
        def output_types(cls, settings, name):
            return (SourceType.Data, SourceType.Bitstream)

    with pytest.raises(ValueError, match="must narrow"):
        selected_types(TExpanded, TExpanded.Settings(), "made", output=True)


def test_a_required_specialized_input_can_be_supplied_without_a_default_producer(tmp_path):
    from xeda.flow_runner import DefaultRunner

    class TSourceOnly(Flow):
        """Read an explicitly supplied source of the selected type."""

        results_description = {}

        class Inputs(Flow.Inputs):
            file: Path = In((SourceType.Data, SourceType.Bitstream), description="Selected data.")

        @classmethod
        def input_types(cls, settings, name):
            return (SourceType.Data,)

        def run(self):
            pass

    (tmp_path / "data").write_text("given\n")
    design = Design(
        name="d",
        root=tmp_path,
        rtl={"sources": [{"file": "data", "type": "Data"}], "top": "t"},
    )
    plan = DefaultRunner(tmp_path / "run").plan(TSourceOnly, design)
    assert plan.nodes[0].inputs[0].sources == (tmp_path / "data",)


def test_inputs_and_outputs_are_unset_until_set_and_validated_when_set(tmp_path):
    design = Design(name="d", root=tmp_path, rtl={"sources": [], "top": "t"})
    taker = TTaker({}, design, tmp_path / "run")
    assert taker.inputs.made is None
    taker.inputs.made = str(tmp_path / "x")
    assert taker.inputs.made == tmp_path / "x"
    assert TMaker({}, design, tmp_path / "run2").outputs.made is None


def test_a_field_not_made_with_in_is_refused():
    with pytest.raises(TypeError, match="not declared with In"):

        class TUnmarked(Flow):
            """Refused."""

            class Inputs(Flow.Inputs):
                netlist: Path = Field(None, description="x")

            def run(self) -> None:
                pass


def test_an_input_that_is_no_path_is_refused():
    with pytest.raises(TypeError, match=r"a `Path`, `Path \| None` or `list\[Path\]`"):

        class TText(Flow):
            """Refused."""

            class Inputs(Flow.Inputs):
                netlist: str = In(SourceType.JsonNetlist, description="x")

            def run(self) -> None:
                pass


def test_an_output_switched_on_by_a_setting_it_does_not_have_is_refused():
    with pytest.raises(TypeError, match="names no setting"):

        class TSwitchless(Flow):
            """Refused."""

            class Outputs(Flow.Outputs):
                netlist: Path | None = Out(
                    SourceType.JsonNetlist, enabled_by="write_netlist", description="x"
                )

            def run(self) -> None:
                pass


def test_an_output_switched_on_by_a_setting_must_be_allowed_to_be_absent():
    with pytest.raises(TypeError, match="may be absent"):

        class TRequired(Flow):
            """Refused."""

            class Outputs(Flow.Outputs):
                netlist: Path = Out(SourceType.JsonNetlist, enabled_by="docker", description="x")

            def run(self) -> None:
                pass


def test_a_declaration_names_at_least_one_source_type():
    with pytest.raises(TypeError, match="one or more SourceType"):
        In([], description="x")


def test_list_flows_publishes_the_declarations():
    info = flow_class_info(TTaker)
    assert info["inputs"] == [
        {
            "name": "made",
            "value": "files",
            "types": ["Data"],
            "cardinality": "one",
            "required": True,
            "optional": False,
            "equivalence": False,
            "producer": "t_maker",
            "output": "made",
            "same_producer_as": None,
            "paired": False,
            "via": None,
            "description": "What it reads.",
        }
    ]
    assert info["dependencies"] == ["t_maker"]
    assert flow_class_info(TMaker)["outputs"] == [
        {
            "name": "made",
            "value": "files",
            "types": ["Data"],
            "cardinality": "optional",
            "enabled_by": "write",
            "description": "The file.",
        }
    ]


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
    class TMany(TTaker):
        """Extends a scalar input with an optional list and nullable input."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}

        class Inputs(TTaker.Inputs):
            files: list[Path] = In(
                (SourceType.Data, SourceType.C), optional=True, description="Files."
            )
            maybe: Path | None = In(SourceType.Data, description="A nullable file.")

    inputs = declared_inputs(TMany)
    assert list(inputs) == ["made", "files", "maybe"]
    assert inputs["made"] == declared_inputs(TTaker)["made"]
    assert inputs["files"].types == (SourceType.Data, SourceType.C)
    assert inputs["files"].cardinality == "many"
    assert not inputs["files"].required
    assert inputs["maybe"].cardinality == "optional"
    assert not inputs["maybe"].required
    assert list(declared_inputs(TTaker)) == ["made"]
    many_required = _flow_with_model(
        "Inputs", list[Path], In(SourceType.Data, description="Files.")
    )
    assert declared_inputs(many_required)["file"].required

    class TManyOutputs(TMaker):
        """Extends an optional output with an ordered list output."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}

        class Outputs(TMaker.Outputs):
            files: list[Path] = Out((SourceType.Data, SourceType.C), description="Files.")

    outputs = declared_outputs(TManyOutputs)
    assert list(outputs) == ["made", "files"]
    assert outputs["made"] == declared_outputs(TMaker)["made"]
    assert outputs["files"].cardinality == "many"
    assert outputs["files"].types == (SourceType.Data, SourceType.C)
    assert list(declared_outputs(TMaker)) == ["made"]


def test_list_flows_json_publishes_the_declarations():
    result = CliRunner().invoke(cli, ["list-flows", "--json"])
    assert result.exit_code == 0, result.output
    flows = {item["name"]: item for item in json.loads(result.stdout)}
    assert flows["t_taker"]["inputs"] == flow_class_info(TTaker)["inputs"]
    assert flows["t_maker"]["outputs"] == flow_class_info(TMaker)["outputs"]
    assert flows["t_taker"]["dependencies"] == ["t_maker"]


def test_staged_models_validate_scalar_and_list_assignments_and_are_private(tmp_path):
    flow_cls = _flow_with_model(
        "Inputs", list[Path], In(SourceType.Data, optional=True, description="x")
    )
    design = Design(name="d", root=tmp_path, rtl={"sources": [], "top": "t"})
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
    taker = TTaker({}, design, tmp_path / "taker")
    for value in (None, False, [], 42):
        with pytest.raises(ValidationError):
            taker.inputs.made = value
    maker = TMaker({}, design, tmp_path / "maker")
    maker.outputs.made = str(tmp_path / "made")
    assert maker.outputs.made == tmp_path / "made"
    with pytest.raises(ValidationError):
        maker.outputs.made = False
    maker.outputs.made = None


def test_declaration_metadata_is_json_schema_safe():
    schema = TTaker.Inputs.model_json_schema()
    assert schema["properties"]["made"]["x-xeda-io"] == {
        "kind": "input",
        "types": ["Data"],
        "producer": "t_maker",
        "output": "made",
        "optional": False,
    }


class TSwitches(Flow):
    """Outputs enabled by Boolean, text and path settings."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

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
    settings = TSwitches.Settings()
    outputs = declared_outputs(TSwitches)
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
    settings = TSwitches.Settings()
    switch_on(settings, declared_outputs(TSwitches)["flag"])
    assert settings.flag is True
    settings.flag = False
    switch_on(settings, declared_outputs(TSwitches)["flag"])
    assert settings.flag is True
    maker_settings = TMaker.Settings(write=False)
    switch_on(maker_settings, declared_outputs(TMaker)["made"])
    assert maker_settings.write is True


@pytest.mark.parametrize("name", ["path", "empty"])
def test_unset_non_boolean_defaults_need_an_explicit_value(name):
    settings = TSwitches.Settings()
    before = settings.model_dump()
    with pytest.raises(ValueError, match=f"give `{name}` a value"):
        switch_on(settings, declared_outputs(TSwitches)[name])
    assert settings.model_dump() == before


@pytest.mark.parametrize("name", ["text", "destination", "files"])
def test_non_boolean_switches_restore_a_non_empty_default(name):
    settings = TSwitches.Settings()
    if name == "text":
        settings.text = ""
    elif name == "files":
        settings.files = []
    else:
        settings.destination = Path("different.txt")
    switch_on(settings, declared_outputs(TSwitches)[name])
    assert getattr(settings, name) == getattr(TSwitches.Settings(), name)
    assert output_enabled(settings, declared_outputs(TSwitches)[name])
