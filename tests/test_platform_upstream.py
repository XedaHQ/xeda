"""The adapters that read the configuration of a PDK or of OpenROAD-flow-scripts.

Two adapters, each with a closed set of keys, read what the PDK already says (the placement site,
the tap cells, the layers, the cells) so that xeda does not copy it into a residue file. One reads
the `config.tcl` of open_pdks and IHP, the other the `config.mk` of OpenROAD-flow-scripts. Neither
evaluates Tcl or Make: a line it needs but does not understand is an error that names the line.
"""

import re
from pathlib import Path
from typing import Any

import pytest

from xeda.platforms import Located, Residue, build_platform
from xeda.platforms.model import normalize_leaf
from xeda.platforms.upstream import (
    PlatformConfigError,
    read_open_pdks_config,
    read_orfs_config_mk,
)

TINY = Path(__file__).parent / "resources" / "platforms" / "tiny"
NANGATE45_CONFIG = Path(
    "/Volumes/src/opensource-asics/OpenROAD-flow-scripts/flow/platforms/nangate45/config.mk"
)


def write(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def read_tcl(tmp_path: Path, *texts: str) -> dict[str, Any]:
    paths = [write(tmp_path, f"config{index}.tcl", text) for index, text in enumerate(texts)]
    return read_open_pdks_config(paths)


def read_mk(tmp_path: Path, text: str, platform_dir: Path = Path("/pdk")) -> dict[str, Any]:
    return read_orfs_config_mk(write(tmp_path, "config.mk", text), platform_dir)


def with_empty_residue(tmp_path: Path, upstream: dict[str, Any]) -> Any:
    """Build a platform model from the upstream leaves and a residue that gives nothing."""
    residue = Residue.model_validate(
        {
            "name": "tiny",
            "providers": {"default": "directory", "directory": {"layout": "orfs"}},
            "corners": {"default": "typical"},
            "files": {"liberty": {"typical": ["lib/typical.lib"]}, "tech_lef": "lef/tech.lef"},
        }
    )
    directory = tmp_path / "tiny"
    found = Located(
        provider="directory",
        version="unpinned",
        root=tmp_path,
        directory=directory,
        corner="typical",
        files={
            "liberty": [directory / "lib/typical.lib"],
            "tech_lef": [directory / "lef/tech.lef"],
        },
        upstream_files=[],
    )
    return build_platform(found, residue, None, {}, upstream)


def refusal(read: Any, *args: Any) -> str:
    """The message of the `PlatformConfigError` that `read(*args)` raises."""
    with pytest.raises(PlatformConfigError) as raised:
        read(*args)
    assert isinstance(raised.value, ValueError)
    return str(raised.value)


# ---------------------------------------------------------------------------------------------
# config.tcl: every key of the set
# ---------------------------------------------------------------------------------------------

CELL = {"cell": "tiny_diode", "port": "DIODE"}
TCL_ROWS: list[tuple[str, str, Any]] = [
    ("PLACE_SITE", "site", "core"),
    ("FP_WELLTAP_CELL", "tap_cell", "tiny_core_tap"),
    ("FP_ENDCAP_CELL", "endcap_cell", "tiny_endcap"),
    ("FP_TAPCELL_DIST", "tapcell.distance", 12.5),
    ("SYNTH_TIEHI_PORT", "tie_high", {"cell": "tiny_tiehi", "port": "HI"}),
    ("SYNTH_TIELO_PORT", "tie_low", {"cell": "tiny_tielo", "port": "LO"}),
    (
        "SYNTH_MIN_BUF_PORT",
        "min_buffer",
        {"cell": "tiny_buf_1", "input": "A", "output": "X"},
    ),
    ("SYNTH_DRIVING_CELL", "abc_driver_cell", "tiny_buf_4"),
    ("FILL_CELL, DECAP_CELL", "fill_cells", ["tiny_fill_2", "tiny_fill_1", "tiny_decap_3"]),
    ("DIODE_CELL, DIODE_CELL_PIN", "antenna_cell", CELL),
    ("RT_MIN_LAYER", "routing.min_layer", "m1"),
    ("RT_MAX_LAYER", "routing.max_layer", "m4"),
    ("RT_CLOCK_MIN_LAYER", "routing.clock_min_layer", "m3"),
    ("FP_IO_HLAYER", "io_layers.horizontal", "m3"),
    ("FP_IO_VLAYER", "io_layers.vertical", "m4"),
    ("DATA_WIRE_RC_LAYER", "wire_rc.signal_layer", "m2"),
    ("CLOCK_WIRE_RC_LAYER", "wire_rc.clock_layer", "m4"),
    ("LAYERS_RC", "wire_rc.layers.m1", [8.5e-03, 1e-10]),
    ("LAYERS_RC", "wire_rc.layers.m2", [2.5e-03, 1.5e-04]),
    ("VIAS_R", "wire_rc.vias.via1", 2.0e-03),
    ("VIAS_R", "wire_rc.vias.via2", 0.4e-03),
    (
        "CTS_ROOT_BUFFER, CTS_CLK_BUFFER_LIST",
        "cts_buffers",
        ["tiny_clkbuf_8", "tiny_clkbuf_4", "tiny_clkbuf_2"],
    ),
    (
        "FP_PDN_RAIL_LAYER, _WIDTH, _OFFSET",
        "pdn.rails",
        [{"layer": "m1", "width": 0.48, "offset": 0.0}],
    ),
    (
        "FP_PDN_VERTICAL_LAYER, _VWIDTH, _VPITCH, _VOFFSET and the same for H",
        "pdn.straps",
        [
            {"layer": "m3", "width": 1.6, "pitch": 150.0, "offset": 16.5},
            {"layer": "m4", "width": 1.8, "pitch": 160.0, "offset": 17.0},
        ],
    ),
]


@pytest.fixture(scope="module")
def tiny_tcl() -> dict[str, Any]:
    return read_open_pdks_config([TINY / "config.tcl"])


@pytest.mark.parametrize(("keys", "leaf", "expected"), TCL_ROWS, ids=[row[1] for row in TCL_ROWS])
def test_config_tcl_gives_the_leaf_of_each_key(
    tiny_tcl: dict[str, Any], keys: str, leaf: str, expected: Any
) -> None:
    assert tiny_tcl[leaf] == expected, keys


def test_config_tcl_gives_exactly_the_leaves_of_its_keys(tiny_tcl: dict[str, Any]) -> None:
    """The keys that the reader does not map give nothing, whatever the file holds there."""
    assert set(tiny_tcl) == {leaf for _, leaf, _ in TCL_ROWS}


def test_every_leaf_of_config_tcl_validates_as_its_type(
    tiny_tcl: dict[str, Any], tmp_path: Path
) -> None:
    for key, value in tiny_tcl.items():
        normalize_leaf(key, value)
    model = with_empty_residue(tmp_path, tiny_tcl)
    assert model.site == "core" and model.tapcell.distance == 12.5
    assert model.min_buffer.output == "X" and model.antenna_cell.port == "DIODE"
    assert model.wire_rc.layers["m1"] == (8.5e-03, 1e-10)
    assert [strap.layer for strap in model.pdn.straps] == ["m3", "m4"]
    assert model.pdn.rails[0].pitch is None
    assert all(model.sources[key] == "upstream" for key in tiny_tcl)


def test_the_layer_adjustments_of_open_pdks_are_not_mapped(tmp_path: Path) -> None:
    """The list starts at the lowest routing layer of the technology, which only the tech LEF
    knows, so the reader leaves it out (even a value it could not parse)."""
    leaves = read_tcl(tmp_path, 'set ::env(GRT_LAYER_ADJUSTMENTS) "0.99,0,0,0,0,0"\n')
    assert leaves == {}
    assert read_tcl(tmp_path, "set ::env(GRT_LAYER_ADJUSTMENTS) [bad\n") == {}


# ---------------------------------------------------------------------------------------------
# config.tcl: the forms of a value
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        'set ::env(PLACE_SITE) "core"',
        "set ::env(PLACE_SITE) {core}",
        "set ::env(PLACE_SITE) core",
        'set ::env(PLACE_SITE) "core" ; # the site',
        "set ::env(PLACE_SITE)   core  ;",
        "\tset ::env(PLACE_SITE) core",
    ],
)
def test_a_value_is_quoted_braced_or_a_bare_word(tmp_path: Path, line: str) -> None:
    assert read_tcl(tmp_path, line + "\n") == {"site": "core"}


