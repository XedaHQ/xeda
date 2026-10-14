"""The ASIC platform model: its residue file, the layer merge and the redundancy lint.

A platform is filled in four layers, weakest first: derived (from the PDK files), upstream (from a
configuration file of the PDK), residue (the small file xeda ships) and user (a residue file of
the user). A later layer wins at each leaf, and `sources` says which layer gave each leaf. The
residue stays minimal because a lint reports a line that a lower layer already gives.
"""

import ast
import json
from fnmatch import fnmatchcase
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
import tomllib
import yaml

from xeda.platforms import (
    Located,
    PlatformModel,
    Residue,
    build_platform,
    bundled_platform_names,
    load_residue,
)
from xeda.platforms.build import residue_problems
from xeda.platforms.model import Number, Track, leaf_annotation, nest_leaves, normalize_leaf
from xeda.platforms.residue import ResidueError, effective_residue
from xeda.yaml_loader import load_yaml

REPO = Path(__file__).parent.parent
PLATFORMS = REPO / "src" / "xeda" / "platforms"


def residue_dict(**changes: Any) -> dict[str, Any]:
    """A small complete residue as a table; `changes` replaces whole top-level keys."""
    table: dict[str, Any] = {
        "name": "tiny",
        "providers": {"default": "directory", "directory": {"layout": "orfs"}},
        "corners": {"default": "typical", "aliases": {"tt": "typical"}},
        "files": {
            "liberty": {"typical": ["lib/typical.lib"], "fast": ["lib/fast.lib"]},
            "tech_lef": "lef/tech.lef",
            "cell_lef": ["lef/cells.lef"],
        },
        "values": {},
        "overrides": {},
    }
    table.update(changes)
    return table


def make_residue(**changes: Any) -> Residue:
    return Residue.model_validate(residue_dict(**changes))


def located(tmp_path: Path, **changes: Any) -> Located:
    directory = tmp_path / "tiny"
    fields: dict[str, Any] = {
        "provider": "directory",
        "version": "unpinned",
        "root": tmp_path,
        "directory": directory,
        "corner": "typical",
        "files": {
            "liberty": [directory / "lib/typical.lib"],
            "tech_lef": [directory / "lef/tech.lef"],
            "cell_lef": [directory / "lef/cells.lef"],
        },
        "upstream_files": [],
    }
    fields.update(changes)
    return Located(**fields)


TRACK_FINE = {"x": [0.095, 0.19], "y": [0.07, 0.14]}
TRACK_COARSE = {"x": [0.095, 0.28], "y": [0.07, 0.28]}


# ---------------------------------------------------------------------------------------------
# The bundled residue file and its schema
# ---------------------------------------------------------------------------------------------


def test_the_only_bundled_platform_is_nangate45() -> None:
    assert bundled_platform_names() == ["nangate45"]


@pytest.mark.parametrize("name", bundled_platform_names())
def test_a_bundled_residue_loads_under_its_directory_name(name: str) -> None:
    residue = load_residue(name)
    assert residue.name == name
    assert residue.extends is None
    assert residue.resolve_corner(None) == "typical"
    assert (PLATFORMS / name / "platform.yaml").is_file()


def test_the_bundled_nangate45_residue_is_a_small_file() -> None:
    size = (PLATFORMS / "nangate45" / "platform.yaml").stat().st_size
    assert size <= 2048, f"platform.yaml of nangate45 is {size} bytes"


def test_a_bundled_name_that_is_unknown_lists_the_known_ones() -> None:
    with pytest.raises(ResidueError, match="nangate45") as error:
        load_residue("nangate44")
    assert "nangate44" in str(error.value)


def test_a_residue_path_loads_the_file_and_an_error_names_the_file(tmp_path: Path) -> None:
    path = tmp_path / "mine.yaml"
    path.write_text(yaml.safe_dump(residue_dict(bogus=1)))
    with pytest.raises(ResidueError, match="mine.yaml") as error:
        load_residue(path)
    assert "bogus" in str(error.value)


def test_a_residue_file_that_is_not_a_table_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "list.yaml"
    path.write_text("- a\n- b\n")
    with pytest.raises(ResidueError, match="list.yaml"):
        load_residue(path)


def test_a_missing_residue_file_is_refused_naming_it(tmp_path: Path) -> None:
    with pytest.raises(ResidueError, match="absent.yaml"):
        load_residue(tmp_path / "absent.yaml")


def bundled_table(name: str = "nangate45") -> dict[str, Any]:
    text = resources.files("xeda.platforms").joinpath(name, "platform.yaml").read_text()
    table = yaml.safe_load(text)
    assert isinstance(table, dict)
    return table


def test_an_unknown_key_at_the_top_is_refused_naming_it(tmp_path: Path) -> None:
    table = bundled_table()
    table["colour"] = "red"
    path = tmp_path / "platform.yaml"
    path.write_text(yaml.safe_dump(table))
    with pytest.raises(ResidueError, match="colour"):
        load_residue(path)


