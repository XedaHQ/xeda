"""The listing of settings groups: `settings_info`, `xeda list-settings` and the agent reference.

A flow lists the leaves of a settings group under their dotted names, each with its tier. These
tests use a toy flow, because no product flow has to carry a group for the listing to hold.
"""

import json
from typing import Any, ClassVar, Literal, Optional

import pytest
from click.testing import CliRunner

from xeda.flow import MetricSpec
from xeda.agent_skill import _flow_section
from xeda.cli import cli
from xeda.dataclass import Field, extras, tier, WORKING
from xeda.flow import Flow
from xeda.flow.settings_group import Flag, SettingsGroup
from xeda.introspect import all_flow_classes, flow_info, settings_info

from .flow_registry import preserved_flow_registry

pytestmark = pytest.mark.python_compat


class _Inner(SettingsGroup):
    """A switchable group inside the outer group."""

    FLAGS: ClassVar[tuple[Flag, ...]] = (
        Flag("enabled", "-skip_inner", "skip"),
        Flag("knob", "-knob", "value"),
    )

    enabled: bool = Field(True, description="Run the inner step.", json_schema_extra=tier("common"))
    knob: Optional[int] = Field(
        None, description="A number for the inner step.", json_schema_extra=tier("expert")
    )


class _Outer(SettingsGroup):
    """A group with leaves of each tier and a sub-group."""

    FLAGS: ClassVar[tuple[Flag, ...]] = (
        Flag("mode", "-mode", "value"),
        Flag("ratio", "-ratio", "value"),
        Flag("log", "-log", "value"),
    )
    EXTRA_FLAGS: ClassVar[frozenset[str]] = frozenset({"-free"})

    mode: Literal["a", "b"] = Field(
        "a", description="Which mode to use.", json_schema_extra=tier("common")
    )
    ratio: Optional[float] = Field(None, description="A ratio.", json_schema_extra=tier("advanced"))
    log: Optional[str] = Field(
        None,
        description="The name of a log file.",
        json_schema_extra=extras(tier("advanced"), WORKING),
    )
    inner: _Inner = Field(default_factory=_Inner, description="The inner step.")
    extra_args: list[str] = Field(
        [], description="More flags of the command.", json_schema_extra=tier("expert")
    )


@pytest.fixture
def listing_flow():
    """A flow whose settings hold a group. It registers itself, so the registry is restored."""
    with preserved_flow_registry():

        class ListingToy(Flow):
            """A flow that exists to carry a group."""

            results_description: ClassVar[dict[str, MetricSpec]] = {}

            class Settings(Flow.Settings):
                before: int = Field(1, description="A setting before the group.")
                outer: _Outer = Field(default_factory=_Outer, description="The outer command.")
                after: int = Field(2, description="A setting after the group.")

            def run(self) -> None:
                pass

        yield ListingToy


OWN = ["before", "outer", "after"]


def _own_entries(flow) -> list[dict[str, Any]]:
    """The entries of the toy settings, without the settings that every flow has."""
    return [f for f in settings_info(flow)["fields"] if not f["common"]]


def _names(document: dict[str, Any]) -> list[str]:
    return [f["name"] for f in document["fields"] if not f["common"]]