def test_a_comment_and_a_blank_line_give_nothing(tmp_path: Path) -> None:
    text = "# set ::env(PLACE_SITE) a\n\n   # set ::env(RT_MIN_LAYER) b\n"
    assert read_tcl(tmp_path, text) == {}


def test_a_backslash_continues_a_line(tmp_path: Path) -> None:
    text = 'set ::env(FILL_CELL) "fill_2 \\\n    fill_1 \\\n    fill_4"\n'
    assert read_tcl(tmp_path, text) == {"fill_cells": ["fill_2", "fill_1", "fill_4"]}


def test_a_key_is_replaced_by_a_later_line_of_the_same_file(tmp_path: Path) -> None:
    text = "set ::env(PLACE_SITE) a\nset ::env(PLACE_SITE) b\n"
    assert read_tcl(tmp_path, text) == {"site": "b"}


def test_a_value_refers_to_a_key_read_before_in_both_forms(tmp_path: Path) -> None:
    text = (
        "set ::env(PLACE_SITE) core\n"
        'set ::env(FP_WELLTAP_CELL) "tap_$::env(PLACE_SITE)"\n'
        'set ::env(FP_ENDCAP_CELL) "${::env(PLACE_SITE)}_cap"\n'
        "set ::env(RT_MIN_LAYER) $::env(PLACE_SITE)\n"
    )
    leaves = read_tcl(tmp_path, text)
    assert leaves["tap_cell"] == "tap_core" and leaves["endcap_cell"] == "core_cap"
    assert leaves["routing.min_layer"] == "core"


def test_a_later_file_wins_and_may_refer_to_a_key_of_an_earlier_file(tmp_path: Path) -> None:
    pdk = "set ::env(PLACE_SITE) a\nset ::env(FP_ENDCAP_CELL) e\nset ::env(CTS_ROOT_BUFFER) root\n"
    library = (
        'set ::env(PLACE_SITE) "${::env(PLACE_SITE)}b"\n'
        'set ::env(CTS_CLK_BUFFER_LIST) "buf_2 buf_1"\n'
    )
    assert read_tcl(tmp_path, pdk, library) == {
        "site": "ab",
        "endcap_cell": "e",
        "cts_buffers": ["root", "buf_2", "buf_1"],
    }


def test_the_names_that_librelane_uses_are_read_like_the_others(tmp_path: Path) -> None:
    text = (
        'set ::env(FILL_CELLS) "fill_1 fill_2"\n'
        'set ::env(DECAP_CELLS) "decap_*"\n'
        'set ::env(DIODE_CELL) "diode/A"\n'
        "set ::env(CTS_ROOT_BUFFER) root\n"
        'set ::env(CTS_CLK_BUFFERS) "buf_8 root"\n'
        "set ::env(PDN_RAIL_LAYER) m1\nset ::env(PDN_RAIL_WIDTH) 0.44\n"
    )
    assert read_tcl(tmp_path, text) == {
        "fill_cells": ["fill_1", "fill_2", "decap_*"],
        "antenna_cell": {"cell": "diode", "port": "A"},
        "cts_buffers": ["root", "buf_8"],
        "pdn.rails": [{"layer": "m1", "width": 0.44}],
    }


def test_the_tap_distance_may_be_zero(tmp_path: Path) -> None:
    assert read_tcl(tmp_path, "set ::env(FP_TAPCELL_DIST) 0\n") == {"tapcell.distance": 0.0}


def test_a_driving_cell_is_the_cell_part_of_a_cell_slash_pin_form(tmp_path: Path) -> None:
    assert read_tcl(tmp_path, "set ::env(SYNTH_DRIVING_CELL) buf_4/X\n") == {
        "abc_driver_cell": "buf_4"
    }
    assert read_tcl(tmp_path, "set ::env(SYNTH_DRIVING_CELL) buf_4\n") == {
        "abc_driver_cell": "buf_4"
    }


def test_an_empty_text_value_gives_no_leaf_and_clears_an_earlier_one(tmp_path: Path) -> None:
    assert read_tcl(tmp_path, "set ::env(PLACE_SITE) a\n", 'set ::env(PLACE_SITE) ""\n') == {}


def test_a_diode_cell_names_its_pin_in_either_way(tmp_path: Path) -> None:
    both = 'set ::env(DIODE_CELL) "d/A"\nset ::env(DIODE_CELL_PIN) "A"\n'
    assert read_tcl(tmp_path, both) == {"antenna_cell": {"cell": "d", "port": "A"}}
    pin_first = 'set ::env(DIODE_CELL_PIN) "A"\nset ::env(DIODE_CELL) "d"\n'
    assert read_tcl(tmp_path, pin_first) == {"antenna_cell": {"cell": "d", "port": "A"}}


def test_a_pin_without_a_diode_cell_gives_nothing(tmp_path: Path) -> None:
    assert read_tcl(tmp_path, 'set ::env(DIODE_CELL_PIN) "A"\n') == {}


def test_the_repeats_of_the_clock_buffers_are_dropped_keeping_the_first(tmp_path: Path) -> None:
    text = 'set ::env(CTS_ROOT_BUFFER) a\nset ::env(CTS_CLK_BUFFER_LIST) "b a c b"\n'
    assert read_tcl(tmp_path, text) == {"cts_buffers": ["a", "b", "c"]}


# ---------------------------------------------------------------------------------------------
# config.tcl: the lines that are ignored
# ---------------------------------------------------------------------------------------------


