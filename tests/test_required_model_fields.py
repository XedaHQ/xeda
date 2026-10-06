"""A flow whose settings model requires a field launches when the value is given, wherever it is
given, and is refused with a clear error when it is given nowhere.

The convention is a `required_settings` entry, which the launch checks. A flow written outside
Xeda may still require a field in its model, so the resolver, which checks parts of a flow's
settings (one origin's sections, one shared leaf) before they are composed, must not turn a part
that lacks the field into an error. It checks the parts with the flow's own model: a validator
written for a field's type sees that type, and an error names the flow's own settings class.
"""

from pathlib import Path
from typing import ClassVar

import pytest
import yaml

from xeda import Design
from xeda.dataclass import Field, field_validator, model_validator
from xeda.flow import Flow, FlowSettingsError
from xeda.flow_runner import DefaultRunner


class _RequiredLeaf(Flow):
    """Declares nothing, and its settings model requires one field."""

    results_description: ClassVar[dict[str, str]] = {}

    class Settings(Flow.Settings):
        foo: str = Field(description="A setting the model requires.")

    def run(self) -> None:
        self.results["foo"] = self.settings.foo


class _RequiredChecked(Flow):
    """Requires a path and a label, each checked by a validator written for its type."""

    results_description: ClassVar[dict[str, str]] = {}

    class Settings(Flow.Settings):
        where: Path = Field(description="A required path, which its validator expands.")
        label: str = Field(description="A required label, checked once the model is built.")
        note: str = Field("", description="An optional note.")

        @field_validator("where")
        @classmethod
        def _expand(cls, value: Path) -> Path:
            return value.expanduser()  # a method of the type: it needs a Path, not raw text

        @model_validator(mode="after")
        def _label_ends_well(self):
            if self.label.lower().endswith("bad"):  # reads a required field
                raise ValueError("a label may not end with `bad`")
            return self

    def run(self) -> None:
        self.results["where"] = str(self.settings.where)
        self.results["label"] = self.settings.label


def _design_file(root: Path, flows: dict | None = None) -> Path:
    data: dict = {"name": "d", "rtl": {"sources": [], "top": "t"}}
    if flows is not None:
        data["flows"] = flows
    path = root / "d.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def _project_file(root: Path, flows: dict) -> Path:
    path = root / "xedaproject.yaml"
    path.write_text(
        yaml.safe_dump({"flows": flows, "design": [{"name": "d", "rtl": {"sources": []}}]})
    )
    return path


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return DefaultRunner(tmp_path / "xeda_run", display_results=False)


@pytest.fixture
def design(tmp_path):
    return Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "t"})


GIVEN = {"where": "a/b", "label": "ok"}
NAME = _RequiredChecked.name


def _launch(how: str, runner: DefaultRunner, design: Design, root: Path) -> Flow:
    """Launch `_RequiredChecked` with `GIVEN` given the way `how` says."""
    cls = _RequiredChecked
    if how == "design file":
        return runner.run(cls, _design_file(root, {NAME: GIVEN}))
    if how == "project file":
        return runner.run(cls, "d", xedaproject=str(_project_file(root, {NAME: GIVEN})))
    if how == "-s":
        flow_settings = [f"{key}={value}" for key, value in GIVEN.items()]
        return runner.run(cls, _design_file(root), flow_settings=flow_settings)
    if how == "design file and -s":
        return runner.run(
            cls, _design_file(root, {NAME: {"where": "a/b"}}), flow_settings=["label=ok"]
        )
    if how == "API dict":
        return runner.launch_flow(cls, design, dict(GIVEN))
    if how == "run_flow":
        return runner.run_flow(cls, design, dict(GIVEN))
    if how == "instance":
        return runner.launch_flow(cls, design, cls.Settings(**GIVEN))
    if how == "sections":
        return runner.launch_flow(cls, design, {}, all_flows_settings={NAME: dict(GIVEN)})
    assert how == "sections and API dict"
    return runner.launch_flow(
        cls, design, {"label": "ok"}, all_flows_settings={NAME: {"where": "a/b"}}
    )


WAYS = [
    "design file",
    "project file",
    "-s",
    "design file and -s",
    "API dict",
    "run_flow",
    "instance",
    "sections",
    "sections and API dict",
]


@pytest.mark.parametrize("how", WAYS)
def test_a_required_typed_setting_is_given_to_its_validators_as_its_type(
    how, runner, design, tmp_path
):
    """The path validator calls a method of `Path`, and the model validator reads the label, in
    every way a setting can be given."""
    flow = _launch(how, runner, design, tmp_path)
    assert flow is not None and flow.succeeded
    assert Path(flow.results["where"]) == Path("a/b") and flow.results["label"] == "ok"


@pytest.mark.parametrize("how", ["API dict", "sections", "design file"])
def test_a_value_its_model_validator_refuses_is_refused_by_the_complete_settings(
    how, runner, design, tmp_path
):
    """The settings given in parts are never judged by a validator that needs them all: the
    complete settings are, once every part is composed."""
    cls = _RequiredChecked
    bad = {"where": "a/b", "label": "very bad"}
    with pytest.raises(FlowSettingsError, match="may not end with `bad`") as refused:
        if how == "API dict":
            runner.launch_flow(cls, design, bad)
        elif how == "sections":
            runner.launch_flow(cls, design, {}, all_flows_settings={NAME: bad})
        else:
            runner.run(cls, _design_file(tmp_path, {NAME: bad}))
    assert f"{cls.__name__}.Settings" in str(refused.value)


@pytest.mark.parametrize("how", ["API dict", "sections", "design file"])
def test_a_setting_given_nowhere_is_a_clear_error_naming_it_and_the_flow(
    how, runner, design, tmp_path
):
    cls = _RequiredChecked
    given = {"note": "only an optional one"}
    with pytest.raises(FlowSettingsError) as refused:
        if how == "API dict":
            runner.launch_flow(cls, design, given)
        elif how == "sections":
            runner.launch_flow(cls, design, {}, all_flows_settings={NAME: given})
        else:
            runner.run(cls, _design_file(tmp_path, {NAME: given}))
    message = str(refused.value)
    assert f"{cls.__name__}.Settings" in message
    assert "Field required: where" in message and "Field required: label" in message
    assert "AttributeError" not in message


def test_a_required_text_setting_is_launched_by_dict_by_instance_and_by_a_section(runner, design):
    for flow in (
        runner.launch_flow(_RequiredLeaf, design, {"foo": "x"}),
        runner.launch_flow(_RequiredLeaf, design, _RequiredLeaf.Settings(foo="x")),
        runner.run_flow(_RequiredLeaf, design, {"foo": "x"}),
        runner.launch_flow(
            _RequiredLeaf, design, {}, all_flows_settings={_RequiredLeaf.name: {"foo": "x"}}
        ),
    ):
        assert flow.succeeded and flow.results["foo"] == "x"


def test_a_required_text_setting_given_nowhere_is_still_refused(runner, design):
    with pytest.raises(FlowSettingsError, match="foo"):
        runner.launch_flow(_RequiredLeaf, design, {})