def test_an_unknown_key_inside_values_is_refused_naming_it(tmp_path: Path) -> None:
    table = bundled_table()
    table["values"]["routing"] = {"min_layre": "metal2"}
    path = tmp_path / "platform.yaml"
    path.write_text(yaml.safe_dump(table))
    with pytest.raises(ResidueError, match="min_layre"):
        load_residue(path)


def test_an_unknown_key_inside_files_is_refused_naming_it(tmp_path: Path) -> None:
    table = bundled_table()
    table["files"]["spice"] = "x.sp"
    path = tmp_path / "platform.yaml"
    path.write_text(yaml.safe_dump(table))
    with pytest.raises(ResidueError, match="spice"):
        load_residue(path)


def test_values_never_hold_the_name_the_corner_the_files_or_the_sources() -> None:
    for key in ("name", "corner", "files", "sources"):
        with pytest.raises(ValueError, match=key):
            Residue.model_validate(residue_dict(values={key: "x"}))


@pytest.mark.parametrize(
    "files",
    [
        {"tech_lef": ["a.lef"]},  # one file is text, not a list
        {"cell_lef": "a.lef"},  # a set of files is a list, not text
        {"gds": "a.gds"},
        {"liberty": {"typical": "a.lib"}},
        {"tech_lef": "/etc/tech.lef"},  # relative to the platform directory
        {"tech_lef": "../tech.lef"},
        {"tech_lef": "lef\\tech.lef"},  # POSIX text
    ],
)
def test_a_file_role_takes_exactly_one_form_of_relative_posix_text(files: dict) -> None:
    base = residue_dict()["files"]
    with pytest.raises(ValueError):
        Residue.model_validate(residue_dict(files={**base, **files}))


def test_a_provider_without_an_entry_cannot_be_the_default() -> None:
    providers = {"default": "orfs", "directory": {"layout": "orfs"}}
    with pytest.raises(ValueError, match="orfs"):
        Residue.model_validate(residue_dict(providers=providers))


def test_an_orfs_pin_is_a_full_commit_hash() -> None:
    providers = {
        "default": "orfs",
        "orfs": {"version": "6b305a7", "directory": "flow/platforms/x", "download_mib": 1.0},
    }
    with pytest.raises(ValueError, match="version"):
        Residue.model_validate(residue_dict(providers=providers))


def test_the_open_pdks_layout_needs_a_variant_and_a_library() -> None:
    providers = {"default": "directory", "directory": {"layout": "open_pdks"}}
    with pytest.raises(ValueError, match="variant"):
        Residue.model_validate(residue_dict(providers=providers))


def test_an_alias_must_name_a_corner_of_the_liberty_files() -> None:
    corners = {"default": "typical", "aliases": {"slow": "ss"}}
    with pytest.raises(ValueError, match="ss"):
        Residue.model_validate(residue_dict(corners=corners))


def test_a_table_key_with_a_dot_is_refused() -> None:
    values = {"tracks": {"metal1.x": TRACK_FINE}}
    with pytest.raises(ValueError, match=r"metal1\.x"):
        Residue.model_validate(residue_dict(values=values))
    with pytest.raises(ValueError, match=r"metal1\.x"):
        PlatformModel.model_validate({"name": "t", "tracks": {"metal1.x": TRACK_FINE}})
    with pytest.raises(ValueError, match=r"a\.b"):
        Residue.model_validate(
            residue_dict(overrides={"tracks.a.b": {"value": TRACK_FINE, "reason": "r"}})
        )


def test_a_number_is_a_number_and_not_text() -> None:
    for values in ({"place_density": "0.3"}, {"tracks": {"metal1": {"x": ["0.1", 0.2]}}}):
        with pytest.raises(ValueError):
            Residue.model_validate(residue_dict(values=values))


def test_a_value_that_is_null_gives_nothing_and_is_refused() -> None:
    with pytest.raises(ValueError, match="place_density"):
        Residue.model_validate(residue_dict(values={"place_density": None}))
    overrides = {"place_density": {"value": None, "reason": "r"}}
    with pytest.raises(ValueError, match="place_density"):
        Residue.model_validate(residue_dict(overrides=overrides))


# ---------------------------------------------------------------------------------------------
# Overrides
# ---------------------------------------------------------------------------------------------


def test_an_override_needs_a_reason() -> None:
    for override in ({"value": 0.5}, {"value": 0.5, "reason": ""}, {"value": 0.5, "reason": "  "}):
        with pytest.raises(ValueError, match="reason"):
            Residue.model_validate(residue_dict(overrides={"place_density": override}))


def test_an_override_value_of_the_wrong_type_is_refused_naming_the_leaf() -> None:
    overrides = {"place_density": {"value": "dense", "reason": "r"}}
    with pytest.raises(ValueError, match="place_density"):
        Residue.model_validate(residue_dict(overrides=overrides))
    overrides = {"tracks.metal1": {"value": {"x": [1.0]}, "reason": "r"}}
    with pytest.raises(ValueError, match=r"tracks\.metal1"):
        Residue.model_validate(residue_dict(overrides=overrides))


