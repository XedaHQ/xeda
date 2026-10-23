"""Settings groups: nested settings models with tiers, flag tables and checked extra arguments.

A group is a `SettingsGroup`, a field of the settings of a flow. These tests use a toy tree of
groups on a toy flow, because no product flow has to carry one for the machinery to hold.
"""

import copy
import json
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, Optional, Union

import pytest

from xeda.flow import MetricSpec
from xeda.dataclass import (
    Field,
    ValidationError,
    WORKING,
    extras,
    tier,
    tier_of,
)
from xeda.design import Design
from xeda.flow import Flow, FlowSettingsError, flowrun_hash
from xeda.flow.settings_group import Flag, SettingsGroup, group_problems, render_flags
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.settings_layers import compose_flow_settings
from xeda.units import Bounds, Capacitance, Time
from xeda.utils import dump_json, tcl_word

from .flow_registry import preserved_flow_registry
from .settings_samples import PROBES

pytestmark = pytest.mark.python_compat


class _Sub(SettingsGroup):
    """A switchable group inside the toy group."""

    FLAGS: ClassVar[tuple[Flag, ...]] = (
        Flag("enabled", "-skip_sub", "skip"),
        Flag("knob", "-knob", "value"),
    )

    enabled: bool = Field(True, description="Run the sub-step.", json_schema_extra=tier("common"))
    knob: Optional[int] = Field(
        None, description="A number for the sub-step.", json_schema_extra=tier("advanced")
    )


class _Toy(SettingsGroup):
    """A group with one field of each kind of flag."""

    FLAGS: ClassVar[tuple[Flag, ...]] = (
        Flag("sizing", "-sizing", "value"),
        Flag("value", "-value", "value"),
        Flag("items", "-items", "list"),
        Flag("words", "-words", "words"),
        Flag("fast", "-fast", "switch"),
        Flag("swap", "-skip_swap", "skip"),
    )
    EXTRA_FLAGS: ClassVar[frozenset[str]] = frozenset({"-free"})
    removed_settings: ClassVar[dict[str, str]] = {"old": "`toy.value`"}

    sizing: Literal["a", "b"] = Field(
        "a", description="Which sizing to use.", json_schema_extra=tier("common")
    )
    value: Optional[float] = Field(
        None, ge=0, le=1, description="A ratio.", json_schema_extra=tier("common")
    )
    items: list[str] = Field(
        [], description="Items given as a list.", json_schema_extra=tier("advanced")
    )
    words: list[str] = Field(
        [], description="Words given after the flag.", json_schema_extra=tier("advanced")
    )
    fast: bool = Field(False, description="Be fast.", json_schema_extra=tier("expert"))
    swap: bool = Field(True, description="Swap pins.", json_schema_extra=tier("advanced"))
    sub: _Sub = Field(default_factory=_Sub, description="The sub-step.")
    extra_args: list[str] = Field(
        [], description="More flags of the command.", json_schema_extra=tier("expert")
    )
    path: Optional[Path] = Field(None, description="A file.", json_schema_extra=tier("expert"))


@pytest.fixture
def toy_flow():
    """A flow whose settings hold a group. It registers itself, so the registry is restored."""
    with preserved_flow_registry():

        class ToyGroups(Flow):
            """A flow that exists to carry a group."""

            results_description: ClassVar[dict[str, MetricSpec]] = {}

            class Settings(Flow.Settings):
                toy: _Toy = Field(default_factory=_Toy, description="The toy group.")

            def run(self) -> None:
                pass

        yield ToyGroups


def _settings(flow, *layers):
    """The settings of `flow` as `xeda run` builds them from `-s` items."""
    return flow.Settings.from_input(compose_flow_settings(flow, [], *layers))


def _messages(error: FlowSettingsError) -> list[str]:
    return [message for _, message, _, _ in error.errors]


def _design(root: Path, flows: str = "") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "top.v").write_text("module top; endmodule\n")
    path = root / "design.yaml"
    path.write_text(
        "name: d\nrtl:\n  sources: [top.v]\n  top: top\n" + (f"flows:\n{flows}" if flows else "")
    )
    return path


# ------------------------------------------------------------------------------- dotted keys


def test_a_dotted_command_line_key_sets_a_leaf_three_levels_down(toy_flow):
    settings = _settings(toy_flow, ["toy.sub.knob=3"])
    assert settings.toy.sub.knob == 3