def test_a_line_with_a_key_outside_the_set_is_ignored_whatever_it_holds(tmp_path: Path) -> None:
    text = (
        "set ::env(OTHER) [bad\n"
        'set ::env(OTHER) "$::env(UNKNOWN) [glob *]"\n'
        "source $::env(HOME)/x.tcl\n"
        "proc f {} {\n    set ::env(OTHER) inside\n}\n"
        "set tmp $::env(PLACE_SITE)\n"
        "foreach x {a b} { puts $x }\n"
        "}\n}\n"
    )
    assert read_tcl(tmp_path, text) == {}


def test_a_read_of_the_environment_is_not_a_write(tmp_path: Path) -> None:
    text = (
        "set ::env(PLACE_SITE) core\n"
        'puts "$::env(PLACE_SITE) ${::env(PLACE_SITE)} $::env($name)"\n'
        "set tmp $::env(PLACE_SITE)\n"
        "if { [info exist ::env(UNRELATED)] } { puts ok }\n"
    )
    assert read_tcl(tmp_path, text) == {"site": "core"}


def test_a_reference_to_a_key_in_a_line_of_another_key_is_not_an_error(tmp_path: Path) -> None:
    text = 'set ::env(PLACE_SITE) core\nset ::env(OTHER) "$::env(PLACE_SITE)/x"\n'
    assert read_tcl(tmp_path, text) == {"site": "core"}


def test_a_value_cannot_refer_to_a_key_that_the_reader_does_not_map(tmp_path: Path) -> None:
    text = "set ::env(STD_CELL_LIBRARY) lib\nset ::env(PLACE_SITE) $::env(STD_CELL_LIBRARY)\n"
    message = refusal(read_open_pdks_config, [write(tmp_path, "c.tcl", text)])
    assert "c.tcl:2" in message and "STD_CELL_LIBRARY" in message


# ---------------------------------------------------------------------------------------------
# config.tcl: the refusals
# ---------------------------------------------------------------------------------------------

TCL_REFUSALS: list[tuple[str, int, str]] = [
    ('# a\nset ::env(PLACE_SITE) "$::env(UNKNOWN)"\n', 2, "UNKNOWN"),
    ("set ::env(PLACE_SITE) $::env(LATER)\nset ::env(LATER) x\n", 1, "LATER"),
    ("if { 1 } {\n  set ::env(PLACE_SITE) x\n}\n", 2, "inside a block"),
    ("if { ![info exist ::env(PLACE_SITE)] } {\n  set ::env(PLACE_SITE) x\n}\n", 1, "PLACE_SITE"),
    ("proc f {} {\n  if { 1 } {\n    set ::env(RT_MIN_LAYER) x\n  }\n}\n", 3, "inside a block"),
    ("if { 1 } { set ::env(PLACE_SITE) x }\n", 1, "PLACE_SITE"),
    ("if { [info exist ::env(PLACE_SITE)] } {\n}\n", 1, "PLACE_SITE"),
    ("set ::env(PLACE_SITE) [lindex $x 0]\n", 1, "["),
    ('set ::env(PLACE_SITE) "a[b]"\n', 1, "["),
    ("set ::env(FILL_CELL) [glob *.lef]\n", 1, "["),
    ("lappend ::env(FILL_CELL) x\n", 1, "FILL_CELL"),
    ("lappend ::env(FILL_CELLS) x\n", 1, "FILL_CELLS"),
    ("append ::env(FP_PDN_RAIL_LAYER) x\n", 1, "FP_PDN_RAIL_LAYER"),
    ("lappend ::env(LAYERS_RC) x\n", 1, "LAYERS_RC"),
    ("append ::env(DIODE_CELL_PIN) x\n", 1, "DIODE_CELL_PIN"),
    ("array set ::env {PLACE_SITE x}\n", 1, "::env"),
    ("array set ::env [list PLACE_SITE x]\n", 1, "::env"),
    ("array unset ::env\n", 1, "::env"),
    ("dict set ::env PLACE_SITE x\n", 1, "::env"),
    ("upvar #0 ::env e\n", 1, "::env"),
    ("set ::env($k) x\n", 1, "::env"),
    ("foreach {k v} {PLACE_SITE x} {\n  set ::env($k) $v\n}\n", 2, "::env"),
    ("set {::env(PLACE_SITE)} x\n", 1, "PLACE_SITE"),
    ("eval {set ::env(PLACE_SITE) x}\n", 1, "PLACE_SITE"),
    ("switch x { x { set ::env(RT_MAX_LAYER) m5 } }\n", 1, "RT_MAX_LAYER"),
    ("set ::env(OTHER) x ; set ::env(PLACE_SITE) y\n", 1, "PLACE_SITE"),
    ("set ::env(PLACE_SITE) a ; array set ::env {}\n", 1, "PLACE_SITE"),
    ("append ::env(PLACE_SITE) x\n", 1, "PLACE_SITE"),
    ("unset ::env(PLACE_SITE)\n", 1, "PLACE_SITE"),
    ("dict set ::env(PLACE_SITE) a b\n", 1, "PLACE_SITE"),
    ("set ::env(FILL_CELL) $tmp\n", 1, "$"),
    ("set ::env(PLACE_SITE) a$b\n", 1, "$"),
    ("set ::env(PLACE_SITE) {a $b}\n", 1, "$"),
    ("set ::env(PLACE_SITE) {a {b}}\n", 1, "{"),
    ('set ::env(PLACE_SITE) "a\\nb"\n', 1, "\\"),
    ('set ::env(PLACE_SITE) "a\n', 1, "quote"),
    ("set ::env(PLACE_SITE) {a\n", 1, "brace"),
    ("set ::env(PLACE_SITE)\n", 1, "value"),
    ("set ::env(PLACE_SITE) a b\n", 1, "PLACE_SITE"),
    ("set ::env(PLACE_SITE) a ; set x y\n", 1, "PLACE_SITE"),
    ('set ::env(PLACE_SITE) "a b"\n', 1, "one word"),
    ('set ::env(RT_MIN_LAYER) "m1 m2"\n', 1, "one word"),
    ("set ::env(FP_TAPCELL_DIST) abc\n", 1, "number"),
    ("set ::env(FP_TAPCELL_DIST) 1,5\n", 1, "number"),
    ('set ::env(FP_TAPCELL_DIST) "1 2"\n', 1, "number"),
    ("set ::env(FP_TAPCELL_DIST) nan\n", 1, "number"),
    ('set ::env(SYNTH_TIEHI_PORT) "cell"\n', 1, "cell port"),
    ('set ::env(SYNTH_TIELO_PORT) "cell port extra"\n', 1, "cell port"),
    ('set ::env(SYNTH_MIN_BUF_PORT) "cell in"\n', 1, "cell in out"),
    ('set ::env(SYNTH_MIN_BUF_PORT) "cell a b c"\n', 1, "cell in out"),
    ('set ::env(SYNTH_DRIVING_CELL) "a b"\n', 1, "one word"),
    ('set ::env(SYNTH_DRIVING_CELL) "a/b/c"\n', 1, "cell"),
    ('set ::env(DIODE_CELL) "d"\n', 1, "pin"),
    ('set ::env(DIODE_CELL) "d/A"\nset ::env(DIODE_CELL_PIN) "B"\n', 1, "A"),
    ('set ::env(DIODE_CELL) "d x"\nset ::env(DIODE_CELL_PIN) A\n', 1, "one word"),
    ("set ::env(LAYERS_RC) [list]\n", 1, "dict create"),
    ("set ::env(VIAS_R) {}\n", 1, "dict create"),
    ('dict set ::env(LAYERS_RC) "m5" m1 res 1\n', 1, "*"),
    ('dict set ::env(LAYERS_RC) "*" m1 foo 1\n', 1, "res"),
    ('dict set ::env(LAYERS_RC) "*" m1 res abc\n', 1, "number"),
    ('dict set ::env(LAYERS_RC) "*" m1 res\n', 1, "LAYERS_RC"),
    ('dict set ::env(LAYERS_RC) "*" m1.a res 1\n', 1, "LAYERS_RC"),
    ('dict set ::env(LAYERS_RC) "*" m1 res 1 extra\n', 1, "LAYERS_RC"),
    ('dict set ::env(LAYERS_RC) "*" m1 res 1\n', 1, "cap"),
    ('dict set ::env(LAYERS_RC) "*" m1 cap 1\n', 1, "res"),
    ('dict set ::env(VIAS_R) "*" v1 cap 1\n', 1, "res"),
    ('dict set ::env(VIAS_R) "x" v1 res 1\n', 1, "*"),
    ('if { 1 } {\n  dict set ::env(VIAS_R) "*" v1 res 1\n}\n', 2, "inside a block"),
    ("set ::env(FP_PDN_RAIL_LAYER) m1\n", 1, "FP_PDN_RAIL_WIDTH"),
    ("set ::env(FP_PDN_VERTICAL_LAYER) m3\nset ::env(FP_PDN_VWIDTH) 1\n", 1, "VPITCH"),
    ("set ::env(PDN_HORIZONTAL_LAYER) m4\nset ::env(PDN_HWIDTH) 1\n", 1, "HPITCH"),
    (
        "set ::env(PDN_HORIZONTAL_LAYER) m4\nset ::env(PDN_HWIDTH) 1\nset ::env(PDN_HPITCH) 9\n",
        1,
        "HOFFSET",
    ),
    ("set ::env(FP_PDN_RAIL_LAYER) m1\nset ::env(FP_PDN_RAIL_WIDTH) wide\n", 2, "number"),
]