@pytest.mark.parametrize("key", ["match_cell_footprint", "diode_usable"])
@pytest.mark.parametrize("value", [1, 0, "true", "1", "yes", 1.0, [True]])
def test_a_boolean_override_takes_true_and_false_only(key: str, value: Any) -> None:
    """The model refuses `1` for a boolean in `values`; an override must refuse it too."""
    overrides = {key: {"value": value, "reason": "r"}}
    with pytest.raises(ValueError, match=key):
        Residue.model_validate(residue_dict(overrides=overrides))
    with pytest.raises(ValueError, match=key):
        normalize_leaf(key, value)


@pytest.mark.parametrize("key", ["match_cell_footprint", "diode_usable"])
@pytest.mark.parametrize("value", [True, False])
def test_a_boolean_override_takes_a_boolean(key: str, value: bool) -> None:
    residue = Residue.model_validate(residue_dict(overrides={key: {"value": value, "reason": "r"}}))
    assert residue.override_leaves() == {key: value}
    assert normalize_leaf(key, value) is value


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("place_density", "0.3"),
        ("abc_load_in_ff", "5"),
        ("synth_minimum_keep_size", "10"),
        ("synth_minimum_keep_size", 10.0),
        ("synth_minimum_keep_size", True),
        ("place_density", True),
        ("supply.power_nets.VDD", "1.1"),
        ("wire_rc.layers.metal1", ["0.1", 0.2]),
    ],
)
def test_a_number_override_is_a_number_and_not_text(key: str, value: Any) -> None:
    overrides = {key: {"value": value, "reason": "r"}}
    with pytest.raises(ValueError, match=key.replace(".", r"\.")):
        Residue.model_validate(residue_dict(overrides=overrides))


@pytest.mark.parametrize(
    "key", ["nonsense", "files.liberty", "name", "sources", "tracks", "routing", "tapcell.nope"]
)
def test_an_override_names_a_leaf_of_the_model(key: str) -> None:
    overrides = {key: {"value": 1, "reason": "r"}}
    with pytest.raises(ValueError, match=key.replace(".", r"\.")):
        Residue.model_validate(residue_dict(overrides=overrides))


def test_a_leaf_in_values_and_in_overrides_is_refused() -> None:
    with pytest.raises(ValueError, match="place_density"):
        Residue.model_validate(
            residue_dict(
                values={"place_density": 0.3},
                overrides={"place_density": {"value": 0.5, "reason": "r"}},
            )
        )


# ---------------------------------------------------------------------------------------------
# Leaf keys
# ---------------------------------------------------------------------------------------------


def test_nested_text_and_leaf_keys_convert_both_ways() -> None:
    leaves = {
        "site": "unit",
        "tapcell.distance": 120.0,
        "tracks.metal1": TRACK_FINE,
        "wire_rc.layers.metal2": [1.0, 2.0],
        "dont_use_cells": ["A", "B"],
        "pdn.rails": [{"layer": "metal1", "width": 0.2}],
    }
    nested = nest_leaves(leaves)
    assert nested["tapcell"] == {"distance": 120.0}
    assert nested["tracks"] == {"metal1": TRACK_FINE}
    assert nested["pdn"] == {"rails": [{"layer": "metal1", "width": 0.2}]}
    assert Residue.model_validate(residue_dict(values=nested)).values is not None


def test_leaf_annotation_gives_the_type_of_the_value() -> None:
    assert leaf_annotation("tracks.metal7") is Track
    assert leaf_annotation("routing.layer_adjustments.metal2-metal3") == Number
    for bad in ("tracks", "tracks.a.b", "tapcell", "place_density.x", "name", "sources.x"):
        with pytest.raises(ValueError, match=bad.replace(".", r"\.")):
            leaf_annotation(bad)


# ---------------------------------------------------------------------------------------------
# The layer merge
# ---------------------------------------------------------------------------------------------

DERIVED: dict[str, Any] = {
    "site": "unit",
    "supply.voltage": 1.1,
    "supply.power_pin": "VDD",
    "tracks.metal1": TRACK_COARSE,
    "tracks.metal4": TRACK_COARSE,
    "tie_high": {"cell": "TIEH", "port": "H"},
    "dont_use_cells": ["DERIVED_DONT_USE"],
    "time_unit": "1ns",
}
UPSTREAM: dict[str, Any] = {
    "place_density": 0.3,
    "routing.min_layer": "metal2",
    "routing.max_layer": "metal10",
    "dont_use_cells": ["TAP", "FILL"],
    "supply.power_nets.VDD": 1.1,
}