def test_a_dotted_key_at_two_levels_and_one_level_sets_the_leaf_and_the_group(toy_flow):
    settings = _settings(toy_flow, ["toy.sizing=b", "toy.sub.enabled=false"])
    assert settings.toy.sizing == "b"
    assert settings.toy.sub.enabled is False


def test_a_nested_table_and_a_dotted_key_in_a_flows_section_give_the_same_settings(toy_flow):
    nested = compose_flow_settings(toy_flow, [{toy_flow.name: {"toy": {"sub": {"knob": 3}}}}])
    dotted = compose_flow_settings(toy_flow, [{toy_flow.name: {"toy.sub.knob": 3}}])
    assert nested == dotted == {"toy": {"sub": {"knob": 3}}}
    assert toy_flow.Settings.from_input(dotted).toy.sub.knob == 3


def test_a_design_file_takes_a_dotted_key_and_a_nested_table(toy_flow, tmp_path):
    runner = DefaultRunner(tmp_path / "runs", display_results=False)
    for name, flows in {
        "dotted": f"  {toy_flow.name}:\n    toy.sub.knob: 3\n",
        "nested": f"  {toy_flow.name}:\n    toy:\n      sub:\n        knob: 3\n",
    }.items():
        plan = runner.plan(toy_flow, _design(tmp_path / name, flows))
        assert plan.node(toy_flow.name).settings.toy.sub.knob == 3, name


def test_the_command_line_key_reaches_the_planned_settings(toy_flow, tmp_path):
    runner = DefaultRunner(tmp_path / "runs", display_results=False)
    plan = runner.plan(
        toy_flow, _design(tmp_path / "d"), flow_settings=["toy.sub.knob=3", "toy.sub.enabled=true"]
    )
    assert plan.node(toy_flow.name).settings.toy.sub.knob == 3


def test_a_group_leaf_is_validated_where_the_key_is_given(toy_flow):
    with pytest.raises(FlowSettingsError) as raised:
        _settings(toy_flow, ["toy.value=2", "toy.sizing=c"])
    locations = {location for location, *_ in raised.value.errors}
    assert locations == {"toy -> value", "toy -> sizing"}


def test_a_path_variable_at_a_group_leaf_is_expanded_once(toy_flow, tmp_path):
    root = tmp_path / "root"
    settings = toy_flow.Settings.from_input(
        {"toy": {"path": "$DESIGN_ROOT/x.txt"}}, design_root=root, runner_cwd=tmp_path
    )
    assert settings.toy.path == root / "x.txt"
    settings.toy.path = None
    again = toy_flow.Settings.from_input(settings.model_dump(), design_root=root)
    assert again.toy.path is None


# --------------------------------------------------------------------------- mis-cased keys


@pytest.mark.parametrize(
    "key, hint",
    [
        ("toy.sub.Knob=3", "knob"),
        ("toy.Sub.knob=3", "sub"),
        ("Toy.sub.knob=3", "toy"),
        ("toy.SIZING=b", "sizing"),
        ("Nthreads=2", "nthreads"),
    ],
)
def test_a_mis_cased_key_is_refused_with_the_right_spelling(toy_flow, key, hint):
    with pytest.raises(FlowSettingsError) as raised:
        _settings(toy_flow, [key])
    (message,) = _messages(raised.value)
    assert message == f"Extra inputs are not permitted; did you mean `{hint}`?"


def test_a_mis_cased_key_with_dashes_names_the_right_spelling(toy_flow):
    with pytest.raises(FlowSettingsError) as raised:
        _settings(toy_flow, ["toy.Extra-Args=-free"])
    assert "did you mean `extra_args`?" in _messages(raised.value)[0]


def test_a_mis_cased_key_is_refused_by_a_planning_entry_point(toy_flow, tmp_path):
    runner = DefaultRunner(tmp_path / "runs", display_results=False)
    with pytest.raises(FlowSettingsError, match="did you mean `knob`"):
        runner.plan(toy_flow, _design(tmp_path / "d"), flow_settings=["toy.sub.Knob=3"])


def test_a_key_with_no_case_insensitive_match_keeps_the_plain_message(toy_flow):
    with pytest.raises(FlowSettingsError) as raised:
        _settings(toy_flow, ["toy.sub.nothing=3", "nothing=1"])
    assert _messages(raised.value) == ["Extra inputs are not permitted"] * 2