@pytest.mark.parametrize(
    ("text", "line", "fragment"),
    TCL_REFUSALS,
    ids=[f"{index:02}" for index in range(len(TCL_REFUSALS))],
)
def test_config_tcl_refuses_what_it_does_not_understand_naming_the_line(
    tmp_path: Path, text: str, line: int, fragment: str
) -> None:
    path = write(tmp_path, "config.tcl", text)
    message = refusal(read_open_pdks_config, [path])
    assert re.search(rf"config\.tcl:{line}\b", message), message
    assert fragment in message, message


def test_the_message_of_a_refusal_names_the_file_of_the_line(tmp_path: Path) -> None:
    first = write(tmp_path, "pdk.tcl", "set ::env(PLACE_SITE) a\n")
    second = write(tmp_path, "library.tcl", "\n\nset ::env(RT_MIN_LAYER) [x]\n")
    message = refusal(read_open_pdks_config, [first, second])
    assert "library.tcl:3" in message and "pdk.tcl" not in message


def test_a_file_that_cannot_be_read_is_refused_naming_it(tmp_path: Path) -> None:
    missing = tmp_path / "absent.tcl"
    assert "absent.tcl" in refusal(read_open_pdks_config, [missing])
    binary = tmp_path / "binary.tcl"
    binary.write_bytes(b"\xff\xfe\x00")
    assert "binary.tcl" in refusal(read_open_pdks_config, [binary])
    assert "absent.mk" in refusal(read_orfs_config_mk, tmp_path / "absent.mk", tmp_path)


# ---------------------------------------------------------------------------------------------
# config.tcl: the list of cells that DRC_EXCLUDE_CELL_LIST names
# ---------------------------------------------------------------------------------------------

EXCLUDE_KEY = (
    'set ::env(DRC_EXCLUDE_CELL_LIST) "$::env(PDK_ROOT)/$::env(PDK)/libs.tech/'
    '$::env(STD_CELL_LIBRARY)/drc_exclude.cells"\n'
)
CELLS = "# cells that fail the checks\ncell_a\n\n  cell_b  # trailing comment\nlib1__*_0\n"


def exclude_store(
    tmp_path: Path, config: str = EXCLUDE_KEY, cells: str | None = CELLS
) -> tuple[Path, Path, list[Path]]:
    """A store with the layout of an open_pdks build: `pdk/varA` is the platform directory, with
    a `config.tcl` and the list of cells. Return the directory, the config file and the files."""
    directory = tmp_path / "pdk" / "varA"
    folder = directory / "libs.tech" / "lib1"
    folder.mkdir(parents=True)
    tcl = write(folder, "config.tcl", config)
    listed = [tcl]
    if cells is not None:
        listed.append(write(folder, "drc_exclude.cells", cells))
    return directory, tcl, listed


def test_the_exclude_list_gives_the_dont_use_cells(tmp_path: Path) -> None:
    """`$::env(PDK_ROOT)` is the parent of the platform directory, `$::env(PDK)` its name and
    `$::env(STD_CELL_LIBRARY)` the library. Comments and blank lines are not cells."""
    directory, _, files = exclude_store(tmp_path)
    leaves = read_open_pdks_config(files, directory, "lib1")
    assert leaves == {"dont_use_cells": ["cell_a", "cell_b", "lib1__*_0"]}
    model = with_empty_residue(tmp_path, leaves)
    assert model.dont_use_cells == ["cell_a", "cell_b", "lib1__*_0"]
    assert model.sources["dont_use_cells"] == "upstream"


def test_a_repeated_cell_of_the_exclude_list_counts_once(tmp_path: Path) -> None:
    directory, _, files = exclude_store(tmp_path, cells="a\nb\na\n")
    assert read_open_pdks_config(files, directory, "lib1")["dont_use_cells"] == ["a", "b"]


def test_an_empty_exclude_list_gives_an_empty_leaf(tmp_path: Path) -> None:
    directory, _, files = exclude_store(tmp_path, cells="# nothing\n\n")
    assert read_open_pdks_config(files, directory, "lib1") == {"dont_use_cells": []}


def test_a_config_without_the_key_gives_no_dont_use_cells(tmp_path: Path) -> None:
    directory, _, files = exclude_store(tmp_path, config="set ::env(PLACE_SITE) core\n", cells=None)
    assert read_open_pdks_config(files, directory, "lib1") == {"site": "core"}


@pytest.mark.parametrize("variable", ["HOME", "PDK_ROOTS", "OTHER", "STD_CELL_LIBRARY_OPT"])
def test_the_exclude_list_refuses_a_variable_other_than_the_three(
    tmp_path: Path, variable: str
) -> None:
    config = f'set ::env(DRC_EXCLUDE_CELL_LIST) "$::env(PDK_ROOT)/$::env({variable})/x.cells"\n'
    directory, _, files = exclude_store(tmp_path, config=config, cells=None)
    message = refusal(read_open_pdks_config, files, directory, "lib1")
    assert "config.tcl:1" in message and variable in message