def test_a_later_layer_wins_and_sources_say_which(tmp_path: Path) -> None:
    residue = make_residue(
        values={
            "place_density": 0.5,
            "tracks": {"metal1": TRACK_FINE},
            "tapcell": {"distance": 120},
        },
        overrides={"site": {"value": "other_site", "reason": "the cells use this site"}},
    )
    user = Residue.model_validate(
        {
            "name": "mine",
            "extends": "tiny",
            "values": {"place_density": 0.6, "routing": {"max_layer": "metal9"}},
        }
    )
    model = build_platform(located(tmp_path), residue, user, DERIVED, UPSTREAM)

    assert model.place_density == 0.6 and model.sources["place_density"] == "user"
    assert model.routing.max_layer == "metal9" and model.sources["routing.max_layer"] == "user"
    assert model.routing.min_layer == "metal2" and model.sources["routing.min_layer"] == "upstream"
    assert model.site == "other_site" and model.sources["site"] == "residue"
    assert model.tapcell.distance == 120 and model.sources["tapcell.distance"] == "residue"
    assert model.supply.voltage == 1.1 and model.sources["supply.voltage"] == "derived"
    assert model.time_unit == "1ns" and model.sources["time_unit"] == "derived"
    assert model.tie_high is not None and model.tie_high.cell == "TIEH"


def test_a_table_merges_key_by_key(tmp_path: Path) -> None:
    residue = make_residue(values={"tracks": {"metal1": TRACK_FINE}})
    model = build_platform(located(tmp_path), residue, None, DERIVED, UPSTREAM)
    assert model.tracks["metal1"].x == (0.095, 0.19)
    assert model.sources["tracks.metal1"] == "residue"
    assert model.tracks["metal4"].x == (0.095, 0.28)
    assert model.sources["tracks.metal4"] == "derived"
    assert model.supply.power_nets == {"VDD": 1.1}
    assert model.sources["supply.power_nets.VDD"] == "upstream"


def test_a_list_is_replaced_whole(tmp_path: Path) -> None:
    residue = make_residue(values={"dont_use_cells": ["ONLY_THIS"]})
    model = build_platform(located(tmp_path), residue, None, DERIVED, UPSTREAM)
    assert model.dont_use_cells == ["ONLY_THIS"]
    assert model.sources["dont_use_cells"] == "residue"
    upstream_only = build_platform(located(tmp_path), make_residue(), None, DERIVED, UPSTREAM)
    assert upstream_only.dont_use_cells == ["TAP", "FILL"]
    assert upstream_only.sources["dont_use_cells"] == "upstream"


def test_a_whole_value_is_replaced_not_merged(tmp_path: Path) -> None:
    residue = make_residue(values={"tracks": {"metal4": {"x": [0.1, 0.2]}}})
    model = build_platform(located(tmp_path), residue, None, DERIVED, UPSTREAM)
    assert model.tracks["metal4"].y is None
    assert model.tracks["metal4"].x == (0.1, 0.2)


def test_an_override_wins_over_the_values_of_its_own_layer(tmp_path: Path) -> None:
    residue = make_residue(
        values={"place_density": 0.4},
        overrides={"tapcell.halo": {"value": 2.0, "reason": "the tap cells need room"}},
    )
    user = Residue.model_validate(
        {
            "name": "mine",
            "extends": "tiny",
            "values": {"tapcell": {"halo": 1.0}},
            "overrides": {"place_density": {"value": 0.7, "reason": "dense on purpose"}},
        }
    )
    model = build_platform(located(tmp_path), residue, user, DERIVED, UPSTREAM)
    assert model.place_density == 0.7 and model.sources["place_density"] == "user"
    assert model.tapcell.halo == 1.0 and model.sources["tapcell.halo"] == "user"


def test_sources_has_an_entry_for_every_leaf_with_a_value(tmp_path: Path) -> None:
    residue = make_residue(
        values={
            "place_density": 0.5,
            "tapcell": {"distance": 120},
            "wire_rc": {"signal_layer": "metal3", "layers": {"metal3": [0.1, 0.2]}},
            "pdn": {"power_net": "VDD", "connects": [["metal1", "metal4"]]},
        }
    )
    model = build_platform(located(tmp_path), residue, None, DERIVED, UPSTREAM)
    leaves = model.leaf_values()
    assert set(leaves) <= set(model.sources)
    for expected in (
        "name",
        "corner",
        "files.liberty",
        "files.tech_lef",
        "files.cell_lef",
        "site",
        "tracks.metal1",
        "wire_rc.layers.metal3",
        "pdn.connects",
        "supply.power_nets.VDD",
    ):
        assert expected in model.sources, expected
        assert expected in leaves, expected
    assert model.sources["name"] == "residue"
    assert model.sources["corner"] == "residue"
    assert model.sources["files.liberty"] == "residue"
    assert model.name == "tiny" and model.corner == "typical"
    assert model.files["tech_lef"] == [tmp_path / "tiny" / "lef/tech.lef"]


def test_the_user_file_is_the_layer_of_the_roles_it_gives(tmp_path: Path) -> None:
    user = Residue.model_validate(
        {"name": "mine", "extends": "tiny", "files": {"tech_lef": "mine/tech.lef"}}
    )
    found = located(tmp_path)
    model = build_platform(found, make_residue(), user, {}, {})
    assert model.name == "mine" and model.sources["name"] == "user"
    assert model.sources["files.tech_lef"] == "user"
    assert model.sources["files.cell_lef"] == "residue"
    assert model.sources["files.liberty"] == "residue"