# ------------------------------------------------------------------------------ render_flags


def _render(group, **kwargs):
    return render_flags(group, tcl_word, **kwargs)


def test_render_flags_prints_each_kind_in_table_order(toy_flow):
    group = _Toy(
        sizing="b",
        value=0.5,
        items=["a", "b"],
        words=["x", "y z"],
        fast=True,
        swap=False,
        extra_args=["-free", "1"],
    )
    assert _render(group) == (
        '-sizing "b" -value "0.5" -items [list "a" "b"] -words "x" "y z" -fast -skip_swap '
        '"-free" "1"'
    )


def test_render_flags_prints_nothing_for_a_default_group():
    assert _render(_Toy()) == '-sizing "a"'


def test_render_flags_prints_nothing_for_none_and_for_empty_lists():
    group = _Toy(value=None, items=[], words=[])
    assert "-value" not in _render(group)
    assert "-items" not in _render(group)
    assert "-words" not in _render(group)


def test_a_switch_prints_for_true_only_and_a_skip_for_false_only():
    assert "-fast" not in _render(_Toy(fast=False))
    assert "-fast" in _render(_Toy(fast=True))
    assert "-skip_swap" not in _render(_Toy(swap=True))
    assert "-skip_swap" in _render(_Toy(swap=False))


def test_a_list_prints_a_yosys_form_through_the_given_list_word():
    group = _Toy(items=["a", "b"])
    text = render_flags(
        group, tcl_word, list_word=lambda items: tcl_word(",".join(map(str, items)))
    )
    assert '-items "a,b"' in text


def test_extra_args_words_come_last_each_through_quote():
    group = _Toy(items=["a"], extra_args=["-free", "x y"])
    assert _render(group).endswith('"-free" "x y"')


def test_a_disabled_sub_group_renders_only_its_enabled_flag():
    assert _render(_Sub(enabled=False, knob=3)) == "-skip_sub"
    assert _render(_Sub(enabled=True, knob=3)) == '-knob "3"'


def test_render_flags_of_a_parent_never_renders_a_sub_group():
    group = _Toy(sub=_Sub(enabled=False, knob=3))
    assert "-skip_sub" not in _render(group)
    assert "-knob" not in _render(group)
    assert _render(group.sub) == "-skip_sub"


# ------------------------------------------------------------------------------ group_problems


def _problems(toy_flow, **toy):
    return group_problems(toy_flow.Settings(toy=_Toy(**toy)))


def test_a_disabled_sub_group_with_a_setting_is_refused(toy_flow):
    assert _problems(toy_flow, sub={"enabled": False, "knob": 3}) == [
        ("toy.sub.knob", "`toy.sub.knob` has no effect while `toy.sub.enabled` is false")
    ]


def test_a_disabled_sub_group_with_only_defaults_passes(toy_flow):
    assert _problems(toy_flow, sub={"enabled": False}) == []


def test_a_disabled_group_is_judged_against_its_defaults_and_not_against_what_was_set(toy_flow):
    """A reloaded `settings.json` sets every field, so being set is no evidence."""
    settings = toy_flow.Settings(toy={"sub": {"enabled": False, "knob": None}})
    assert group_problems(settings) == []


def test_a_disabled_group_refuses_the_leaves_of_its_own_sub_groups():
    class Outer(SettingsGroup):
        enabled: bool = True
        inner: _Sub = Field(default_factory=_Sub)

    class Holder(Flow.Settings):
        outer: Outer = Field(default_factory=Outer)

    problems = group_problems(Holder(outer={"enabled": False, "inner": {"knob": 2}}))
    assert problems == [
        ("outer.inner.knob", "`outer.inner.knob` has no effect while `outer.enabled` is false")
    ]


@pytest.mark.parametrize("items", [["-free", "1"], ["-free", "-0.5"], ["-free"], []])
def test_extra_args_of_an_allowed_flag_pass(toy_flow, items):
    assert _problems(toy_flow, extra_args=items) == []


def test_extra_args_naming_a_flag_a_setting_renders_are_refused_naming_the_setting(toy_flow):
    assert _problems(toy_flow, extra_args=["-value", "1"]) == [
        (
            "toy.extra_args",
            "`-value` in `toy.extra_args` is the setting `toy.value`: set that instead",
        )
    ]


