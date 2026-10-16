"""The platform file that makes sky130hd read the files and the values of OpenROAD-flow-scripts.

`tools/asic_parity/overrides/sky130hd.yaml` extends the bundled `sky130hd`. The comparison with
OpenROAD-flow-scripts (ORFS) uses it, so that both flows run on the same library and the same
values. The checks without an ORFS checkout look at the file and at the model it builds from a
directory of made-up paths. The checks with a checkout (`XEDA_TESTS_ORFS`, skipped without it)
resolve every role to a real file and compare the model with `config.mk`.
"""

from pathlib import Path
from typing import Any

import pytest

from xeda.platforms import Residue, build_platform, load_residue, locate
from xeda.platforms.derive import derive
from xeda.platforms.model import Located
from xeda.platforms.residue import effective_residue
from xeda.platforms.upstream import read_upstream

from .tool_utils import require_orfs_checkout

ROOT = Path(__file__).parent.parent
OVERRIDE = ROOT / "tools" / "asic_parity" / "overrides" / "sky130hd.yaml"


@pytest.fixture(scope="module")
def bundled() -> Residue:
    return load_residue("sky130hd")


@pytest.fixture(scope="module")
def parity() -> Residue:
    return load_residue(OVERRIDE)


def _fake_located(structure: Residue, directory: Path) -> Located:
    """The platform as a directory of the layout of ORFS holds it, with paths that need not
    exist: building a model reads no file."""
    corner = structure.resolve_corner(None)
    return Located(
        provider="directory",
        version="unpinned",
        root=directory.parent,
        directory=directory,
        corner=corner,
        files={
            role: [directory / path for path in paths]
            for role, paths in structure.files.role_paths(corner).items()
        },
        upstream_files=[directory / path for path in structure.upstream.files],
    )


# ---------------------------------------------------------------------------- the file itself


def test_the_file_extends_sky130hd_and_reads_the_layout_of_orfs(bundled, parity):
    assert parity.name == "sky130hd" and parity.extends == "sky130hd"
    structure = effective_residue(bundled, parity)
    assert structure.providers.default == "directory"
    assert structure.providers.directory is not None
    assert structure.providers.directory.layout == "orfs"
    assert structure.upstream is not None
    assert (structure.upstream.adapter, structure.upstream.files) == (
        "orfs_config_mk",
        ["config.mk"],
    )


def test_the_corner_is_that_of_the_bundled_platform(bundled, parity):
    structure = effective_residue(bundled, parity)
    assert structure.resolve_corner(None) == bundled.resolve_corner(None) == "tt_025C_1v80"
    assert structure.resolve_corner("tt") == "tt_025C_1v80"
    assert structure.files.liberty["tt_025C_1v80"] == ["lib/sky130_fd_sc_hd__tt_025C_1v80.lib"]


def test_every_role_of_the_bundled_platform_is_set_in_the_layout_of_orfs(bundled, parity):
    """No role keeps a path of the build of `ciel`, which a directory of ORFS lacks."""
    structure = effective_residue(bundled, parity)
    corner = structure.resolve_corner(None)
    roles = structure.files.role_paths(corner)
    assert set(roles) == set(bundled.files.role_paths(corner)) | {"clkgate_map"}
    for role, paths in roles.items():
        assert not any(path.startswith("libs.") for path in paths), role


def test_an_override_changes_a_value_of_the_bundled_platform_and_a_value_adds_one(bundled, parity):
    """The lines are in the right place: an override replaces a leaf that the bundled platform
    gives, with another value, and a leaf under `values` is one that it does not give."""
    held = {**bundled.value_leaves(), **bundled.override_leaves()}
    for key, value in parity.override_leaves().items():
        assert key in held, f"{key} is no leaf of the bundled platform: move it to values"
        assert held[key] != value, f"{key} has the value of the bundled platform: delete it"
    for key in parity.value_leaves():
        assert key not in held, f"{key} is a leaf of the bundled platform: move it to overrides"