def test_a_user_file_without_extends_is_a_complete_platform(tmp_path: Path) -> None:
    user = make_residue(values={"site": "mine"})
    model = build_platform(located(tmp_path), None, user, {"site": "derived_site"}, {})
    assert model.site == "mine" and model.sources["site"] == "user"
    assert set(model.sources.values()) <= {"derived", "user"}


@pytest.mark.parametrize(
    "corners, requested, layer",
    [
        ({}, "typical", "residue"),
        ({"aliases": {"slow": "fast"}}, "typical", "residue"),  # an unrelated alias
        ({"aliases": {"nominal": "typical"}}, "typical", "user"),  # an alias of the corner
        ({"default": "fast"}, "fast", "user"),  # the default of the user file selects it
        ({"default": "fast"}, "typical", "residue"),  # another corner was asked for
        ({"default": "tt"}, "typical", "user"),  # the default is an alias of the platform
    ],
)
def test_the_corner_comes_from_the_layer_that_selected_it(
    tmp_path: Path, corners: dict, requested: str, layer: str
) -> None:
    user = Residue.model_validate({"name": "mine", "extends": "tiny", "corners": corners})
    files = {"liberty": [tmp_path / "tiny" / f"lib/{requested}.lib"]}
    model = build_platform(
        located(tmp_path, corner=requested, files=files), make_residue(), user, {}, {}
    )
    assert model.corner == requested
    assert model.sources["corner"] == layer


def test_the_corner_of_a_complete_user_file_comes_from_the_user(tmp_path: Path) -> None:
    model = build_platform(located(tmp_path), None, make_residue(), {}, {})
    assert model.sources["corner"] == "user"


@pytest.mark.parametrize("case", ["nothing", "extends_without_residue", "wrong_base"])
def test_the_residue_and_the_user_file_must_agree_on_the_base(tmp_path: Path, case: str) -> None:
    extending = Residue.model_validate({"name": "mine", "extends": "tiny"})
    arguments: dict[str, tuple[Residue | None, Residue | None]] = {
        "nothing": (None, None),
        "extends_without_residue": (None, extending),
        "wrong_base": (make_residue(name="other"), extending),
    }
    residue, user = arguments[case]
    with pytest.raises(ValueError):
        build_platform(located(tmp_path), residue, user, {}, {})


def test_a_corner_that_the_platform_does_not_have_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="slowest"):
        build_platform(located(tmp_path, corner="slowest"), make_residue(), None, {}, {})


@pytest.mark.parametrize(
    "layer, leaves, message",
    [
        ("derived", {"nonsense": 1}, "nonsense"),
        ("upstream", {"place_density": "dense"}, "place_density"),
        ("derived", {"files.liberty": ["a"]}, "files.liberty"),
        ("upstream", {"tracks.a.b": TRACK_FINE}, r"tracks\.a\.b"),
    ],
)
def test_a_lower_layer_that_gives_a_bad_leaf_is_refused_naming_layer_and_leaf(
    tmp_path: Path, layer: str, leaves: dict, message: str
) -> None:
    derived, upstream = ({}, {})
    if layer == "derived":
        derived = leaves
    else:
        upstream = leaves
    with pytest.raises(ValueError, match=message) as error:
        build_platform(located(tmp_path), make_residue(), None, derived, upstream)
    assert layer in str(error.value)


def test_build_platform_does_not_change_its_arguments(tmp_path: Path) -> None:
    residue = make_residue(values={"tracks": {"metal1": TRACK_FINE}})
    derived = json.loads(json.dumps(DERIVED))
    upstream = json.loads(json.dumps(UPSTREAM))
    before = (residue.model_dump(), json.dumps(derived), json.dumps(upstream))
    build_platform(located(tmp_path), residue, None, derived, upstream)
    assert (residue.model_dump(), json.dumps(derived), json.dumps(upstream)) == before


