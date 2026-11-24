"""The `TypedDict` classes of the public API describe the mappings that Xeda really returns.

A type checker cannot see through the code that fills these mappings, so each test validates what a
real call returns against its class: the exact keys (a missing, an extra or an optional key) and
the type of every value. The checks for a plan and a record are in the tests of those objects.
"""

from __future__ import annotations

import typing_extensions

import pytest
from pydantic import ValidationError

import xeda
import xeda.introspect as introspect
from xeda._internal.flows import __builtin_flows__

from .typed_shapes import check_shape

pytestmark = pytest.mark.python_compat

FLOWS = sorted(flow.name for flow in __builtin_flows__)


def test_a_flow_listing_has_the_keys_of_its_class() -> None:
    listing = introspect.flows_info()
    assert len(listing) >= len(FLOWS)
    for info in listing:
        check_shape(introspect.FlowInfo, info)


@pytest.mark.parametrize("flow", FLOWS)
def test_the_listings_of_a_flow_have_the_keys_of_their_classes(flow: str) -> None:
    check_shape(introspect.FlowInfo, introspect.flow_info(flow))
    check_shape(introspect.SettingsInfo, introspect.settings_info(flow))
    check_shape(introspect.ResultsInfo, introspect.results_info(flow))


def test_a_flow_has_the_key_experimental_only_when_it_is_experimental() -> None:
    listing = introspect.flows_info()
    assert any("experimental" in info for info in listing)
    assert all(info.get("experimental") is True for info in listing if "experimental" in info)


def test_the_relations_of_a_flow_have_the_keys_of_their_classes() -> None:
    relations = [
        relation
        for info in introspect.flows_info()
        for relation in (*info["can_follow"], *info["can_precede"])
    ]
    assert any(relation["binds"] for relation in relations)
    for relation in relations:
        check_shape(introspect.FlowRelation, relation)
        for binding in relation["binds"]:
            check_shape(introspect.FlowBinding, binding)


def test_a_setting_has_the_unit_key_unless_only_a_design_file_accepts_it() -> None:
    entries = [entry for flow in FLOWS for entry in introspect.settings_info(flow)["fields"]]
    for entry in entries:
        check_shape(introspect.SettingInfo, entry)
    assert any("experimental" in entry for entry in entries)
    # A setting that only the input form of a design file accepts has no `unit` key.
    assert any("unit" not in entry for entry in entries)
    assert any(entry["group"] is not None for entry in entries)


def test_the_bundled_boards_and_platforms_have_the_keys_of_their_classes() -> None:
    boards = introspect.boards_info()
    assert boards
    for board in boards:
        check_shape(introspect.BoardInfo, board)
    platforms = introspect.platforms_info()
    assert platforms
    for platform in platforms:
        check_shape(introspect.PlatformInfo, platform)


def test_a_shape_check_refuses_a_missing_an_extra_and_a_mistyped_key() -> None:
    options = {
        "rebuild_all": False,
        "clean": False,
        "prune_variants": False,
        "outputs_to": None,
        "overwrite_outputs": False,
        "post_cleanup": False,
        "post_cleanup_purge": False,
    }
    check_shape(xeda.PlanOptions, options)
    for broken in (
        {key: value for key, value in options.items() if key != "clean"},
        {**options, "extra": 1},
        {**options, "clean": "no"},
    ):
        with pytest.raises(ValidationError):
            check_shape(xeda.PlanOptions, broken)


def test_every_public_class_of_introspect_is_a_typed_dict() -> None:
    classes = [
        getattr(introspect, name)
        for name in introspect.__all__
        if isinstance(getattr(introspect, name), type)
    ]
    assert classes
    assert all(typing_extensions.is_typeddict(cls) for cls in classes)