def test_the_file_gives_the_values_of_orfs_in_the_model(bundled, parity, tmp_path):
    structure = effective_residue(bundled, parity)
    located = _fake_located(structure, tmp_path / "sky130hd")
    model = build_platform(located, bundled, parity, {}, {})
    assert model.pdn.power_net == "VDD" and model.pdn.ground_net == "VSS"
    assert len(model.pdn.global_connects) == 9
    assert model.pdn.pin_layers == ["met5"]
    assert [(s.layer, s.pitch, s.offset) for s in model.pdn.straps] == [
        ("met4", 27.14, 13.57),
        ("met5", 27.2, 13.6),
    ]
    assert [(r.layer, r.pitch) for r in model.pdn.rails] == [("met1", 5.44)]
    assert model.tapcell.distance == 14
    assert model.routing.layer_adjustments["met1-met5"] == 0.2
    assert model.routing.layer_adjustments["li1"] == 0
    assert (model.wire_rc.signal_layer, model.wire_rc.clock_layer) == ("met1", "met3")
    assert set(model.wire_rc.layers) == {"li1", "met1", "met2", "met3", "met4", "met5"}
    assert set(model.wire_rc.vias) == {"mcon", "via", "via2", "via3", "via4"}
    for key in ("pdn.power_net", "tapcell.distance", "wire_rc.layers.met1", "files.clkgate_map"):
        assert model.sources[key] == "user", key
    assert model.sources["pdn.connects"] == "residue"


# ---------------------------------------------------------------------------- the ORFS checkout


@pytest.fixture(scope="module")
def orfs() -> Path:
    """The platform directories of the ORFS checkout that `XEDA_TESTS_ORFS` names."""
    platforms = require_orfs_checkout() / "flow" / "platforms"
    if not (platforms / "sky130hd" / "config.mk").is_file():
        pytest.fail(f"the ORFS checkout of XEDA_TESTS_ORFS has no {platforms}/sky130hd/config.mk")
    return platforms


def test_the_directory_provider_resolves_every_role_to_a_file_of_orfs(bundled, parity, orfs):
    structure = effective_residue(bundled, parity)
    located = locate(structure, None, None, orfs, None)
    assert located.provider == "directory"
    assert located.directory == orfs / "sky130hd"
    assert set(located.files) >= {"liberty", "tech_lef", "cell_lef", "gds", "rcx_rules"}
    assert set(located.files) >= {"latch_map", "clkgate_map", "adder_map"}
    for role, paths in located.files.items():
        assert paths and all(path.is_file() for path in paths), role
    assert [path.is_file() for path in located.upstream_files] == [True]


def _layers(bundled: Residue, parity: Residue, orfs: Path) -> tuple[Any, dict, dict]:
    structure = effective_residue(bundled, parity)
    located = locate(structure, None, None, orfs, None)
    derived = derive(located.files, located.corner)
    upstream = read_upstream(
        structure.upstream.adapter, located.upstream_files, located.directory, located.library
    )
    return located, derived, upstream


def test_the_values_of_the_file_are_not_given_again_by_the_layers_below(bundled, parity, orfs):
    """A line of `values` is a leaf that neither the files nor `config.mk` give, and an
    override is a leaf that a layer below gives with another value."""
    _, derived, upstream = _layers(bundled, parity, orfs)
    below = {**derived, **upstream}
    assert not set(parity.value_leaves()) & set(below), "a value that a layer below gives"
    held = {**bundled.value_leaves(), **bundled.override_leaves()}
    for key in parity.override_leaves():
        assert key in held or key in below, key


def test_the_model_takes_the_cells_and_the_settings_from_config_mk(bundled, parity, orfs):
    located, derived, upstream = _layers(bundled, parity, orfs)
    model = build_platform(located, bundled, parity, derived, upstream)
    assert model.site == "unithd"
    assert model.min_buffer is not None and model.min_buffer.cell == "sky130_fd_sc_hd__buf_4"
    assert model.abc_driver_cell == "sky130_fd_sc_hd__buf_1" and model.abc_load_in_ff == 5
    assert len(model.dont_use_cells) == 36, "the cells of config.mk, not those of open_pdks"
    assert model.sources["dont_use_cells"] == "upstream"
    assert model.match_cell_footprint is True
    assert model.place_density == 0.6
    assert model.fill_cells[:2] == ["sky130_fd_sc_hd__fill_1", "sky130_fd_sc_hd__fill_2"]
    assert (model.routing.min_layer, model.routing.max_layer) == ("met1", "met5")
    assert model.routing.clock_min_layer == "met3"
    for key in ("min_buffer", "dont_use_cells", "match_cell_footprint"):
        assert model.sources[key] == "upstream", key
    assert model.files["tech_lef"] == [orfs / "sky130hd" / "lef" / "sky130_fd_sc_hd.tlef"]
    assert model.files["cell_lef"] == [orfs / "sky130hd" / "lef" / "sky130_fd_sc_hd_merged.lef"]