def test_the_bundled_nangate45_builds_with_the_values_it_gives(tmp_path: Path) -> None:
    residue = load_residue("nangate45")
    directory = tmp_path / "nangate45"
    found = Located(
        provider="orfs",
        version="6b305a712966be72d8908695c04398acdbe273ca",
        root=tmp_path,
        directory=directory,
        corner="typical",
        files={"liberty": [directory / "lib/NangateOpenCellLibrary_typical.lib"]},
        upstream_files=[directory / "config.mk"],
    )
    model = build_platform(found, residue, None, {}, {})
    assert model.name == "nangate45" and model.corner == "typical"
    assert residue.description
    assert model.tapcell.distance == 120 and model.endcap_cell == "TAPCELL_X1"
    assert model.wire_rc.signal_layer == "metal3" and model.wire_rc.clock_layer == "metal5"
    assert len(model.wire_rc.layers) == 10
    assert model.wire_rc.layers["metal1"] == (5.4286e-03, 7.41819e-02)
    assert model.wire_rc.layers["metal10"] == (3.75000e-05, 7.37095e-01)
    assert model.routing.layer_adjustments == {"metal2-metal3": 0.5, "metal4-metal10": 0.25}
    assert (model.pdn.power_net, model.pdn.ground_net) == ("VDD", "VSS")
    assert [(r.layer, r.width, r.pitch, r.offset) for r in model.pdn.rails] == [
        ("metal1", 0.17, 2.4, 0.0)
    ]
    assert [(s.layer, s.width, s.pitch, s.offset) for s in model.pdn.straps] == [
        ("metal4", 0.48, 56.0, 2.0),
        ("metal7", 1.4, 30.0, 2.0),
    ]
    assert model.pdn.connects == [("metal1", "metal4"), ("metal4", "metal7")]
    assert model.pdn.pin_layers == ["metal7"]
    assert [(g.net, g.pin_pattern, g.kind) for g in model.pdn.global_connects] == [
        ("VDD", "^VDD$", "power"),
        ("VSS", "^VSS$", "ground"),
    ]
    assert sorted(model.tracks) == ["metal1", "metal2", "metal3"]
    assert model.tracks["metal1"].x == (0.095, 0.19) and model.tracks["metal1"].y == (0.07, 0.14)
    assert model.tracks["metal2"].x == (0.095, 0.19) and model.tracks["metal2"].y == (0.07, 0.14)
    assert all(model.sources[f"tracks.{layer}"] == "residue" for layer in model.tracks)
    assert set(model.leaf_values()) <= set(model.sources)


def test_every_override_of_the_bundled_residue_gives_a_reason() -> None:
    residue = load_residue("nangate45")
    assert residue.overrides
    assert all(override.reason.strip() for override in residue.overrides.values())


# ---------------------------------------------------------------------------------------------
# The redundancy lint
# ---------------------------------------------------------------------------------------------


def test_a_value_that_a_lower_layer_gives_again_is_reported() -> None:
    residue = make_residue(values={"place_density": 0.3, "site": "unit"})
    problems = residue_problems(residue, {"site": "unit"}, {"place_density": 0.3})
    assert problems == [
        "place_density: upstream gives it; delete this line",
        "site: derived gives it; delete this line",
    ]


def test_a_value_that_a_lower_layer_gives_differently_is_reported() -> None:
    residue = make_residue(values={"place_density": 0.5, "routing": {"min_layer": "metal3"}})
    problems = residue_problems(residue, {"place_density": 0.9}, {"routing.min_layer": "metal2"})
    assert problems == [
        "place_density: derived gives 0.9; move it to overrides with a reason",
        'routing.min_layer: upstream gives "metal2"; move it to overrides with a reason',
    ]


def test_the_upstream_layer_is_named_before_the_derived_layer() -> None:
    residue = make_residue(values={"site": "unit"})
    problems = residue_problems(residue, {"site": "derived_site"}, {"site": "unit"})
    assert problems == ["site: upstream gives it; delete this line"]


def test_an_override_that_equals_the_lower_layer_is_reported() -> None:
    residue = make_residue(overrides={"site": {"value": "unit", "reason": "r"}})
    assert residue_problems(residue, {"site": "unit"}, {}) == [
        "site: derived gives the same value; delete this override"
    ]


def test_an_override_that_no_lower_layer_gives_is_reported() -> None:
    residue = make_residue(overrides={"site": {"value": "unit", "reason": "r"}})
    assert residue_problems(residue, {}, {}) == ["site: no lower layer gives it; move it to values"]


def test_a_clean_residue_has_no_problem() -> None:
    residue = make_residue(
        values={"tapcell": {"distance": 120}, "place_density": 0.5},
        overrides={"site": {"value": "other", "reason": "this site fits the cells"}},
    )
    assert residue_problems(residue, {"site": "unit"}, {}) == []


def test_the_lint_compares_a_value_by_what_it_means() -> None:
    residue = make_residue(
        values={"tracks": {"metal1": TRACK_FINE}, "place_density": 1},
    )
    derived = {"tracks.metal1": {"x": (0.095, 0.19), "y": (0.07, 0.14)}}
    upstream = {"place_density": 1.0}
    assert residue_problems(residue, derived, upstream) == [
        "place_density: upstream gives it; delete this line",
        "tracks.metal1: derived gives it; delete this line",
    ]