def _list(*args: str) -> dict[str, Any]:
    result = CliRunner().invoke(cli, ["list-settings", "listing_toy", *args, "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


# ----------------------------------------------------------------------------- the entries


def test_a_group_lists_its_fields_after_its_own_entry_depth_first(listing_flow):
    assert [f["name"] for f in _own_entries(listing_flow)] == [
        "before",
        "outer",
        "outer.mode",
        "outer.ratio",
        "outer.log",
        "outer.inner",
        "outer.inner.enabled",
        "outer.inner.knob",
        "outer.extra_args",
        "after",
    ]


def test_a_leaf_entry_describes_the_leaf(listing_flow):
    by_name = {f["name"]: f for f in _own_entries(listing_flow)}
    mode = by_name["outer.mode"]
    assert mode == {
        "name": "outer.mode",
        "alias": None,
        "type": "string",
        "required": False,
        "default": "a",
        "description": "Which mode to use.",
        "enum": ["a", "b"],
        "common": False,
        "declared_by": f"{_Outer.__module__}.{_Outer.__qualname__}",
        "writes": None,
        "opaque_keys": False,
        "json_schema": mode["json_schema"],
        "tier": "common",
        "group": "outer",
    }
    assert mode["json_schema"]["default"] == "a"
    assert mode["json_schema"]["description"] == "Which mode to use."
    assert by_name["outer.ratio"]["tier"] == "advanced"
    assert by_name["outer.ratio"]["default"] is None
    assert by_name["outer.inner.knob"]["tier"] == "expert"
    assert by_name["outer.inner.knob"]["type"] == "integer"
    assert by_name["outer.extra_args"]["type"] == "array[string]"
    assert by_name["outer.extra_args"]["default"] == []


def test_a_leaf_entry_reports_what_the_flow_writes(listing_flow):
    by_name = {f["name"]: f for f in _own_entries(listing_flow)}
    assert by_name["outer.log"]["writes"] == "working"
    assert by_name["outer.mode"]["writes"] is None


def test_every_entry_of_a_group_names_the_top_level_group(listing_flow):
    by_name = {f["name"]: f for f in _own_entries(listing_flow)}
    for name in ("outer.mode", "outer.inner", "outer.inner.enabled", "outer.extra_args"):
        assert by_name[name]["group"] == "outer"
    assert by_name["outer.inner"]["type"] == "_Inner"
    assert by_name["outer.inner"]["description"] == "The inner step."
    assert by_name["outer.inner"]["default"] == _Inner().model_dump()


def test_a_setting_outside_a_group_has_no_group_and_a_setting_without_a_tier_is_common(
    listing_flow,
):
    by_name = {f["name"]: f for f in _own_entries(listing_flow)}
    for name in ("before", "outer", "after"):
        assert by_name[name]["group"] is None
        assert by_name[name]["tier"] == "common"


@pytest.mark.parametrize("flow", ["vivado_synth", "ghdl_sim", "nextpnr"])
def test_every_entry_of_a_real_flow_has_a_tier_and_a_group(flow):
    fields = settings_info(flow)["fields"]
    assert fields
    for field in fields:
        assert field["tier"] == "common", field["name"]
        assert field["group"] is None, field["name"]


def test_the_entries_of_yosys_name_the_group_of_each_dotted_name():
    """`yosys` is the one product flow with groups: a leaf of a group has a dotted name and names
    its group, and a setting outside the groups, or the field of a group, has neither."""
    fields = settings_info("yosys")["fields"]
    assert any(field["group"] is not None for field in fields)
    for field in fields:
        assert field["tier"] in ("common", "advanced", "expert"), field["name"]
        assert (field["group"] is not None) == ("." in field["name"]), field["name"]
        if field["group"] is not None:
            assert field["name"].startswith(field["group"] + "."), field["name"]


def test_no_flow_of_the_product_lists_a_dotted_name_yet():
    assert not [
        f["name"]
        for cls in all_flow_classes()
        for f in settings_info(cls)["fields"]
        if "." in f["name"] and f["group"] is None
    ]


# ------------------------------------------------------------------------ xeda list-settings


def test_list_settings_lists_the_leaves_by_dotted_name_with_tier_and_group(listing_flow):
    document = _list()
    leaves = {f["name"]: f for f in document["fields"] if f["group"] == "outer"}
    assert set(leaves) == {
        "outer.mode",
        "outer.ratio",
        "outer.log",
        "outer.inner",
        "outer.inner.enabled",
        "outer.inner.knob",
        "outer.extra_args",
    }
    assert leaves["outer.inner.knob"]["tier"] == "expert"
    assert all(f["tier"] in ("common", "advanced", "expert") for f in document["fields"])


def test_tier_advanced_hides_the_expert_entries_in_json(listing_flow):
    names = _names(_list("--tier", "advanced"))
    assert "outer.ratio" in names and "outer.mode" in names
    assert "outer.inner.knob" not in names and "outer.extra_args" not in names
    # a setting that has no tier is common, and a sub-group entry has none
    assert {"before", "outer", "outer.inner", "after"} <= set(names)


def test_tier_common_shows_only_the_common_entries(listing_flow):
    document = _list("--tier", "common")
    assert {f["tier"] for f in document["fields"]} == {"common"}
    assert "outer.mode" in _names(document) and "outer.ratio" not in _names(document)


def test_tier_expert_shows_everything(listing_flow):
    assert _names(_list("--tier", "expert")) == _names(_list())


def test_tier_filters_the_records_of_jsonl_as_well(listing_flow):
    result = CliRunner().invoke(
        cli, ["list-settings", "listing_toy", "--format", "jsonl", "--tier", "advanced"]
    )
    assert result.exit_code == 0, result.output
    names = [json.loads(line)["name"] for line in result.stdout.splitlines()]
    assert "outer.ratio" in names and "outer.inner.knob" not in names


def test_tier_keeps_the_settings_that_every_flow_accepts(listing_flow):
    names = [f["name"] for f in _list("--tier", "common")["fields"]]
    assert "nthreads" in names
    assert "nthreads" not in [f["name"] for f in _list("--tier", "common", "--no-common")["fields"]]


def test_an_unknown_tier_is_a_usage_error(listing_flow):
    result = CliRunner().invoke(cli, ["list-settings", "listing_toy", "--tier", "basic"])
    assert result.exit_code == 2
    assert "basic" in result.output and "common" in result.output


def _titles(output: str) -> list[str]:
    return [
        line.strip()
        for line in output.splitlines()
        if "settings" in line and "│" not in line and "┃" not in line
    ]


def _table_rows(output: str) -> list[str]:
    """The first cell of each row of the tables, in order, without the folded name parts."""
    return [
        line.split("│")[1].strip()
        for line in output.splitlines()
        if line.startswith("│") and line.split("│")[1].strip()
    ]


def test_the_table_prints_a_table_per_tier_after_the_common_one(listing_flow, monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")
    result = CliRunner().invoke(cli, ["list-settings", "listing_toy"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    titles = _titles(result.output)
    assert titles[:3] == [
        "listing_toy settings",
        "listing_toy settings: advanced",
        "listing_toy settings: expert",
    ]
    assert titles[3].startswith("Common settings")
    rows = _table_rows(result.output)
    assert rows.index("outer.mode") < rows.index("outer.ratio") < rows.index("outer.inner.knob")
    assert rows.index("before") < rows.index("outer.mode") < rows.index("after")
    assert rows.index("outer.inner.knob") < rows.index("nthreads")


def test_the_table_leaves_out_a_tier_with_no_entries(listing_flow, monkeypatch):
    result = CliRunner().invoke(
        cli, ["list-settings", "listing_toy", "--tier", "advanced"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.output
    titles = _titles(result.output)
    assert "listing_toy settings: advanced" in titles
    assert "listing_toy settings: expert" not in titles
    assert "outer.inner.knob" not in _table_rows(result.output)
    only_common = CliRunner().invoke(
        cli, ["list-settings", "listing_toy", "--tier", "common"], env={"COLUMNS": "200"}
    )
    assert _titles(only_common.output)[0] == "listing_toy settings"
    assert not [t for t in _titles(only_common.output) if t.endswith(("advanced", "expert"))]


def test_the_table_of_a_flow_without_groups_has_one_table_of_its_own_settings():
    result = CliRunner().invoke(cli, ["list-settings", "ghdl_sim"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    titles = _titles(result.output)
    assert titles[0] == "ghdl_sim settings"
    assert not [t for t in titles if t.endswith(("advanced", "expert"))]


# ------------------------------------------------------------------------- the agent reference


def test_the_agent_reference_counts_top_level_settings_and_summarizes_each_group(listing_flow):
    section = _flow_section(flow_info(listing_flow))
    own = len([f for f in _own_entries(listing_flow) if f["group"] is None])
    assert f"{own} flow-specific settings (plus the common ones)" in section
    assert own == len(OWN)
    line = next(line for line in section.splitlines() if line.startswith("Group `outer`"))
    assert "`outer.mode`" in line
    assert "`outer.inner.enabled`" in line
    assert "outer.ratio" not in line and "outer.inner.knob" not in line
    # advanced and expert leaves that the common tier leaves out, sub-group entries excluded:
    # ratio, log, knob and extra_args
    assert "4 more" in line
    assert "xeda list-settings listing_toy --tier expert --json" in line


def test_the_agent_reference_has_no_group_line_for_a_flow_without_groups():
    assert "Group `" not in _flow_section(flow_info("ghdl_sim"))


# ------------------------------------------------------------------------------ the option


def test_the_tier_option_offers_the_tiers_of_a_setting():
    from xeda.cli_types import TIER_NAMES
    from xeda.dataclass import TIERS

    assert TIER_NAMES == TIERS == ("common", "advanced", "expert")
    command = cli.commands["list-settings"]
    option = next(p for p in command.params if p.name == "tier")
    assert option.opts == ["--tier"] and not option.is_flag
    assert option.default is None


# ------------------------------------------------------------- aliases and shared groups


class _Aliased(SettingsGroup):
    """A group with a field that has a second name."""

    knob: Optional[int] = Field(
        None, alias="knob_alias", description="A number.", json_schema_extra=tier("common")
    )
    plain: Optional[int] = Field(None, description="No second name.")


@pytest.fixture
def aliased_flow():
    with preserved_flow_registry():

        class AliasedToy(Flow):
            """A flow whose group has a field with an alias."""

            results_description: ClassVar[dict[str, MetricSpec]] = {}

            class Settings(Flow.Settings):
                aliased: _Aliased = Field(default_factory=_Aliased, description="The group.")

            def run(self) -> None:
                pass

        yield AliasedToy


def test_a_field_of_a_group_reports_its_alias_as_a_dotted_path(aliased_flow):
    by_name = {f["name"]: f for f in settings_info(aliased_flow)["fields"]}
    assert by_name["aliased.knob"]["alias"] == "aliased.knob_alias"
    assert by_name["aliased.plain"]["alias"] is None
    assert by_name["aliased"]["alias"] is None


def test_the_dotted_alias_of_a_group_field_is_accepted_as_a_setting(aliased_flow):
    from xeda.flow_runner.settings_layers import compose_flow_settings

    settings = aliased_flow.Settings.from_input(
        compose_flow_settings(aliased_flow, [], ["aliased.knob_alias=3"])
    )
    assert settings.aliased.knob == 3
    by_name = {f["name"]: f for f in settings_info(aliased_flow)["fields"]}
    assert by_name["aliased.knob"]["alias"] == "aliased.knob_alias"


@pytest.fixture
def shared_group_flow(monkeypatch):
    """A flow with a group that every flow accepts: the group is one of the fields of
    `Flow.Settings` for the length of the test (the real model is restored afterwards)."""
    with preserved_flow_registry():

        class SharedGroupToy(Flow):
            """A flow with a group that every flow accepts, and one of its own."""

            results_description: ClassVar[dict[str, MetricSpec]] = {}

            class Settings(Flow.Settings):
                shared: _Outer = Field(default_factory=_Outer, description="Shared group.")
                own: _Inner = Field(default_factory=_Inner, description="Own group.")

            def run(self) -> None:
                pass

        monkeypatch.setitem(
            Flow.Settings.model_fields, "shared", SharedGroupToy.Settings.model_fields["shared"]
        )
        yield SharedGroupToy


def test_the_entries_of_a_group_that_every_flow_accepts_are_common(shared_group_flow):
    by_name = {f["name"]: f for f in settings_info(shared_group_flow)["fields"]}
    shared = [f for name, f in by_name.items() if name.split(".")[0] == "shared"]
    assert len(shared) == 8 and all(f["common"] for f in shared)
    own = [f for name, f in by_name.items() if name.split(".")[0] == "own"]
    assert len(own) == 3 and not any(f["common"] for f in own)


def test_no_common_hides_a_group_that_every_flow_accepts(shared_group_flow):
    result = CliRunner().invoke(cli, ["list-settings", "shared_group_toy", "--json", "--no-common"])
    assert result.exit_code == 0, result.output
    names = [f["name"] for f in json.loads(result.stdout)["fields"]]
    assert "own.enabled" in names
    assert not [n for n in names if n.split(".")[0] == "shared"]


def test_the_agent_reference_does_not_count_a_group_that_every_flow_accepts(shared_group_flow):
    section = _flow_section(flow_info(shared_group_flow))
    assert "1 flow-specific settings" in section
    assert "Group `own`" in section
    assert "Group `shared`" not in section