def test_the_exclude_list_refuses_a_variable_that_an_earlier_line_set(tmp_path: Path) -> None:
    """Another key of the reader may be set, but the path expands the three variables only."""
    config = (
        "set ::env(PLACE_SITE) core\n"
        'set ::env(DRC_EXCLUDE_CELL_LIST) "$::env(PDK_ROOT)/$::env(PDK)/$::env(PLACE_SITE).cells"\n'
    )
    directory, _, files = exclude_store(tmp_path, config=config, cells=None)
    message = refusal(read_open_pdks_config, files, directory, "lib1")
    assert "config.tcl:2" in message and "PLACE_SITE" in message


def test_the_library_is_known_only_when_the_provider_gives_it(tmp_path: Path) -> None:
    directory, _, files = exclude_store(tmp_path)
    message = refusal(read_open_pdks_config, files, directory)
    assert "config.tcl:1" in message and "STD_CELL_LIBRARY" in message


@pytest.mark.parametrize(
    "path", ["$::env(PDK_ROOT)/other/x.cells", "$::env(PDK_ROOT)/$::env(PDK)/../x.cells", "/etc/x"]
)
def test_the_exclude_list_must_be_inside_the_platform_directory(tmp_path: Path, path: str) -> None:
    config = f'set ::env(DRC_EXCLUDE_CELL_LIST) "{path}"\n'
    directory, _, files = exclude_store(tmp_path, config=config, cells=None)
    message = refusal(read_open_pdks_config, files, directory, "lib1")
    assert "config.tcl:1" in message and "inside the platform directory" in message


def test_the_exclude_list_needs_the_platform_directory(tmp_path: Path) -> None:
    _, _, files = exclude_store(tmp_path)
    assert "config.tcl:1" in refusal(read_open_pdks_config, files)


def test_the_exclude_list_must_be_among_the_files_that_the_residue_lists(tmp_path: Path) -> None:
    """The reader opens only listed files, so that the run records every file it read."""
    directory, tcl, _ = exclude_store(tmp_path)
    message = refusal(read_open_pdks_config, [tcl], directory, "lib1")
    assert "config.tcl:1" in message and "upstream.files" in message


def test_a_listed_exclude_list_that_is_missing_is_an_error(tmp_path: Path) -> None:
    directory, tcl, files = exclude_store(tmp_path)
    files[1].unlink()
    message = refusal(read_open_pdks_config, files, directory, "lib1")
    assert "drc_exclude.cells" in message and "cannot read" in message


def test_a_listed_file_that_no_key_names_is_refused(tmp_path: Path) -> None:
    directory, _, files = exclude_store(tmp_path, config="set ::env(PLACE_SITE) core\n")
    message = refusal(read_open_pdks_config, files, directory, "lib1")
    assert "drc_exclude.cells" in message and "DRC_EXCLUDE_CELL_LIST" in message


def test_a_line_of_the_exclude_list_with_two_words_names_its_line(tmp_path: Path) -> None:
    directory, _, files = exclude_store(tmp_path, cells="cell_a\ncell_b cell_c\n")
    message = refusal(read_open_pdks_config, files, directory, "lib1")
    assert "drc_exclude.cells:2" in message


def test_the_exclude_list_in_a_block_is_refused_as_any_key_is(tmp_path: Path) -> None:
    config = "if { 1 } {\n" + EXCLUDE_KEY + "}\n"
    directory, _, files = exclude_store(tmp_path, config=config, cells=None)
    assert "DRC_EXCLUDE_CELL_LIST" in refusal(read_open_pdks_config, files, directory, "lib1")


# ---------------------------------------------------------------------------------------------
# config.mk: every key of the set
# ---------------------------------------------------------------------------------------------

MK_ROWS: list[tuple[str, str, Any]] = [
    ("PLACE_SITE", "site", "TinySite"),
    ("TIEHI_CELL_AND_PORT", "tie_high", {"cell": "TINY_TIEHI", "port": "Z"}),
    ("TIELO_CELL_AND_PORT", "tie_low", {"cell": "TINY_TIELO", "port": "Z"}),
    (
        "MIN_BUF_CELL_AND_PORTS",
        "min_buffer",
        {"cell": "TINY_BUF_X1", "input": "A", "output": "Z"},
    ),
    ("ABC_DRIVER_CELL", "abc_driver_cell", "TINY_BUF_X2"),
    ("ABC_LOAD_IN_FF", "abc_load_in_ff", 3.5),
    ("DONT_USE_CELLS", "dont_use_cells", ["TINY_TAP_X1", "TINY_FILL_X1", "TINY_NAND_X1"]),
    ("FILL_CELLS", "fill_cells", ["TINY_FILL_X1", "TINY_FILL_X2"]),
    ("TAP_CELL_NAME", "tap_cell", "TINY_TAP_X1"),
    ("IO_PLACER_H", "io_layers.horizontal", "m5"),
    ("IO_PLACER_V", "io_layers.vertical", "m6"),
    ("MIN_ROUTING_LAYER", "routing.min_layer", "m2"),
    ("MIN_CLK_ROUTING_LAYER", "routing.clock_min_layer", "m4"),
    ("MAX_ROUTING_LAYER", "routing.max_layer", "m10"),
    ("PLACE_DENSITY", "place_density", 0.3),
    ("MATCH_CELL_FOOTPRINT", "match_cell_footprint", True),
    ("OPT_POST_GRT_WNS", "opt_post_grt_wns", 0.0),
    ("SYNTH_MINIMUM_KEEP_SIZE", "synth_minimum_keep_size", 10000),
    ("PWR_NETS_VOLTAGES", "supply.power_nets.VDD", 1.1),
    ("PWR_NETS_VOLTAGES", "supply.power_nets.VDD2", 0.9),
    ("GND_NETS_VOLTAGES", "supply.ground_nets.VSS", 0.0),
    ("IR_DROP_LAYER", "ir_drop_layer", "m1"),
    ("ROUTING_LAYER_ADJUSTMENT", "routing.layer_adjustment", 0.25),
]


@pytest.fixture(scope="module")
def tiny_mk() -> dict[str, Any]:
    return read_orfs_config_mk(TINY / "config.mk", Path("/pdk/tiny"))


@pytest.mark.parametrize(("key", "leaf", "expected"), MK_ROWS, ids=[row[1] for row in MK_ROWS])
def test_config_mk_gives_the_leaf_of_each_key(
    tiny_mk: dict[str, Any], key: str, leaf: str, expected: Any
) -> None:
    assert tiny_mk[leaf] == expected, key


def test_config_mk_gives_exactly_the_leaves_of_its_keys(tiny_mk: dict[str, Any]) -> None:
    """The other keys, file names among them, give nothing."""
    assert set(tiny_mk) == {leaf for _, leaf, _ in MK_ROWS}