def test_the_bundled_nangate45_residue_is_clean_against_what_config_mk_gives() -> None:
    """`config.mk` of nangate45 gives these leaves (the adapter's output for it), and the tech LEF
    gives the tracks; the residue gives none of them again."""
    upstream = {
        "place_density": 0.3,
        "routing.min_layer": "metal2",
        "routing.clock_min_layer": "metal4",
        "routing.max_layer": "metal10",
        "io_layers.horizontal": "metal5",
        "io_layers.vertical": "metal6",
        "tap_cell": "TAPCELL_X1",
        "dont_use_cells": ["TAPCELL_X1", "FILLCELL_X1", "AOI211_X1", "OAI211_X1"],
        "supply.power_nets.VDD": 1.1,
        "supply.ground_nets.VSS": 0.0,
        "tie_high": {"cell": "LOGIC1_X1", "port": "Z"},
        "tie_low": {"cell": "LOGIC0_X1", "port": "Z"},
        "min_buffer": {"cell": "BUF_X1", "input": "A", "output": "Z"},
        "abc_driver_cell": "BUF_X1",
        "abc_load_in_ff": 3.898,
        "ir_drop_layer": "metal1",
        "opt_post_grt_wns": 0.0,
        "synth_minimum_keep_size": 10000,
        "fill_cells": ["FILLCELL_X1"],
    }
    # The tech LEF gives one pitch per layer and the offsets `0.095 0.07` for all layers.
    derived = {
        "tracks.metal1": {"x": [0.095, 0.14], "y": [0.07, 0.14]},
        "tracks.metal2": {"x": [0.095, 0.19], "y": [0.07, 0.19]},
        "tracks.metal3": {"x": [0.095, 0.14], "y": [0.07, 0.14]},
    }
    assert residue_problems(load_residue("nangate45"), derived, upstream) == []


# ---------------------------------------------------------------------------------------------
# A user file that extends a bundled platform
# ---------------------------------------------------------------------------------------------


def test_effective_residue_takes_the_users_entry_where_it_gives_one() -> None:
    bundled = make_residue(
        providers={
            "default": "orfs",
            "orfs": {
                "version": "6b305a712966be72d8908695c04398acdbe273ca",
                "directory": "flow/platforms/tiny",
            },
            "directory": {"layout": "orfs"},
        },
        upstream={"adapter": "orfs_config_mk", "files": ["config.mk"]},
        values={"place_density": 0.5},
    )
    user = Residue.model_validate(
        {
            "name": "mine",
            "extends": "tiny",
            "providers": {"default": "directory"},
            "upstream": {"adapter": "orfs_config_mk", "files": ["mine.mk"]},
            "corners": {"default": "fast"},
            "files": {
                "tech_lef": "mine/tech.lef",
                "liberty": {"typical": ["mine/typical.lib"]},
            },
            "values": {"place_density": 0.9},
        }
    )
    effective = effective_residue(bundled, user)
    assert effective.extends is None
    assert effective.name == "mine"
    assert effective.providers.default == "directory"
    assert effective.providers.orfs == bundled.providers.orfs  # kept: the user gave none
    assert effective.upstream is not None and effective.upstream.files == ["mine.mk"]
    assert effective.files.tech_lef == "mine/tech.lef"
    assert effective.files.cell_lef == ["lef/cells.lef"]  # kept
    assert effective.files.liberty == {"typical": ["mine/typical.lib"], "fast": ["lib/fast.lib"]}
    assert effective.resolve_corner(None) == "fast"
    assert effective.resolve_corner("tt") == "typical"  # the alias of the platform stays
    assert effective.values.model_fields_set == set()  # values stay per layer


def test_an_explicit_null_upstream_clears_the_inherited_one_and_an_omitted_one_keeps_it() -> None:
    bundled = make_residue(upstream={"adapter": "orfs_config_mk", "files": ["config.mk"]})
    cleared = Residue.model_validate({"name": "mine", "extends": "tiny", "upstream": None})
    kept = Residue.model_validate({"name": "mine", "extends": "tiny"})
    assert effective_residue(bundled, cleared).upstream is None
    assert effective_residue(bundled, kept).upstream == bundled.upstream


def test_an_explicit_null_description_clears_the_inherited_one() -> None:
    bundled = make_residue(description="the bundled one")
    cleared = Residue.model_validate({"name": "mine", "extends": "tiny", "description": None})
    kept = Residue.model_validate({"name": "mine", "extends": "tiny"})
    assert effective_residue(bundled, cleared).description is None
    assert effective_residue(bundled, kept).description == "the bundled one"


def test_effective_residue_of_one_residue_is_that_residue() -> None:
    residue = make_residue()
    assert effective_residue(residue, None) is residue
    assert effective_residue(None, residue) is residue
    with pytest.raises(ValueError):
        effective_residue(None, None)


def test_a_user_file_that_extends_may_leave_out_what_the_base_gives() -> None:
    user = Residue.model_validate({"name": "mine", "extends": "tiny"})
    assert user.extends == "tiny"
    with pytest.raises(ValueError, match="providers"):
        Residue.model_validate({"name": "mine"})


def test_a_residue_file_given_by_path_may_extend_a_bundled_platform(tmp_path: Path) -> None:
    table = bundled_table()
    table["extends"] = "nangate45"
    path = tmp_path / "platform.yaml"
    path.write_text(yaml.safe_dump(table))
    assert load_residue(path).extends == "nangate45"


# ---------------------------------------------------------------------------------------------
# Corners
# ---------------------------------------------------------------------------------------------