def test_extra_args_with_an_unknown_flag_list_the_allowed_flags(toy_flow):
    ((key, message),) = _problems(toy_flow, extra_args=["-unknown"])
    assert key == "toy.extra_args"
    assert "`-unknown`" in message and "-free" in message


def test_extra_args_must_start_with_a_flag(toy_flow):
    assert _problems(toy_flow, extra_args=["1"]) == [
        ("toy.extra_args", "`toy.extra_args` must start with a flag, not `1`")
    ]


def test_all_problems_of_one_settings_object_come_back_at_once(toy_flow):
    problems = _problems(
        toy_flow, extra_args=["-unknown", "-value"], sub={"enabled": False, "knob": 1}
    )
    assert [key for key, _ in problems] == ["toy.extra_args", "toy.extra_args", "toy.sub.knob"]


def test_a_group_adds_its_own_problems():
    class Strict(SettingsGroup):
        a: bool = False
        b: bool = False

        def problems(self, key: str) -> list[tuple[str, str]]:
            return (
                [(f"{key}.a", f"`{key}.a` and `{key}.b` exclude each other")]
                if (self.a and self.b)
                else []
            )

    class Holder(Flow.Settings):
        strict: Strict = Field(default_factory=Strict)

    assert group_problems(Holder(strict={"a": True, "b": True})) == [
        ("strict.a", "`strict.a` and `strict.b` exclude each other")
    ]
    assert group_problems(Holder(strict={"a": True})) == []


def test_the_base_check_raises_one_error_listing_every_problem(toy_flow):
    settings = toy_flow.Settings(
        toy=_Toy(extra_args=["-unknown"], sub={"enabled": False, "knob": 1})
    )
    with pytest.raises(FlowSettingsError) as raised:
        toy_flow.check_settings_supported(settings)
    assert [location for location, *_ in raised.value.errors] == ["toy.extra_args", "toy.sub.knob"]
    assert raised.value.model is toy_flow.Settings


def test_the_base_check_passes_settings_without_a_problem(toy_flow):
    toy_flow.check_settings_supported(toy_flow.Settings())


def test_planning_refuses_a_bad_group_before_any_run_directory_exists(toy_flow, tmp_path):
    runner = DefaultRunner(tmp_path / "runs", display_results=False)
    with pytest.raises(FlowSettingsError, match="toy.sub.knob") as raised:
        runner.plan(
            toy_flow,
            _design(tmp_path / "d"),
            flow_settings=["toy.sub.enabled=false", "toy.sub.knob=3", "toy.extra_args=-nope"],
        )
    assert len(raised.value.errors) == 2
    assert not (tmp_path / "runs").exists()


def test_a_launch_refuses_a_bad_group_before_any_run_directory_exists(toy_flow, tmp_path):
    runner = DefaultRunner(tmp_path / "runs", display_results=False)
    with pytest.raises(FlowSettingsError, match="toy.extra_args"):
        runner.run_flow(
            toy_flow, Design(**_toy_design(tmp_path / "d")), {"toy": {"extra_args": ["-nope"]}}
        )
    assert not (tmp_path / "runs").exists()