def test_the_types_of_config_mk_are_exact(tiny_mk: dict[str, Any]) -> None:
    assert isinstance(tiny_mk["synth_minimum_keep_size"], int)
    assert tiny_mk["match_cell_footprint"] is True
    assert isinstance(tiny_mk["place_density"], float)
    assert isinstance(tiny_mk["opt_post_grt_wns"], float)
    assert isinstance(tiny_mk["supply.ground_nets.VSS"], float)


def test_every_leaf_of_config_mk_validates_as_its_type(
    tiny_mk: dict[str, Any], tmp_path: Path
) -> None:
    for key, value in tiny_mk.items():
        normalize_leaf(key, value)
    model = with_empty_residue(tmp_path, tiny_mk)
    assert model.site == "TinySite" and model.match_cell_footprint is True
    assert model.supply.power_nets == {"VDD": 1.1, "VDD2": 0.9}
    assert model.min_buffer.cell == "TINY_BUF_X1" and model.synth_minimum_keep_size == 10000
    assert all(model.sources[key] == "upstream" for key in tiny_mk)


# ---------------------------------------------------------------------------------------------
# config.mk: the forms of a line
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "export PLACE_SITE = Site",
        "export PLACE_SITE=Site",
        "export PLACE_SITE ?= Site",
        "export PLACE_SITE := Site",
        "PLACE_SITE = Site",
        "PLACE_SITE\t?=\tSite",
        "export  PLACE_SITE   =   Site   ",
        "export PLACE_SITE = Site # a comment",
    ],
)
def test_an_assignment_takes_the_forms_of_make(tmp_path: Path, line: str) -> None:
    assert read_mk(tmp_path, line + "\n") == {"site": "Site"}


def test_a_comment_gives_nothing_and_a_hash_ends_a_value(tmp_path: Path) -> None:
    text = "# export PLACE_SITE = a\n  # export TAP_CELL_NAME = b\nexport IR_DROP_LAYER = m1 # c\n"
    assert read_mk(tmp_path, text) == {"ir_drop_layer": "m1"}


def test_a_backslash_continues_a_line_and_a_comment_after_it_goes_on(tmp_path: Path) -> None:
    text = (
        "export DONT_USE_CELLS = A \\\n   B \\\n   C\n"
        "export FILL_CELLS = F1 \\\n  F2 # note \\\n  F3\nexport PLACE_SITE = S\n"
    )
    assert read_mk(tmp_path, text) == {
        "dont_use_cells": ["A", "B", "C"],
        "fill_cells": ["F1", "F2"],
        "site": "S",
    }


def test_a_plus_equal_appends_with_a_space(tmp_path: Path) -> None:
    text = "export DONT_USE_CELLS = A B\nexport DONT_USE_CELLS += C\nDONT_USE_CELLS += D E\n"
    assert read_mk(tmp_path, text) == {"dont_use_cells": ["A", "B", "C", "D", "E"]}
    assert read_mk(tmp_path, "export DONT_USE_CELLS += C\n") == {"dont_use_cells": ["C"]}
    assert read_mk(tmp_path, "export DONT_USE_CELLS =\nexport DONT_USE_CELLS += C\n") == {
        "dont_use_cells": ["C"]
    }


def test_a_question_equal_sets_a_key_only_when_it_has_no_value(tmp_path: Path) -> None:
    assert read_mk(tmp_path, "PLACE_SITE = a\nPLACE_SITE ?= b\n") == {"site": "a"}
    assert read_mk(tmp_path, "PLACE_SITE ?= a\nPLACE_SITE = b\n") == {"site": "b"}
    assert read_mk(tmp_path, "PLACE_SITE ?= a\nPLACE_SITE ?= b\n") == {"site": "a"}
    assert read_mk(tmp_path, "PLACE_SITE := a\nPLACE_SITE := b\n") == {"site": "b"}


def test_platform_dir_expands_and_additional_variables_expand_to_nothing(tmp_path: Path) -> None:
    text = "export IR_DROP_LAYER = $(PLATFORM_DIR)/x $(ADDITIONAL_FILES) $(ADDITIONAL_)\n"
    assert read_mk(tmp_path, text, Path("/p/q")) == {"ir_drop_layer": "/p/q/x"}
    assert read_mk(tmp_path, "export IR_DROP_LAYER = $(PLATFORM_DIR)\n", Path("/p/q")) == {
        "ir_drop_layer": "/p/q"
    }
    assert read_mk(tmp_path, "export DONT_USE_CELLS = A $(ADDITIONAL_CELLS) B\n") == {
        "dont_use_cells": ["A", "B"]
    }


def test_a_value_with_double_quotes_around_all_of_it_loses_them(tmp_path: Path) -> None:
    assert read_mk(tmp_path, 'export PWR_NETS_VOLTAGES = "VDD 1.8 VDD2 1.2"\n') == {
        "supply.power_nets.VDD": 1.8,
        "supply.power_nets.VDD2": 1.2,
    }
    assert read_mk(tmp_path, 'export TIEHI_CELL_AND_PORT = "TIE Z"\n') == {
        "tie_high": {"cell": "TIE", "port": "Z"}
    }


def test_an_empty_value_is_an_empty_list_or_no_leaf(tmp_path: Path) -> None:
    text = "export DONT_USE_CELLS =\nexport PLACE_SITE =\nexport PLACE_DENSITY =\n"
    assert read_mk(tmp_path, text) == {"dont_use_cells": []}
    assert read_mk(tmp_path, "PLACE_SITE = a\nPLACE_SITE =\n") == {}


def test_a_boolean_is_one_or_zero(tmp_path: Path) -> None:
    assert read_mk(tmp_path, "MATCH_CELL_FOOTPRINT = 0\n") == {"match_cell_footprint": False}
    assert read_mk(tmp_path, "MATCH_CELL_FOOTPRINT = 1\n") == {"match_cell_footprint": True}


def test_a_number_may_be_an_integer_a_decimal_or_have_an_exponent(tmp_path: Path) -> None:
    for text, value in [("5", 5.0), ("0.01343", 0.01343), ("1.5e-3", 1.5e-3), (".5", 0.5)]:
        assert read_mk(tmp_path, f"ABC_LOAD_IN_FF = {text}\n") == {"abc_load_in_ff": value}


def test_a_line_of_another_key_is_ignored_whatever_it_holds(tmp_path: Path) -> None:
    text = (
        "export OTHER = $(MYSTERY) $$HOME $(shell date)\n"
        "export GDS_FILES = $(sort $(wildcard $(PLATFORM_DIR)/gds/*.gds)) \\\n   $(ADDITIONAL_GDS)\n"
        "export LIB_FILES = $(PLATFORM_DIR)/lib/a.lib\n"
        "export CDL_FILE = $(PLATFORM_DIR)/cdl/a.cdl\n"
        "export KLAYOUT_TECH_FILE = $(PLATFORM_DIR)/a.lyt\n"
        "export LATCH_MAP_FILE = $(PLATFORM_DIR)/cells_latch.v\n"
        "export PDN_TCL ?= $(PLATFORM_DIR)/grid.tcl\n"
        "export RCX_RULES = $(PLATFORM_DIR)/rcx.rules\n"
        "all: other\n\t@echo $$HOME\n"
        "export\nunexport OTHER\n"
    )
    assert read_mk(tmp_path, text) == {}