def test_a_corner_resolves_through_the_aliases_of_the_platform() -> None:
    residue = make_residue()
    assert residue.resolve_corner(None) == "typical"
    assert residue.resolve_corner("tt") == "typical"
    assert residue.resolve_corner("typical") == "typical"
    assert residue.resolve_corner("fast") == "fast"


def test_an_unknown_corner_is_refused_naming_the_known_corners_and_aliases() -> None:
    with pytest.raises(ValueError) as error:
        make_residue().resolve_corner("ss")
    message = str(error.value)
    assert "'ss'" in message
    for name in ("typical", "fast", "tt"):
        assert name in message


def test_the_bundled_nangate45_maps_tt_to_typical() -> None:
    residue = load_residue("nangate45")
    assert residue.corners.aliases == {"tt": "typical"}
    assert residue.resolve_corner("tt") == "typical"
    with pytest.raises(ValueError, match="typical"):
        residue.resolve_corner("ff")


# ---------------------------------------------------------------------------------------------
# What the model modules import and read
# ---------------------------------------------------------------------------------------------

PROCESS_MODULES = {"subprocess", "multiprocessing", "pty", "socket", "asyncio", "xeda.proc_utils"}
FILE_READS = {"open", "read_text", "read_bytes", "load_yaml", "iterdir", "glob", "stat", "exists"}


@pytest.mark.parametrize("module", ["model", "build"])
def test_the_pure_modules_start_no_process_and_build_reads_no_file(module: str) -> None:
    tree = ast.parse((PLATFORMS / f"{module}.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not {name for name in imported if name.split(".")[0] in PROCESS_MODULES}
    assert not {name for name in imported if name in PROCESS_MODULES}
    assert not {"os", "xeda.tool", "xeda.run_dir"} & imported
    if module == "build":
        called = {
            node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        }
        assert not called & FILE_READS


# ---------------------------------------------------------------------------------------------
# Packaging
# ---------------------------------------------------------------------------------------------


def matches(pattern: str, relative: str) -> bool:
    """Whether the package-data glob `pattern` selects `relative` (a `*` stays in one directory)."""
    parts, wanted = relative.split("/"), pattern.split("/")
    return len(parts) == len(wanted) and all(map(fnmatchcase, parts, wanted))


def test_every_platform_file_that_xeda_ships_is_selected_by_the_packaging() -> None:
    setuptools = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["setuptools"]
    package = "xeda.platforms"

    find = setuptools["packages"]["find"]
    assert any(fnmatchcase(package, pattern) for pattern in find["include"])
    assert not any(fnmatchcase(package, pattern) for pattern in find.get("exclude", []))

    include = setuptools["package-data"][package]
    exclude = [
        pattern
        for key in (package, "*")
        for pattern in setuptools.get("exclude-package-data", {}).get(key, [])
    ]
    shipped = [
        path.relative_to(PLATFORMS).as_posix() for path in PLATFORMS.glob("*/platform.yaml")
    ] + ["NOTICE.md"]
    assert "nangate45/platform.yaml" in shipped
    for relative in shipped:
        assert (PLATFORMS / relative).is_file()
        assert any(matches(pattern, relative) for pattern in include), relative
        assert not any(matches(pattern, relative) for pattern in exclude), relative
    # The techmaps of a platform ride along.
    assert any(matches(pattern, "nangate45/cells_latch.v") for pattern in include)


def shipping_problems(root: Path) -> list[str]:
    """Name each file under `root` that is none of the kinds the wheel may ship."""
    problems = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(root)
        parts = relative.parts
        if path.suffix == ".py" or relative.as_posix() == "NOTICE.md":
            continue
        if len(parts) == 2 and (parts[1] == "platform.yaml" or path.suffix == ".v"):
            continue
        problems.append(relative.as_posix())
    return problems


def test_the_platforms_directory_holds_only_the_kinds_of_file_it_may_ship() -> None:
    assert shipping_problems(PLATFORMS) == []


def test_a_pdk_library_file_under_the_platforms_directory_is_named_as_a_problem(
    tmp_path: Path,
) -> None:
    for relative in ("nangate45/lib/cells.lib.gz", "nangate45/lef/cells.lef", "nangate45/x.gds"):
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_bytes(b"")
    (tmp_path / "nangate45" / "cells_latch.v").write_text("")
    (tmp_path / "nangate45" / "platform.yaml").write_text("")
    (tmp_path / "model.py").write_text("")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "model.cpython-311.pyc").write_bytes(b"")
    assert shipping_problems(tmp_path) == [
        "nangate45/lef/cells.lef",
        "nangate45/lib/cells.lib.gz",
        "nangate45/x.gds",
    ]


def test_the_package_data_of_the_platforms_is_read_from_the_installed_package() -> None:
    notice = resources.files("xeda.platforms").joinpath("NOTICE.md").read_text()
    assert "BSD 3-Clause" in notice
    assert "Apache" in notice
    assert "does not ship" in notice
    table = load_yaml(PLATFORMS / "nangate45" / "platform.yaml")
    assert table["name"] == "nangate45"