def _toy_design(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    (root / "top.v").write_text("module top; endmodule\n")
    return {"name": "d", "design_root": root, "rtl": {"sources": ["top.v"], "top": "top"}}


# --------------------------------------------------------------------------- removed settings


def test_a_removed_group_leaf_names_what_replaces_it(toy_flow):
    with pytest.raises(FlowSettingsError) as raised:
        toy_flow.Settings.from_input({"toy": {"old": 1}})
    assert _messages(raised.value) == ["`old` was removed: use `toy.value`"]


def test_a_removed_group_leaf_given_on_the_command_line_names_what_replaces_it(toy_flow):
    with pytest.raises(FlowSettingsError, match="`old` was removed: use `toy.value`"):
        _settings(toy_flow, ["toy.old=1"])


def test_a_removed_leaf_of_a_sub_group_is_refused_by_that_group():
    class Inner(SettingsGroup):
        removed_settings: ClassVar[dict[str, str]] = {"gone": "`inner.now`"}
        now: int = 0

    with pytest.raises(ValidationError, match="`gone` was removed: use `inner.now`"):
        Inner(gone=1)


@pytest.mark.parametrize(
    "value", [3, "old", "toy", ["old"], 2.5], ids=["int", "removed-name", "text", "list", "float"]
)
def test_a_value_that_is_no_table_is_refused_as_the_wrong_type(toy_flow, value):
    """The group has removed settings, and the value is no mapping: a number, a text that names a
    removed leaf, a list. Each one is refused as the wrong type, never with a traceback and never
    as if it named a removed leaf."""
    with pytest.raises(FlowSettingsError) as raised:
        toy_flow.Settings.from_input({"toy": value})
    ((location, message, _, kind),) = raised.value.errors
    assert (location, kind) == ("toy", "model_type")
    assert message.startswith("Input should be a valid dictionary or instance of _Toy")


@pytest.mark.parametrize("item", ["toy=3", "toy=old", "toy=a,b"])
def test_a_command_line_value_that_is_no_table_is_refused_as_the_wrong_type(toy_flow, item):
    with pytest.raises(FlowSettingsError) as raised:
        _settings(toy_flow, [item])
    ((location, _, _, kind),) = raised.value.errors
    assert (location, kind) == ("toy", "model_type")


@pytest.mark.parametrize("value", [3, "old", ["old"]], ids=["int", "removed-name", "list"])
def test_assigning_a_value_that_is_no_table_to_a_group_is_refused(toy_flow, value):
    settings = toy_flow.Settings()
    with pytest.raises(ValidationError, match="model_type"):
        settings.toy = value
    assert settings.toy == _Toy()


# ------------------------------------------------------------------------------- list text


def test_list_text_at_a_group_leaf_is_a_comma_separated_list(toy_flow):
    settings = _settings(toy_flow, ["toy.extra_args=-free,1"])
    assert settings.toy.extra_args == ["-free", "1"]


def test_list_text_spelled_as_a_list_is_refused_with_the_list_text_message(toy_flow):
    with pytest.raises(FlowSettingsError) as raised:
        _settings(toy_flow, ["toy.extra_args=[a]"])
    ((location, message, _, kind),) = raised.value.errors
    assert kind == "list_text"
    assert "`[a]` is text, not a list: write `toy.extra_args=`" in message


def test_list_text_in_a_sub_group_names_the_key_from_the_root():
    class Inner(SettingsGroup):
        names: list[str] = []

    class Holder(Flow.Settings):
        deep: Inner = Field(default_factory=Inner)

    with pytest.raises(FlowSettingsError, match=r"write `deep.names=`"):
        Holder.from_input({"deep": {"names": "[x]"}})


def test_an_already_list_value_is_unchanged(toy_flow):
    settings = toy_flow.Settings.from_input({"toy": {"extra_args": ["-free", "a,b"]}})
    assert settings.toy.extra_args == ["-free", "a,b"]


def test_an_empty_text_for_a_list_is_the_empty_list(toy_flow):
    assert _settings(toy_flow, ["toy.items="]).toy.items == []


def test_an_empty_text_for_an_optional_path_is_unset(toy_flow):
    assert _settings(toy_flow, ["toy.path="]).toy.path is None


def test_assigning_list_text_converts_it(toy_flow):
    settings = toy_flow.Settings()
    settings.toy.items = "a, b"
    assert settings.toy.items == ["a", "b"]
    with pytest.raises(ValidationError):
        settings.toy.items = "[a]"


def test_the_input_mapping_is_not_changed(toy_flow):
    given = {"toy": {"items": "a,b", "path": ""}}
    before = copy.deepcopy(given)
    toy_flow.Settings.from_input(given)
    assert given == before


# ----------------------------------------------------------------------------------- tiers


def test_a_tier_shows_in_the_json_schema_of_the_leaf():
    schema = _Toy.model_json_schema()["properties"]
    assert schema["sizing"]["x-xeda-tier"] == "common"
    assert schema["items"]["x-xeda-tier"] == "advanced"
    assert schema["fast"]["x-xeda-tier"] == "expert"


def test_tier_of_reads_the_tier_of_a_leaf_and_none_for_a_leaf_without_one(toy_flow):
    assert tier_of(_Toy, "fast") == "expert"
    assert tier_of(_Toy, "sub") is None
    assert tier_of(_Toy, "no_such_field") is None
    assert tier_of(toy_flow.Settings, "toy") is None


def test_an_unknown_tier_is_refused():
    with pytest.raises(ValueError, match="bogus"):
        tier("bogus")  # type: ignore[arg-type]


def test_extras_keeps_every_key_of_every_marker():
    assert extras(tier("common"), WORKING) == {"x-xeda-tier": "common", **WORKING}
    assert extras() == {}


def test_two_markers_that_disagree_on_a_key_are_refused():
    with pytest.raises(ValueError, match="x-xeda-tier"):
        extras(tier("common"), tier("expert"))
    assert extras(tier("common"), tier("common")) == tier("common")


def test_a_tier_changes_no_validation(toy_flow):
    assert toy_flow.Settings.from_input({"toy": {"fast": True}}).toy.fast is True


# ------------------------------------------------------------------------ declaration checks


def _declaration_cases() -> dict[str, Any]:
    cases: dict[str, Any] = {}

    def declare(name):
        def register(build):
            cases[name] = build
            return build

        return register

    @declare("a flag naming no field")
    def _():
        class Bad(SettingsGroup):
            FLAGS: ClassVar[tuple[Flag, ...]] = (Flag("nothing", "-x", "value"),)
            a: int = 0

    @declare("a field twice in FLAGS")
    def _():
        class Bad(SettingsGroup):
            FLAGS: ClassVar[tuple[Flag, ...]] = (Flag("a", "-a", "value"), Flag("a", "-b", "value"))
            a: int = 0

    @declare("a flag twice in FLAGS")
    def _():
        class Bad(SettingsGroup):
            FLAGS: ClassVar[tuple[Flag, ...]] = (Flag("a", "-a", "value"), Flag("b", "-a", "value"))
            a: int = 0
            b: int = 0

    @declare("a flag of FLAGS in EXTRA_FLAGS")
    def _():
        class Bad(SettingsGroup):
            FLAGS: ClassVar[tuple[Flag, ...]] = (Flag("a", "-a", "value"),)
            EXTRA_FLAGS: ClassVar[frozenset[str]] = frozenset({"-a"})
            a: int = 0
            extra_args: list[str] = []

    @declare("a flag without a dash")
    def _():
        class Bad(SettingsGroup):
            FLAGS: ClassVar[tuple[Flag, ...]] = (Flag("a", "a", "value"),)
            a: int = 0

    @declare("an extra flag without a dash")
    def _():
        class Bad(SettingsGroup):
            EXTRA_FLAGS: ClassVar[frozenset[str]] = frozenset({"free"})
            extra_args: list[str] = []

    @declare("EXTRA_FLAGS without extra_args")
    def _():
        class Bad(SettingsGroup):
            EXTRA_FLAGS: ClassVar[frozenset[str]] = frozenset({"-free"})

    @declare("extra_args without EXTRA_FLAGS")
    def _():
        class Bad(SettingsGroup):
            extra_args: list[str] = []

    @declare("extra_args that is not a list of text")
    def _():
        class Bad(SettingsGroup):
            EXTRA_FLAGS: ClassVar[frozenset[str]] = frozenset({"-free"})
            extra_args: list[int] = []

    @declare("enabled that is not first")
    def _():
        class Bad(SettingsGroup):
            a: int = 0
            enabled: bool = True

    @declare("enabled that is not a bool")
    def _():
        class Bad(SettingsGroup):
            enabled: int = 1

    @declare("a switch on a field that is not a bool")
    def _():
        class Bad(SettingsGroup):
            FLAGS: ClassVar[tuple[Flag, ...]] = (Flag("a", "-a", "switch"),)
            a: int = 0

    @declare("a skip on a field that is not a bool")
    def _():
        class Bad(SettingsGroup):
            FLAGS: ClassVar[tuple[Flag, ...]] = (Flag("a", "-skip_a", "skip"),)
            a: str = ""

    @declare("a flag of an unknown kind")
    def _():
        class Bad(SettingsGroup):
            FLAGS: ClassVar[tuple[Flag, ...]] = (Flag("items", "-items", "word"),)  # type: ignore[arg-type]
            items: list[str] = []

    @declare("a words flag on a field that is not a list")
    def _():
        class Bad(SettingsGroup):
            FLAGS: ClassVar[tuple[Flag, ...]] = (Flag("text", "-text", "words"),)
            text: str = ""

    @declare("a list flag on a field that is not a list")
    def _():
        class Bad(SettingsGroup):
            FLAGS: ClassVar[tuple[Flag, ...]] = (Flag("n", "-n", "list"),)
            n: Optional[int] = None

    @declare("a quantity in FLAGS")
    def _():
        class Bad(SettingsGroup):
            FLAGS: ClassVar[tuple[Flag, ...]] = (Flag("period", "-period", "value"),)
            period: Optional[Time] = "10 ns"  # type: ignore[assignment]

    @declare("a removed setting that is a field")
    def _():
        class Bad(SettingsGroup):
            removed_settings: ClassVar[dict[str, str]] = {"a": "`b`"}
            a: int = 0

    @declare("a group that does not build without arguments")
    def _():
        class Bad(SettingsGroup):
            a: int

    return cases


DECLARATION_CASES = _declaration_cases()


@pytest.mark.parametrize("name", sorted(DECLARATION_CASES))
def test_a_bad_declaration_is_a_type_error_when_the_class_is_created(name):
    with pytest.raises(TypeError, match="Bad"):
        DECLARATION_CASES[name]()


@pytest.mark.parametrize(
    "annotation",
    [
        Time,
        Optional[Time],
        Annotated[Time, Bounds(gt="0 ns")],
        Optional[Annotated[Time, Bounds(ge="0 ns")]],
        Union[Literal["auto"], Annotated[Time, Bounds(ge="0 ns")], None],
        Optional[Capacitance],
    ],
)
def test_a_quantity_in_flags_is_refused_with_what_to_write_instead(annotation):
    """`render_flags` formats a value with `quote()`, so a quantity would reach the tool as
    `10 ns`. The template renders it in the unit that the tool takes."""
    with pytest.raises(TypeError) as raised:

        class Bad(SettingsGroup):
            FLAGS: ClassVar[tuple[Flag, ...]] = (Flag("period", "-period", "value"),)
            period: annotation = "10 ns"  # type: ignore[valid-type]

    message = str(raised.value)
    assert "`period`" in message
    assert "| to(unit)" in message


def test_other_fields_may_stay_in_flags():
    class Fine(SettingsGroup):
        FLAGS: ClassVar[tuple[Flag, ...]] = (
            Flag("name", "-name", "value"),
            Flag("n", "-n", "value"),
            Flag("mode", "-mode", "value"),
        )
        name: Optional[str] = None
        n: Annotated[int, Field(ge=0)] = 0
        mode: Union[Literal["auto"], int, None] = None
        period: Optional[Time] = None  # a quantity is fine outside FLAGS

    assert Fine().period is None


def test_optional_booleans_may_carry_a_switch_and_a_skip():
    class Fine(SettingsGroup):
        FLAGS: ClassVar[tuple[Flag, ...]] = (
            Flag("a", "-a", "switch"),
            Flag("b", "-skip_b", "skip"),
        )
        a: Optional[bool] = None
        b: Optional[bool] = None

    assert render_flags(Fine(), tcl_word) == ""
    assert render_flags(Fine(a=True, b=False), tcl_word) == "-a -skip_b"


def test_a_subclass_inherits_the_flag_table_and_may_add_fields():
    class Wider(_Toy):
        more: int = 0

    assert Wider.FLAGS is _Toy.FLAGS
    assert render_flags(Wider(), tcl_word) == '-sizing "a"'


def test_the_module_imports_nothing_from_the_product_flows():
    source = (Path(__file__).parent.parent / "src/xeda/flow/settings_group.py").read_text()
    assert "xeda.flows" not in source and "from ..flows" not in source


# ----------------------------------------------------------------------- the model invariants

LEAVES = [
    "toy.sizing",
    "toy.value",
    "toy.items",
    "toy.words",
    "toy.fast",
    "toy.swap",
    "toy.extra_args",
    "toy.path",
    "toy.sub.enabled",
    "toy.sub.knob",
    "toy.sub",
    "toy",
]


def _walk_set(settings, dotted: str, value):
    *parents, leaf = dotted.split(".")
    target = settings
    for name in parents:
        target = getattr(target, name)
    setattr(target, leaf, value)


def _outcome(build):
    try:
        return "accepted", build().model_dump()
    except (ValidationError, FlowSettingsError):
        return "rejected", None


@pytest.mark.parametrize("leaf", LEAVES)
@pytest.mark.parametrize("probe", range(len(PROBES)))
def test_a_probe_given_to_a_group_leaf_is_accepted_or_refused_never_a_traceback(
    toy_flow, leaf, probe
):
    value = PROBES[probe]
    given: dict[str, Any] = {}
    node = given
    *parents, last = leaf.split(".")
    for name in parents:
        node = node.setdefault(name, {})
    node[last] = copy.deepcopy(value)
    first = _outcome(lambda: toy_flow.Settings.from_input(copy.deepcopy(given)))
    second = _outcome(lambda: toy_flow.Settings.from_input(copy.deepcopy(given)))
    assert first == second

    def assign():
        settings = toy_flow.Settings()
        _walk_set(settings, leaf, copy.deepcopy(value))
        return settings

    assert _outcome(assign)[0] in ("accepted", "rejected")


def test_assignment_to_a_deep_leaf_validates_it(toy_flow):
    settings = toy_flow.Settings()
    settings.toy.sub.knob = 5
    assert settings.toy.sub.knob == 5
    with pytest.raises(ValidationError):
        settings.toy.sub.knob = "five"
    with pytest.raises(ValidationError):
        settings.toy.value = 2
    assert settings.toy.sub.knob == 5


def test_a_settings_json_round_trip_gives_equal_settings(toy_flow, tmp_path):
    settings = _settings(
        toy_flow, ["toy.sub.knob=3", "toy.items=a,b", "toy.sizing=b", "toy.extra_args=-free,1"]
    )
    dump_json(settings.model_dump(), tmp_path / "settings.json")
    reloaded = toy_flow.Settings.from_input(json.loads((tmp_path / "settings.json").read_text()))
    assert reloaded == settings
    assert reloaded.model_dump() == settings.model_dump()


def test_the_validators_run_again_change_nothing(toy_flow):
    settings = _settings(toy_flow, ["toy.items=a,b", "toy.sub.knob=3"])
    for name in ("sizing", "items", "extra_args", "path"):
        setattr(settings.toy, name, getattr(settings.toy, name))
    settings.toy.sub = settings.toy.sub
    settings.toy = settings.toy
    again = toy_flow.Settings.from_input(settings.model_dump())
    assert again == settings


def test_the_run_hash_changes_with_one_leaf_and_not_without_a_change(toy_flow):
    base = _settings(toy_flow, ["toy.sub.knob=3"])
    same = _settings(toy_flow, ["toy.sub.knob=3"])
    other = _settings(toy_flow, ["toy.sub.knob=4"])
    assert flowrun_hash(toy_flow.name, base) == flowrun_hash(toy_flow.name, same)
    assert flowrun_hash(toy_flow.name, base) != flowrun_hash(toy_flow.name, other)


def test_the_settings_of_a_group_are_independent_copies(toy_flow):
    first, second = toy_flow.Settings(), toy_flow.Settings()
    first.toy.items.append("x")
    assert second.toy.items == []


def test_a_hint_for_another_case_wins_over_the_hint_for_a_producer():
    from xeda.flows import Nextpnr, YosysFpga
    from xeda.flow_runner.settings_layers import suggest_dependency_node

    producer_only = next(
        name
        for name in YosysFpga.Settings.model_fields
        if name not in Nextpnr.Settings.model_fields
    )
    plain = FlowSettingsError(
        [(producer_only, "Extra inputs are not permitted", "", "extra_forbidden")], Nextpnr.Settings
    )
    suggest_dependency_node(Nextpnr, plain)
    assert "flows.yosys_fpga" in plain.errors[0][1]

    hinted_message = "Extra inputs are not permitted; did you mean `other`?"
    hinted = FlowSettingsError(
        [(producer_only, hinted_message, "", "extra_forbidden")], Nextpnr.Settings
    )
    suggest_dependency_node(Nextpnr, hinted)
    assert hinted.errors[0][1] == hinted_message


def test_a_flag_kind_added_without_a_renderer_fails_loudly(monkeypatch):
    later = Flag("value", "-value", "later")  # type: ignore[arg-type]
    monkeypatch.setattr(_Toy, "FLAGS", (later,))
    with pytest.raises(ValueError, match="unknown kind 'later'"):
        render_flags(_Toy(value=0.5), tcl_word)


def test_list_and_optional_list_fields_may_carry_list_and_words_flags():
    class Fine(SettingsGroup):
        FLAGS: ClassVar[tuple[Flag, ...]] = (
            Flag("a", "-a", "list"),
            Flag("b", "-b", "words"),
        )
        a: Optional[list[str]] = None
        b: list[str] | None = None

    assert render_flags(Fine(a=["x"], b=["y"]), tcl_word) == '-a [list "x"] -b "y"'