# ---------------------------------------------------------------------------------------------
# config.mk: the refusals
# ---------------------------------------------------------------------------------------------

NEEDS_MAKE = "needs Make: give these values in the residue file"


@pytest.mark.parametrize(
    "directive",
    [
        "ifeq ($(A),1)",
        "ifeq ($(A),1) ",
        "ifneq ($(A),)",
        "ifdef A",
        "ifndef A",
        "  ifeq ($(A),1)",
        "ifeq($(A),1)",
        "else",
        "else ifeq ($(A),2)",
        "endif",
    ],
)
def test_a_conditional_refuses_the_whole_file(tmp_path: Path, directive: str) -> None:
    text = f"export PLACE_SITE = A\n{directive}\nexport IR_DROP_LAYER = m1\n"
    message = refusal(read_orfs_config_mk, write(tmp_path, "config.mk", text), tmp_path)
    assert message.endswith(NEEDS_MAKE), message
    assert "config.mk:2" in message


def test_a_conditional_anywhere_wins_over_an_error_on_an_earlier_line(tmp_path: Path) -> None:
    """The file needs Make as a whole: say that, not the first line the reader cannot read."""
    text = "export PLACE_SITE = $(MYSTERY)\n\nifeq ($(A),1)\nexport PLACE_SITE = A\nendif\n"
    message = refusal(read_orfs_config_mk, write(tmp_path, "config.mk", text), tmp_path)
    assert message.endswith(NEEDS_MAKE), message
    assert "config.mk:3" in message


@pytest.mark.parametrize(
    "directive", ["include x.mk", "-include x.mk", "sinclude x.mk", "define A", "endef"]
)
def test_include_and_define_refuse_the_whole_file(tmp_path: Path, directive: str) -> None:
    text = f"export PLACE_SITE = A\n{directive}\n"
    message = refusal(read_orfs_config_mk, write(tmp_path, "config.mk", text), tmp_path)
    assert message.endswith(NEEDS_MAKE) and "config.mk:2" in message, message


def test_a_conditional_in_a_comment_or_a_value_is_no_conditional(tmp_path: Path) -> None:
    text = "# ifeq ($(A),1)\nexport PLACE_SITE = ifeq\nexport IR_DROP_LAYER = m1 # endif\n"
    assert read_mk(tmp_path, text) == {"site": "ifeq", "ir_drop_layer": "m1"}


MK_REFUSALS: list[tuple[str, int, str]] = [
    ("export PLACE_SITE = $(MYSTERY)\n", 1, "$(MYSTERY)"),
    ("# a\nexport TAP_CELL_NAME = $(VT)_tap\n", 2, "$(VT)"),
    ("export DONT_USE_CELLS = A $(sort B C)\n", 1, "$(sort"),
    ("export PLACE_SITE = $$HOME\n", 1, "$$"),
    ("export PLACE_SITE = ${PLATFORM_DIR}\n", 1, "${"),
    ("export PLACE_SITE = $X\n", 1, "$X"),
    ("export PLACE_SITE = a$\n", 1, "$"),
    ("export PWR_NETS_VOLTAGES ?= VDD $(VOLTAGE)\n", 1, "$(VOLTAGE)"),
    ("export PLACE_DENSITY = uniform\n", 1, "number"),
    ("export PLACE_DENSITY = 0.3 0.4\n", 1, "number"),
    ("export PLACE_DENSITY = 1e\n", 1, "number"),
    ("export ABC_LOAD_IN_FF = three\n", 1, "number"),
    ("export OPT_POST_GRT_WNS = -\n", 1, "number"),
    ("export ROUTING_LAYER_ADJUSTMENT = 25%\n", 1, "number"),
    ("export SYNTH_MINIMUM_KEEP_SIZE = 10.5\n", 1, "integer"),
    ("export SYNTH_MINIMUM_KEEP_SIZE = many\n", 1, "integer"),
    ("export MATCH_CELL_FOOTPRINT = yes\n", 1, "1 or 0"),
    ("export MATCH_CELL_FOOTPRINT = 2\n", 1, "1 or 0"),
    ("export TIEHI_CELL_AND_PORT = CELL\n", 1, "cell port"),
    ("export TIELO_CELL_AND_PORT = A B C\n", 1, "cell port"),
    ("export MIN_BUF_CELL_AND_PORTS = BUF A\n", 1, "cell in out"),
    ("export MIN_BUF_CELL_AND_PORTS = BUF A Z Y\n", 1, "cell in out"),
    ("export PLACE_SITE = A B\n", 1, "one word"),
    ("export IO_PLACER_H = m1 m2\n", 1, "one word"),
    ("export ABC_DRIVER_CELL = A B\n", 1, "one word"),
    ("export PWR_NETS_VOLTAGES = VDD\n", 1, "pairs"),
    ("export PWR_NETS_VOLTAGES = VDD 1.1 VDD2\n", 1, "pairs"),
    ("export GND_NETS_VOLTAGES = VSS low\n", 1, "number"),
    ("export GND_NETS_VOLTAGES = a.b 0\n", 1, "."),
    ("export PWR_NETS_VOLTAGES = A 1 A 2\n", 1, "twice"),
    ('export PLACE_SITE = a"b\n', 1, "quote"),
    ("export FILL_CELLS = A 'B'\n", 1, "quote"),
]


@pytest.mark.parametrize(
    ("text", "line", "fragment"),
    MK_REFUSALS,
    ids=[f"{index:02}" for index in range(len(MK_REFUSALS))],
)
def test_config_mk_refuses_what_it_does_not_understand_naming_the_line(
    tmp_path: Path, text: str, line: int, fragment: str
) -> None:
    message = refusal(read_orfs_config_mk, write(tmp_path, "config.mk", text), tmp_path)
    assert re.search(rf"config\.mk:{line}\b", message), message
    assert fragment in message, message


MK_UNSUPPORTED_FORMS = [
    "ABC_DRIVER_CELL != printf BUF_X1",
    "export ABC_DRIVER_CELL != printf BUF_X1",
    "ABC_DRIVER_CELL ::= BUF_X1",
    "ABC_DRIVER_CELL :::= BUF_X1",
    "override ABC_DRIVER_CELL = BUF_X1",
    "export override ABC_DRIVER_CELL = BUF_X1",
    "override export ABC_DRIVER_CELL = BUF_X1",
    "private ABC_DRIVER_CELL = BUF_X1",
    "unexport ABC_DRIVER_CELL",
    "export ABC_DRIVER_CELL",
    "ABC_DRIVER_CELL",
    "ABC_DRIVER_CELL: BUF_X1",
    "ABC_DRIVER_CELL : dependency",
    "ABC_DRIVER_CELL BUF_X1",
]


@pytest.mark.parametrize("line", MK_UNSUPPORTED_FORMS)
def test_a_key_of_the_reader_in_a_form_it_does_not_take_is_refused(
    tmp_path: Path, line: str
) -> None:
    """Make would give the key another value than the reader, so the reader must not skip it."""
    text = f"ABC_DRIVER_CELL = INV_X1\n{line}\n"
    message = refusal(read_orfs_config_mk, write(tmp_path, "config.mk", text), tmp_path)
    assert "config.mk:2" in message and "ABC_DRIVER_CELL" in message, message


MK_OPERATORS = ["=", ":=", "::=", ":::=", "?=", "+=", "!="]
MK_SCOPES = [
    "synth:",
    "%.v:",
    "synth floorplan:",
    "synth: export",
    "synth: override",
    "synth: private",
    "%.v: export override",
]


@pytest.mark.parametrize("operator", MK_OPERATORS)
@pytest.mark.parametrize("scope", MK_SCOPES)
def test_a_target_or_pattern_specific_assignment_to_a_key_is_refused(
    tmp_path: Path, scope: str, operator: str
) -> None:
    """Make gives the key that value for the target, so the reader must not keep the plain one."""
    text = f"ABC_DRIVER_CELL = INV_X1\n{scope} ABC_DRIVER_CELL {operator} BUF_X1\n"
    message = refusal(read_orfs_config_mk, write(tmp_path, "config.mk", text), tmp_path)
    assert "config.mk:2" in message and "ABC_DRIVER_CELL" in message, message


def test_an_assignment_to_a_key_without_spaces_or_in_a_value_is_refused(tmp_path: Path) -> None:
    for line in (
        "synth: ABC_DRIVER_CELL=BUF_X1",
        "synth: PLACE_SITE?=A",
        "target:PLACE_SITE:=A",
        "OTHER = $(call set,PLACE_SITE=A)",
    ):
        message = refusal(read_orfs_config_mk, write(tmp_path, "config.mk", line + "\n"), tmp_path)
        assert "config.mk:1" in message, message


def test_a_name_that_only_contains_a_key_is_another_name(tmp_path: Path) -> None:
    text = (
        "synth: ABC_DRIVER_CELLS = x\nsynth: MY_ABC_DRIVER_CELL := x\n"
        "OTHER = -DABC_DRIVER_CELL=1 --ABC_DRIVER_CELL=2 ABC_DRIVER_CELL.x=3\n"
        "synth: ABC_DRIVER_CELL_2 += y\nABC_DRIVER_CELL = INV_X1 # ABC_DRIVER_CELL = x\n"
    )
    assert read_mk(tmp_path, text) == {"abc_driver_cell": "INV_X1"}


def test_a_name_that_only_starts_like_a_key_is_another_key(tmp_path: Path) -> None:
    text = (
        "ABC_DRIVER_CELLS != x\nABC_DRIVER_CELL_2 ::= x\nABC_DRIVER_CELL.x := y\n"
        "PLACE_SITE_X override = y\nABC_DRIVER_CELL = INV_X1\n"
    )
    assert read_mk(tmp_path, text) == {"abc_driver_cell": "INV_X1"}


def test_an_error_after_a_continuation_names_the_first_line_of_the_assignment(
    tmp_path: Path,
) -> None:
    text = "# a\nexport DONT_USE_CELLS = A \\\n   $(BAD)\n"
    assert "config.mk:2" in refusal(
        read_orfs_config_mk, write(tmp_path, "config.mk", text), tmp_path
    )


def test_an_error_in_a_value_that_a_later_line_replaces_still_names_its_line(
    tmp_path: Path,
) -> None:
    text = "export PLACE_SITE = $(BAD)\nexport PLACE_SITE = good\n"
    message = refusal(read_orfs_config_mk, write(tmp_path, "config.mk", text), tmp_path)
    assert "config.mk:1" in message


# ---------------------------------------------------------------------------------------------
# The reference file
# ---------------------------------------------------------------------------------------------


def test_nangate45_config_mk_gives_the_values_that_the_platform_needs() -> None:
    """Reference data on this machine (an OpenROAD-flow-scripts checkout): never copied."""
    if not NANGATE45_CONFIG.is_file():
        pytest.skip("no OpenROAD-flow-scripts checkout at the reference location")
    leaves = read_orfs_config_mk(NANGATE45_CONFIG, NANGATE45_CONFIG.parent)
    assert leaves == {
        "site": "FreePDK45_38x28_10R_NP_162NW_34O",
        "tie_high": {"cell": "LOGIC1_X1", "port": "Z"},
        "tie_low": {"cell": "LOGIC0_X1", "port": "Z"},
        "min_buffer": {"cell": "BUF_X1", "input": "A", "output": "Z"},
        "abc_driver_cell": "BUF_X1",
        "abc_load_in_ff": 3.898,
        "dont_use_cells": ["TAPCELL_X1", "FILLCELL_X1", "AOI211_X1", "OAI211_X1"],
        "fill_cells": [
            "FILLCELL_X1",
            "FILLCELL_X2",
            "FILLCELL_X4",
            "FILLCELL_X8",
            "FILLCELL_X16",
            "FILLCELL_X32",
        ],
        "tap_cell": "TAPCELL_X1",
        "io_layers.horizontal": "metal5",
        "io_layers.vertical": "metal6",
        "routing.min_layer": "metal2",
        "routing.clock_min_layer": "metal4",
        "routing.max_layer": "metal10",
        "place_density": 0.3,
        "opt_post_grt_wns": 0.0,
        "synth_minimum_keep_size": 10000,
        "supply.power_nets.VDD": 1.1,
        "supply.ground_nets.VSS": 0.0,
        "ir_drop_layer": "metal1",
    }


@pytest.mark.parametrize("platform", ["nangate45", "sky130hd", "sky130hs"])
def test_the_config_mk_of_the_plain_platforms_still_reads(platform: str) -> None:
    """Reference data on this machine: the guard against other forms keeps these files readable."""
    directory = NANGATE45_CONFIG.parent.parent / platform
    if not (directory / "config.mk").is_file():
        pytest.skip("no OpenROAD-flow-scripts checkout at the reference location")
    leaves = read_orfs_config_mk(directory / "config.mk", directory)
    assert leaves["site"] and leaves["tie_high"]["cell"] and leaves["min_buffer"]["cell"]


# ---------------------------------------------------------------------------------------------
# The readers evaluate nothing
# ---------------------------------------------------------------------------------------------


def test_the_module_starts_no_process_and_loads_no_tcl_interpreter() -> None:
    source = (Path(__file__).parent.parent / "src/xeda/platforms/upstream.py").read_text()
    for word in ("tkinter", "subprocess", "os.system", "Popen", "eval(", "exec("):
        assert word not in source, word


def test_a_reader_does_not_write_or_change_its_files(tmp_path: Path) -> None:
    tcl = write(tmp_path, "config.tcl", "set ::env(PLACE_SITE) a\n")
    mk = write(tmp_path, "config.mk", "PLACE_SITE = a\n")
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (tcl, mk)}
    read_open_pdks_config([tcl])
    read_orfs_config_mk(mk, tmp_path)
    assert before == {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (tcl, mk)}
    assert sorted(path.name for path in tmp_path.iterdir()) == ["config.mk", "config.tcl"]
