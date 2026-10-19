"""Derivation of platform facts from the LEF and Liberty files of a PDK.

The facts are the core site, the tie cells, the antenna cell, the supply pins and voltage, the
cells that Liberty marks `dont_use`, the routing tracks and the units. The derivation is pure
Python: it reads each file once, writes nothing and starts no process.
"""

import gzip
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import pytest

from xeda.platforms import Located, Residue, build_platform
from xeda.platforms.derive import derive, read_lef_summary, read_liberty_summary
from xeda.platforms.model import normalize_leaf
from .tool_utils import require_yosys

TINY = Path(__file__).parent / "resources" / "platforms" / "tiny"
NANGATE45 = Path(
    "/Volumes/src/opensource-asics/OpenROAD-flow-scripts/flow/platforms/nangate45/lef/"
    "NangateOpenCellLibrary.tech.lef"
)

FILES = {
    "tech_lef": [TINY / "tiny.tlef"],
    "cell_lef": [TINY / "tiny.lef"],
    "liberty": [TINY / "tiny.lib"],
}


def library(*cells: str, header: str = "") -> str:
    """The text of a small Liberty file with these cells (each the text of a `cell` group)."""
    return "library (t) {\n" + header + "".join(cells) + "}\n"


def tie(name: str, area: float, function: str = "1", pin: str = "Z") -> str:
    return (
        f"cell ({name}) {{ area : {area}; pin ({pin}) {{ direction : output; "
        f'function : "{function}"; }} }}\n'
    )


def macro(
    name: str,
    site: Optional[str] = "s",
    pins: str = "",
    cls: str = "CORE",
    size: str = "SIZE 1 BY 1 ;",
) -> str:
    site_line = f"SITE {site} ;" if site else ""
    return f"MACRO {name}\nCLASS {cls} ;\n{size}\n{site_line}\n{pins}END {name}\n"


def pin(name: str, use: str = "SIGNAL", direction: str = "INPUT", extra: str = "") -> str:
    return f"PIN {name}\nDIRECTION {direction} ;\nUSE {use};\n{extra}END {name}\n"


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


def derive_text(tmp_path: Path, lef: str = "", lib: str = "") -> dict[str, Any]:
    files: dict[str, list[Path]] = {}
    if lef:
        files["cell_lef"] = [write(tmp_path, "cells.lef", lef)]
    if lib:
        files["liberty"] = [write(tmp_path, "cells.lib", lib)]
    return derive(files, "typical")


# ---------------------------------------------------------------------------------------------
# The fixtures
# ---------------------------------------------------------------------------------------------


def test_fixture_facts() -> None:
    assert derive(FILES, "typical") == {
        "time_unit": "1ns",
        "capacitance_unit": "1ff",
        "site": "tinysite",
        "tie_high": {"cell": "TIEHI_SMALL", "port": "Z"},
        "tie_low": {"cell": "TIELO_X1", "port": "Z"},
        "antenna_cell": {"cell": "ANTENNA_X1", "port": "I"},
        "diode_usable": True,
        "supply.power_pin": "VDD",
        "supply.ground_pin": "VSS",
        "supply.voltage": 1.1,
        "dont_use_cells": ["INV_X4", "NAND2_X4"],
        "tracks.m1": {"x": [0.1, 0.2], "y": [0.05, 0.2]},
        "tracks.m2": {"x": [0.1, 0.2], "y": [0.15, 0.3]},
        "tracks.m3": {"x": [0.2, 0.4], "y": [0.2, 0.4]},
    }


def test_fixture_values_validate_as_the_leaves_of_the_model(tmp_path: Path) -> None:
    derived = derive(FILES, "typical")
    for key, value in derived.items():
        assert normalize_leaf(key, value) is not None
    residue = Residue.model_validate(
        {
            "name": "tiny",
            "providers": {"default": "directory", "directory": {"layout": "orfs"}},
            "corners": {"default": "typical"},
            "files": {
                "liberty": {"typical": ["tiny.lib"]},
                "tech_lef": "tiny.tlef",
                "cell_lef": ["tiny.lef"],
            },
        }
    )
    located = Located(
        provider="directory",
        version="unpinned",
        root=TINY.parent,
        directory=TINY,
        corner="typical",
        files=FILES,
        upstream_files=[],
    )
    model = build_platform(located, residue, None, derived, {})
    assert model.site == "tinysite"
    assert model.tie_high is not None and model.tie_high.cell == "TIEHI_SMALL"
    assert model.supply.voltage == 1.1
    assert model.tracks["m2"].y == (0.15, 0.3)
    assert model.dont_use_cells == ["INV_X4", "NAND2_X4"]
    assert {model.sources[key] for key in derived} == {"derived"}


def test_lef_summary_of_the_fixtures() -> None:
    summary = read_lef_summary([*FILES["tech_lef"], *FILES["cell_lef"]])
    assert summary.sites == {"tinysite": 12, "bigsite": 1}
    assert summary.macros["TALL_X1"].site == "bigsite"
    assert summary.macros["TALL_X1"].size == (0.4, 2.8)
    assert summary.macros["ANTENNA_X1"].cls == ("CORE", "ANTENNACELL")
    antenna_pin = summary.macros["ANTENNA_X1"].pins["I"]
    assert (antenna_pin.direction, antenna_pin.use, antenna_pin.antenna_diff_area) == (
        "INPUT",
        "SIGNAL",
        0.25,
    )
    assert summary.macros["INV_X1"].pins["VDD"].use == "POWER"
    assert summary.macros["INV_X1"].pins["A"].antenna_diff_area is None
    assert [layer.name for layer in summary.layers] == ["m1", "m2", "m3", "m4"]
    m2 = summary.layers[1]
    assert (m2.direction, m2.pitch, m2.offset) == ("VERTICAL", (0.2, 0.3), (0.1, 0.15))
    assert summary.layers[3].offset is None


def test_liberty_summary_of_the_fixtures() -> None:
    summary = read_liberty_summary(TINY / "tiny.lib")
    assert summary.nom_voltage == 1.1
    assert (summary.time_unit, summary.capacitive_load_unit) == ("1ns", "1ff")
    assert list(summary.cells)[:3] == ["INV_X1", "INV_X4", "BUF_X1"]
    dff = summary.cells["DFF_X1"]
    assert dff.area == pytest.approx(12 * 0.2 * 1.4)
    assert [(p.name, p.direction, p.function) for p in dff.pins] == [
        ("D", "input", None),
        ("CK", "input", None),
        ("Q", "output", "IQ"),
    ]
    assert [name for name, cell in summary.cells.items() if cell.dont_use] == [
        "INV_X4",
        "NAND2_X4",
    ]


# ---------------------------------------------------------------------------------------------
# One rule per test
# ---------------------------------------------------------------------------------------------


def test_site_is_the_one_of_most_macros(tmp_path: Path) -> None:
    lef = macro("A", "s2") + macro("B", "s1") + macro("C", "s2") + macro("D", None)
    assert derive_text(tmp_path, lef)["site"] == "s2"


def test_site_tie_goes_to_the_name_that_sorts_first(tmp_path: Path) -> None:
    lef = macro("A", "zeta") + macro("B", "alpha") + macro("C", "zeta") + macro("D", "alpha")
    assert derive_text(tmp_path, lef)["site"] == "alpha"


def test_no_macro_with_a_site_gives_no_site(tmp_path: Path) -> None:
    assert "site" not in derive_text(tmp_path, macro("A", None))


def test_tie_cells_are_cells_whose_pins_are_all_outputs(tmp_path: Path) -> None:
    inverter = (
        "cell (INV) { area : 0.1; pin (A) { direction : input; } pin (Y) { direction : output; "
        'function : "1"; } }\n'
    )
    derived = derive_text(tmp_path, lib=library(inverter, tie("TIEH", 2.0), tie("TIEL", 2.0, "0")))
    assert derived["tie_high"] == {"cell": "TIEH", "port": "Z"}
    assert derived["tie_low"] == {"cell": "TIEL", "port": "Z"}


def test_tie_cell_not_found_gives_no_key(tmp_path: Path) -> None:
    derived = derive_text(tmp_path, lib=library(tie("TIEH", 1.0)))
    assert "tie_low" not in derived
    assert "tie_high" in derived


def test_tie_tie_goes_to_the_smallest_area_then_the_name(tmp_path: Path) -> None:
    lib = library(tie("TIEA", 3.0), tie("TIEC", 2.0), tie("TIEB", 2.0), tie("TIED", 1.0, "0"))
    derived = derive_text(tmp_path, lib=lib)
    assert derived["tie_high"]["cell"] == "TIEB"
    lib = library(tie("TIEB", 2.0), tie("TIEA", 2.0), tie("TIEL", 2.0, "0"), tie("TIEK", 2.0, "0"))
    derived = derive_text(tmp_path, lib=lib)
    assert (derived["tie_high"]["cell"], derived["tie_low"]["cell"]) == ("TIEA", "TIEK")


def test_a_cell_with_both_constants_serves_both(tmp_path: Path) -> None:
    both = (
        'cell (conb_1) { area : 3.75; pin (HI) { direction : output; function : "1"; } '
        'pin (LO) { direction : output; function : "0"; } }\n'
    )
    derived = derive_text(tmp_path, lib=library(both, tie("TIEL", 1.0, "0", "ZN")))
    assert derived["tie_high"] == {"cell": "conb_1", "port": "HI"}
    assert derived["tie_low"] == {"cell": "TIEL", "port": "ZN"}
    derived = derive_text(tmp_path, lib=library(both))
    assert derived["tie_low"] == {"cell": "conb_1", "port": "LO"}


def test_antenna_cell_is_the_macro_of_class_antennacell(tmp_path: Path) -> None:
    lef = macro("NOT_ANTENNA", pins=pin("A", extra="ANTENNADIFFAREA 9 ;\n")) + macro(
        "DIODE",
        cls="CORE ANTENNACELL",
        pins=pin("VPWR", "POWER", "INOUT") + pin("DIODE", extra="ANTENNADIFFAREA 0.43 ;\n"),
    )
    derived = derive_text(tmp_path, lef)
    assert derived["antenna_cell"] == {"cell": "DIODE", "port": "DIODE"}
    assert derived["diode_usable"] is True


def test_antenna_port_is_the_only_signal_pin_without_a_diffusion_area(tmp_path: Path) -> None:
    lef = macro(
        "DIODE",
        cls="CORE ANTENNACELL",
        pins=pin("VDD", "POWER", "INOUT") + pin("VSS", "GROUND", "INOUT") + pin("A"),
    )
    derived = derive_text(tmp_path, lef)
    assert derived["antenna_cell"] == {"cell": "DIODE", "port": "A"}
    assert "diode_usable" not in derived  # the LEF states no area


def test_diode_with_zero_diffusion_area_is_not_usable(tmp_path: Path) -> None:
    lef = macro("ANTENNA", cls="CORE ANTENNACELL", pins=pin("A", extra="ANTENNADIFFAREA 0.0 ;\n"))
    derived = derive_text(tmp_path, lef)
    assert derived["antenna_cell"] == {"cell": "ANTENNA", "port": "A"}
    assert derived["diode_usable"] is False


def test_several_antenna_cells_go_to_the_smallest_area_then_the_name(tmp_path: Path) -> None:
    def diode(name: str, size: str) -> str:
        return macro(
            name,
            cls="CORE ANTENNACELL",
            size=size,
            pins=pin("A", extra="ANTENNADIFFAREA 1 ;\n"),
        )

    lef = (
        diode("D_C", "SIZE 2 BY 1 ;")
        + diode("D_B", "SIZE 1 BY 1 ;")
        + diode("D_A", "SIZE 1 BY 1 ;")
    )
    assert derive_text(tmp_path, lef)["antenna_cell"]["cell"] == "D_A"
    lef = diode("D_A", "SIZE 3 BY 1 ;") + diode("D_Z", "SIZE 1 BY 1 ;")
    assert derive_text(tmp_path, lef)["antenna_cell"]["cell"] == "D_Z"


def test_antenna_macro_with_an_unclear_port_gives_no_antenna_cell(tmp_path: Path) -> None:
    lef = macro("ANT", cls="CORE ANTENNACELL", pins=pin("A") + pin("B"))
    derived = derive_text(tmp_path, lef)
    assert "antenna_cell" not in derived
    assert "diode_usable" not in derived


def test_supply_pins_are_the_ones_most_macros_have(tmp_path: Path) -> None:
    power = pin("VPWR", "POWER", "INOUT")
    ground = pin("VGND", "GROUND", "INOUT")
    odd = pin("VDDX", "POWER", "INOUT") + pin("VSSX", "GROUND", "INOUT")
    lef = macro("A", pins=power + ground) + macro("B", pins=power + ground) + macro("C", pins=odd)
    derived = derive_text(tmp_path, lef)
    assert (derived["supply.power_pin"], derived["supply.ground_pin"]) == ("VPWR", "VGND")


def test_supply_pin_tie_goes_to_the_name_that_sorts_first(tmp_path: Path) -> None:
    lef = macro("A", pins=pin("VPWR", "POWER", "INOUT") + pin("VDD", "POWER", "INOUT")) + macro(
        "B", pins=pin("VSS", "GROUND", "INOUT") + pin("GND", "GROUND", "INOUT")
    )
    derived = derive_text(tmp_path, lef)
    assert (derived["supply.power_pin"], derived["supply.ground_pin"]) == ("VDD", "GND")


def test_supply_voltage_is_the_nominal_voltage_of_the_library(tmp_path: Path) -> None:
    derived = derive_text(tmp_path, lib=library(header="nom_voltage : 1.80;\n"))
    assert derived["supply.voltage"] == 1.8


def test_library_without_nominal_voltage_gives_no_voltage(tmp_path: Path) -> None:
    derived = derive_text(tmp_path, lib=library(tie("TIEH", 1.0), header='time_unit : "1ps";\n'))
    assert "supply.voltage" not in derived
    assert derived["time_unit"] == "1ps"


def test_nominal_voltage_of_a_nested_group_does_not_count(tmp_path: Path) -> None:
    header = "operating_conditions (c) { nom_voltage : 9; }\n"
    assert "supply.voltage" not in derive_text(tmp_path, lib=library(header=header))


def test_dont_use_cells_come_in_file_order(tmp_path: Path) -> None:
    cells = (
        "cell (Z) { dont_use : true; area : 1; }\n"
        "cell (M) { dont_use : false; area : 1; }\n"
        "cell (A) { area : 1; dont_use : true; }\n"
        "cell (K) { area : 1; }\n"
    )
    assert derive_text(tmp_path, lib=library(cells))["dont_use_cells"] == ["Z", "A"]


def test_no_dont_use_cell_gives_no_key(tmp_path: Path) -> None:
    assert "dont_use_cells" not in derive_text(tmp_path, lib=library("cell (A) { area : 1; }\n"))


def test_tracks_with_one_and_two_values(tmp_path: Path) -> None:
    lef = (
        "LAYER a\nTYPE ROUTING ;\nDIRECTION HORIZONTAL ;\nPITCH 0.2 ;\nOFFSET 0.1 ;\nEND a\n"
        "LAYER b\nTYPE ROUTING ;\nPITCH 0.2 0.4 ;\nOFFSET 0.1 0.3 ;\nEND b\n"
    )
    derived = derive_text(tmp_path, lef)
    assert derived["tracks.a"] == {"x": [0.1, 0.2], "y": [0.1, 0.2]}
    assert derived["tracks.b"] == {"x": [0.1, 0.2], "y": [0.3, 0.4]}


def test_a_layer_without_offset_or_without_pitch_gives_no_track(tmp_path: Path) -> None:
    lef = (
        "LAYER a\nTYPE ROUTING ;\nPITCH 0.2 ;\nEND a\n"
        "LAYER b\nTYPE ROUTING ;\nOFFSET 0.2 ;\nEND b\n"
        "LAYER c\nTYPE CUT ;\nPITCH 0.2 ;\nOFFSET 0.1 ;\nEND c\n"
    )
    derived = derive_text(tmp_path, lef)
    assert not [key for key in derived if key.startswith("tracks.")]


def test_layers_of_the_tech_and_the_cell_lef_both_count(tmp_path: Path) -> None:
    tech = write(tmp_path, "t.lef", "LAYER a\nTYPE ROUTING ;\nPITCH 1 ;\nOFFSET 0.5 ;\nEND a\n")
    cells = write(
        tmp_path,
        "c.lef",
        "LAYER b\nTYPE ROUTING ;\nPITCH 2 ;\nOFFSET 1 ;\nEND b\n" + macro("M", "s"),
    )
    derived = derive({"tech_lef": [tech], "cell_lef": [cells]}, "typical")
    assert {"tracks.a", "tracks.b", "site"} <= set(derived)


def test_a_layer_name_with_a_dot_gives_no_track(tmp_path: Path) -> None:
    lef = "LAYER m.1\nTYPE ROUTING ;\nPITCH 1 ;\nOFFSET 0.5 ;\nEND m.1\n"
    assert not derive_text(tmp_path, lef)


def test_units_are_written_as_the_library_writes_them(tmp_path: Path) -> None:
    header = 'time_unit : "100ps";\ncapacitive_load_unit (1.0000000, "pf");\n'
    derived = derive_text(tmp_path, lib=library(header=header))
    assert (derived["time_unit"], derived["capacitance_unit"]) == ("100ps", "1pf")
    header = "time_unit : 1ns;\ncapacitive_load_unit(0.1, ff);\n"
    derived = derive_text(tmp_path, lib=library(header=header))
    assert (derived["time_unit"], derived["capacitance_unit"]) == ("1ns", "0.1ff")


def test_nothing_stated_gives_no_key(tmp_path: Path) -> None:
    assert derive_text(tmp_path, lef=macro("M", None), lib=library()) == {}
    assert derive({}, "typical") == {}


# ---------------------------------------------------------------------------------------------
# Syntax
# ---------------------------------------------------------------------------------------------


LIBERTY_SYNTAX = r"""/* a comment
   that spans lines: cell (FAKE) { dont_use : true; } */
library ("syntax") {
  /* one more */ time_unit : "1ns" ;   /* trailing */
  nom_voltage   :   0.95
    ;
  default_wire_load : "a \
     continued line";
  capacitive_load_unit (1,
     ff);
  lu_table_template (t) { variable_1 : x; index_1 ("1, 2, \
      3"); }
  cell ("QUOTED") { area : 2.5; dont_use : true; }
  cell (BARE) {
    area : 1.0;
    pin (A) { direction : input; capacitance : 1; timing () { related_pin : "B"; cell_rise (t) {
       values ( \
         "1, 2", \
         "3, 4"); } } }
    bus (B) { bus_type : t; direction : input; pin (B[0]) { direction : output; } }
    ff (IQ, IQN) { next_state : "A"; clocked_on : "CK"; }
    pin (Z) { function : "A & { B }"; direction : output }
  }
  cell (ONE_LINE) { area : 4; pin (Y) { direction : output; function : "0"; } }
}
"""


def test_liberty_syntax(tmp_path: Path) -> None:
    summary = read_liberty_summary(write(tmp_path, "syntax.lib", LIBERTY_SYNTAX))
    assert list(summary.cells) == ["QUOTED", "BARE", "ONE_LINE"]
    assert summary.nom_voltage == 0.95
    assert (summary.time_unit, summary.capacitive_load_unit) == ("1ns", "1ff")
    assert summary.cells["QUOTED"].dont_use
    assert summary.cells["QUOTED"].area == 2.5
    bare = summary.cells["BARE"]
    assert not bare.dont_use
    assert [(p.name, p.direction, p.function) for p in bare.pins] == [
        ("A", "input", None),
        ("B", "input", None),
        ("Z", "output", "A & { B }"),
    ]
    assert [(p.name, p.function) for p in summary.cells["ONE_LINE"].pins] == [("Y", "0")]


def test_a_pin_list_gives_each_pin_the_attributes_of_the_group(tmp_path: Path) -> None:
    lib = library('cell (T) { area : 1; pin (HI, "LO") { direction : output; function : "1"; } }\n')
    summary = read_liberty_summary(write(tmp_path, "list.lib", lib))
    assert [(p.name, p.direction, p.function) for p in summary.cells["T"].pins] == [
        ("HI", "output", "1"),
        ("LO", "output", "1"),
    ]
    assert derive_text(tmp_path, lib=lib)["tie_high"] == {"cell": "T", "port": "HI"}


def test_inputs_declared_as_a_pin_list_make_no_tie_cell(tmp_path: Path) -> None:
    lib = library(
        "cell (C) { area : 1; pin (A, B) { direction : input; } "
        'pin (Y) { direction : output; function : "1"; } }\n'
    )
    summary = read_liberty_summary(write(tmp_path, "list.lib", lib))
    assert [p.name for p in summary.cells["C"].pins] == ["A", "B", "Y"]
    assert "tie_high" not in derive_text(tmp_path, lib=lib)


def test_a_bus_counts_as_an_input_of_a_cell(tmp_path: Path) -> None:
    lib = library(
        "cell (C) { area : 1; bus (B) { direction : input; } "
        'pin (Y) { direction : output; function : "1"; } }\n'
    )
    assert "tie_high" not in derive_text(tmp_path, lib=lib)


LEF_SYNTAX = """# a comment
VERSION 5.8 ;
MACRO TIE # trailing comment
  CLASS CORE TIEHIGH ;
  SIZE 1 BY 2 ;
  SITE core ;
  PIN VDD
    USE POWER;
  END VDD
  PIN Z
    DIRECTION OUTPUT ; USE SIGNAL ;
    PORT
      LAYER m1 ;
        RECT 0 0 1 1 ;
    END
  END Z
  OBS
    LAYER m1 ;
      RECT 0 0 1 1 ;
  END
END TIE
MACRO TWO
  CLASS CORE ANTENNACELL ;
  SITE core ;
  PIN I
    USE SIGNAL ; ANTENNADIFFAREA 0.5 LAYER m1 ; ANTENNADIFFAREA 0.1 ;
  END I
END TWO
END LIBRARY
"""


def test_lef_syntax(tmp_path: Path) -> None:
    summary = read_lef_summary([write(tmp_path, "syntax.lef", LEF_SYNTAX)])
    assert summary.sites == {"core": 2}
    tie_macro = summary.macros["TIE"]
    assert tie_macro.cls == ("CORE", "TIEHIGH")
    assert tie_macro.size == (1.0, 2.0)
    assert tie_macro.pins["VDD"].use == "POWER"
    assert (tie_macro.pins["Z"].direction, tie_macro.pins["Z"].use) == ("OUTPUT", "SIGNAL")
    assert summary.macros["TWO"].pins["I"].antenna_diff_area == 0.5  # the largest


def test_lef_statements_may_span_lines(tmp_path: Path) -> None:
    lef = (
        "LAYER a\n  TYPE ROUTING ;\n  PITCH 0.14\n    0.16 ;\n  OFFSET 0.1\n    0.2\n  ;\nEND a\n"
        "MACRO M\n  CLASS CORE\n    ANTENNACELL ;\n  SIZE 1 BY\n    2 ;\n  SITE\n    s ;\n"
        "  PIN VDD\n    USE\n      POWER\n    ;\n  END VDD\n"
        "  PIN I\n    ANTENNADIFFAREA\n      0.5 ;\n  END I\n"
        "END M\n"
    )
    summary = read_lef_summary([write(tmp_path, "span.lef", lef)])
    assert (summary.layers[0].pitch, summary.layers[0].offset) == ((0.14, 0.16), (0.1, 0.2))
    macro_m = summary.macros["M"]
    assert (macro_m.cls, macro_m.size, macro_m.site) == (("CORE", "ANTENNACELL"), (1.0, 2.0), "s")
    assert macro_m.pins["VDD"].use == "POWER"
    assert macro_m.pins["I"].antenna_diff_area == 0.5
    derived = derive_text(tmp_path, lef)
    assert derived["tracks.a"] == {"x": [0.1, 0.14], "y": [0.2, 0.16]}
    assert derived["supply.power_pin"] == "VDD"


def test_lef_block_of_a_nondefault_rule_or_via_is_no_layer(tmp_path: Path) -> None:
    summary = read_lef_summary(FILES["tech_lef"])
    assert [layer.name for layer in summary.layers] == ["m1", "m2", "m3", "m4"]
    assert summary.layers[0].pitch == (0.2, 0.2)


def test_a_gzip_liberty_file_gives_the_same_facts(tmp_path: Path) -> None:
    packed = tmp_path / "tiny.lib.gz"
    packed.write_bytes(gzip.compress((TINY / "tiny.lib").read_bytes()))
    plain = derive(FILES, "typical")
    assert derive({**FILES, "liberty": [packed]}, "typical") == plain


def test_a_gzip_lef_file_gives_the_same_facts(tmp_path: Path) -> None:
    packed = tmp_path / "tiny.lef.gz"
    packed.write_bytes(gzip.compress((TINY / "tiny.lef").read_bytes()))
    assert derive({**FILES, "cell_lef": [packed]}, "typical") == derive(FILES, "typical")


def test_bytes_that_are_no_utf8_in_a_comment_do_not_stop_the_read(tmp_path: Path) -> None:
    lib = tmp_path / "latin.lib"
    lib.write_bytes(b"/* \xe9\xe8 */\nlibrary (t) { nom_voltage : 1.2; }\n")
    assert derive({"liberty": [lib]}, "typical") == {"supply.voltage": 1.2}


def test_several_liberty_files_of_one_corner(tmp_path: Path) -> None:
    first = write(
        tmp_path,
        "a.lib",
        library(
            tie("TIEH", 2.0),
            "cell (X) { area : 1; dont_use : true; }\n",
            header="nom_voltage : 1.0;\n",
        ),
    )
    second = write(
        tmp_path,
        "b.lib",
        library(
            tie("TIEH_SMALL", 1.0),
            tie("TIEL", 1.0, "0"),
            "cell (Y) { area : 1; dont_use : true; }\n",
            header='nom_voltage : 2.0;\ntime_unit : "1ps";\n',
        ),
    )
    derived = derive({"liberty": [first, second]}, "typical")
    assert derived["supply.voltage"] == 1.0  # the header of the first file that has the value
    assert derived["time_unit"] == "1ps"  # the first file that states a unit
    assert derived["tie_high"]["cell"] == "TIEH_SMALL"
    assert derived["tie_low"]["cell"] == "TIEL"
    assert derived["dont_use_cells"] == ["X", "Y"]


def test_the_first_cell_of_a_name_counts_when_files_repeat_it(tmp_path: Path) -> None:
    first = write(tmp_path, "a.lib", library("cell (X) { area : 1; dont_use : true; }\n"))
    second = write(tmp_path, "b.lib", library("cell (X) { area : 1; dont_use : true; }\n"))
    assert derive({"liberty": [first, second]}, "typical")["dont_use_cells"] == ["X"]


# ---------------------------------------------------------------------------------------------
# The reference tech LEF
# ---------------------------------------------------------------------------------------------


@pytest.mark.skipif(not NANGATE45.is_file(), reason="the nangate45 reference checkout is missing")
def test_nangate45_tech_lef_tracks() -> None:
    derived = derive({"tech_lef": [NANGATE45]}, "typical")
    assert derived["tracks.metal1"] == {"x": [0.095, 0.14], "y": [0.07, 0.14]}
    assert "tracks.poly" not in derived
    assert "tracks.via1" not in derived


# ---------------------------------------------------------------------------------------------
# Purity
# ---------------------------------------------------------------------------------------------

_SEEN: Optional[list[tuple[str, tuple]]] = None


def _audit(event: str, args: tuple) -> None:
    if _SEEN is not None and (
        event == "open"
        or event.startswith(("subprocess.", "socket.", "os.exec", "os.spawn", "os.posix_spawn"))
        or event
        in ("os.system", "os.fork", "os.forkpty", "os.remove", "os.rename", "os.rmdir", "os.mkdir")
    ):
        _SEEN.append((event, args))


sys.addaudithook(_audit)

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


def test_derive_reads_each_file_once_and_writes_nothing(tmp_path: Path) -> None:
    global _SEEN
    packed = tmp_path / "tiny.lib.gz"
    packed.write_bytes(gzip.compress((TINY / "tiny.lib").read_bytes()))
    files = {**FILES, "liberty": [packed]}
    derive(files, "typical")  # import everything the derivation needs before the watch starts
    _SEEN = []
    try:
        derive(files, "typical")
    finally:
        seen, _SEEN = _SEEN, None
    wanted = {str(path) for paths in files.values() for path in paths}
    opened = [str(args[0]) for event, args in seen if event == "open" and str(args[0]) in wanted]
    assert sorted(opened) == sorted(wanted)
    others = [
        (event, args) for event, args in seen if not (event == "open" and str(args[0]) in wanted)
    ]
    assert not others
    for event, args in seen:
        path, mode, flags = args
        assert not (isinstance(mode, str) and any(c in mode for c in "wax+")), args
        assert not (isinstance(flags, int) and flags & _WRITE_FLAGS), args


def test_derive_starts_no_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("derive started a process")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    assert derive(FILES, "typical")["site"] == "tinysite"


# ---------------------------------------------------------------------------------------------
# The fixture library works with real yosys
# ---------------------------------------------------------------------------------------------

COUNTER = """module counter(input clk, input rst, output reg [3:0] q);
  always @(posedge clk) if (rst) q <= 4'd0; else q <= q + 4'd1;
endmodule
"""


def test_yosys_maps_a_counter_to_the_fixture_library(tmp_path: Path) -> None:
    require_yosys()
    liberty = TINY / "tiny.lib"
    write(tmp_path, "counter.v", COUNTER)
    script = (
        f"read_verilog counter.v; synth -top counter; dfflibmap -liberty {liberty}; "
        f"abc -liberty {liberty}; stat -liberty {liberty}"
    )
    done = subprocess.run(
        ["yosys", "-p", script], cwd=tmp_path, capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr
    assert "ERROR" not in done.stdout
    cells = {
        line.split()[2]
        for line in done.stdout.rsplit("Chip area", 1)[0].splitlines()[-12:]
        if line.split()[1:2] and line.split()[-1].endswith(("_X1", "_X4"))
    }
    assert "DFF_X1" in cells
    assert cells <= {"DFF_X1", "INV_X1", "BUF_X1", "NAND2_X1", "NOR2_X1"}


# ------------------------------------------------------------------------------ include_file
#
# OpenSTA opens the name of `include_file(...)` as written, in its working directory, and keeps the
# quotes of a quoted name. Only an absolute name without quotes works, so the reader takes only
# that, and it reads the text that OpenROAD reads.


def test_a_unit_stated_only_in_an_included_file_is_seen(tmp_path: Path) -> None:
    (tmp_path / "units.lib").write_text('time_unit : "1ps";\nnom_voltage : 0.9;\n')
    (tmp_path / "cell.lib").write_text("cell (C) { area : 2; }\n")
    top = tmp_path / "top.lib"
    top.write_text(
        f"library (t) {{\n  include_file({tmp_path / 'units.lib'});\n"
        # a space before the parenthesis or before the semicolon is no part of the name; a space just
        # inside the parentheses is
        f"  include_file ({tmp_path / 'cell.lib'}) ;\n}}\n"
    )
    summary = read_liberty_summary(top)
    assert (summary.time_unit, summary.nom_voltage) == ("1ps", 0.9)
    assert list(summary.cells) == ["C"]


def test_an_included_file_includes_another_by_its_absolute_name(tmp_path: Path) -> None:
    (tmp_path / "outer.lib").write_text(f"include_file({tmp_path / 'inner.lib'});\n")
    (tmp_path / "inner.lib").write_text('time_unit : "10ps";\n')
    top = tmp_path / "top.lib"
    top.write_text(f"library (t) {{ include_file({tmp_path / 'outer.lib'}); }}\n")
    assert read_liberty_summary(top).time_unit == "10ps"


def test_a_relative_or_a_quoted_name_is_refused_as_opensta_cannot_open_it(tmp_path: Path) -> None:
    (tmp_path / "units.lib").write_text('time_unit : "1ps";\n')
    top = tmp_path / "top.lib"
    top.write_text("library (t) { include_file(units.lib); }\n")
    with pytest.raises(ValueError, match="no absolute name.*working directory"):
        read_liberty_summary(top)
    top.write_text(f'library (t) {{ include_file("{tmp_path / "units.lib"}"); }}\n')
    with pytest.raises(ValueError, match="in quotes.*keeps the quotes"):
        read_liberty_summary(top)


def test_a_missing_included_file_is_a_clear_error(tmp_path: Path) -> None:
    top = tmp_path / "top.lib"
    top.write_text(f"library (t) {{ include_file({tmp_path / 'gone.lib'}); }}\n")
    with pytest.raises(ValueError, match=r"top\.lib includes .*gone\.lib, which is not a file"):
        read_liberty_summary(top)


def test_files_that_include_each_other_are_refused(tmp_path: Path) -> None:
    (tmp_path / "a.lib").write_text(f"library (a) {{ include_file({tmp_path / 'b.lib'}); }}\n")
    (tmp_path / "b.lib").write_text(f"include_file({tmp_path / 'a.lib'});\n")
    with pytest.raises(ValueError, match="includes itself"):
        read_liberty_summary(tmp_path / "a.lib")


def chain_of_includes(tmp_path: Path, length: int) -> Path:
    """Files f0 .. f<length>: each includes the next, the last states the unit."""
    for number in range(length):
        nxt = tmp_path / f"f{number + 1}.lib"
        body = f"include_file({nxt});\n"
        (tmp_path / f"f{number}.lib").write_text(
            f"library (t) {{ {body} }}\n" if number == 0 else body
        )
    (tmp_path / f"f{length}.lib").write_text('time_unit : "1ps";\n')
    return tmp_path / "f0.lib"


def test_a_chain_of_includes_as_deep_as_the_limit_is_read_and_one_more_is_refused(
    tmp_path: Path,
) -> None:
    from xeda.platforms.derive import MAX_INCLUDE_DEPTH

    (tmp_path / "ok").mkdir()
    allowed = chain_of_includes(tmp_path / "ok", MAX_INCLUDE_DEPTH)
    assert read_liberty_summary(allowed).time_unit == "1ps"
    (tmp_path / "deep").mkdir()
    deeper = chain_of_includes(tmp_path / "deep", MAX_INCLUDE_DEPTH + 1)
    with pytest.raises(ValueError, match="levels deep"):
        read_liberty_summary(deeper)


def test_the_derivation_reads_the_unit_of_an_included_file(tmp_path: Path) -> None:
    (tmp_path / "units.lib").write_text('time_unit : "1ps";\n')
    lib = library(f'include_file({tmp_path / "units.lib"});\ncell (T) {{ area : 1; }}\n')
    assert derive_text(tmp_path, lib=lib)["time_unit"] == "1ps"


@pytest.mark.parametrize(
    "text",
    [
        'comment : "Use include_file(optional.lib) for the cells.";',
        'comment : "a \\" quote, then include_file(optional.lib)";',
        "my_include_file(optional.lib);",
    ],
    ids=["in a string", "after an escaped quote", "a longer name"],
)
def test_text_that_is_no_include_statement_is_left_alone(tmp_path: Path, text: str) -> None:
    top = tmp_path / "top.lib"
    top.write_text(
        f'library (t) {{\n  {text}\n  time_unit : "1ns";\n  cell (C) {{ area : 1; }}\n}}\n'
    )
    summary = read_liberty_summary(top)
    assert summary.time_unit == "1ns" and list(summary.cells) == ["C"]


def test_a_string_next_to_a_real_include_does_not_hide_it(tmp_path: Path) -> None:
    (tmp_path / "units.lib").write_text('time_unit : "1ps";\n')
    top = tmp_path / "top.lib"
    top.write_text(
        'library (t) {\n  comment : "include_file(optional.lib)";\n'
        f"  include_file({tmp_path / 'units.lib'});\n}}\n"
    )
    assert read_liberty_summary(top).time_unit == "1ps"


def test_the_included_files_are_listed_once_in_the_order_of_the_text(tmp_path: Path) -> None:
    from xeda.platforms.derive import liberty_included_files

    one, two, three = (tmp_path / name for name in ("one.lib", "two.lib", "three.lib"))
    three.write_text("cell (C) { area : 1; }\n")
    two.write_text(f"include_file({three});\n")
    one.write_text('time_unit : "1ns";\n')
    top = tmp_path / "top.lib"
    top.write_text(
        f"library (t) {{ include_file({one}); include_file({two}); include_file({three}); }}\n"
    )
    assert liberty_included_files(top) == [one, two, three]
    assert liberty_included_files(three) == []


def test_an_absolute_name_with_spaces_is_read_as_opensta_reads_it(tmp_path: Path) -> None:
    """OpenSTA takes everything between the parentheses as the name, so a directory name with a
    space works, and a space next to a parenthesis is part of the name."""
    from xeda.platforms.derive import liberty_included_files

    folder = tmp_path / "dir with space"
    folder.mkdir()
    (folder / "units.lib").write_text('time_unit : "1ps";\n')
    top = tmp_path / "top.lib"
    top.write_text(f"library (t) {{ include_file({folder / 'units.lib'}); }}\n")
    assert read_liberty_summary(top).time_unit == "1ps"
    assert liberty_included_files(top) == [folder / "units.lib"]
    top.write_text(f"library (t) {{ include_file( {folder / 'units.lib'} ); }}\n")
    with pytest.raises(ValueError, match="no absolute name"):
        read_liberty_summary(top)


@pytest.mark.parametrize("statement", ["include_file;", "include_file(a.lib", "include_file = x;"])
def test_an_include_file_the_reader_cannot_read_is_an_error_and_not_skipped(
    tmp_path: Path, statement: str
) -> None:
    top = tmp_path / "top.lib"
    top.write_text(f'library (t) {{\n  {statement}\n  time_unit : "1ns";\n}}\n')
    with pytest.raises(ValueError, match="include_file that the reader cannot read"):
        read_liberty_summary(top)


def test_comment_marks_inside_strings_start_no_comment(tmp_path: Path) -> None:
    """OpenSTA lexes a string and a comment apart, so an include between two strings that hold
    `/*` and `*/` is still an include."""
    from xeda.platforms.derive import liberty_included_files

    (tmp_path / "units.lib").write_text('time_unit : "1ps";\n')
    top = tmp_path / "top.lib"
    top.write_text(
        'library (t) {\n  comment : "Opening token /*";\n'
        f'  include_file({tmp_path / "units.lib"});\n  revision : "Closing token */";\n}}\n'
        "/* a real comment: include_file(/gone.lib); */\n"
    )
    assert read_liberty_summary(top).time_unit == "1ps"
    assert liberty_included_files(top) == [tmp_path / "units.lib"]


def test_the_time_unit_keeps_the_spaces_inside_its_quotes(tmp_path: Path) -> None:
    """OpenSTA reads `"1ns "` as an unknown suffix, so the reader does not tidy it."""
    top = tmp_path / "top.lib"
    top.write_text('library (t) { time_unit : "1ns "; }\n')
    assert read_liberty_summary(top).time_unit == "1ns "


def test_a_line_comment_that_mentions_include_file_is_no_include(tmp_path: Path) -> None:
    from xeda.platforms.derive import liberty_included_files

    top = tmp_path / "top.lib"
    top.write_text(
        'library (t) {\n  // include_file(/gone/optional.lib);\n  time_unit : "1ns";\n'
        '  comment : "a // inside a string";\n}\n'
    )
    assert read_liberty_summary(top).time_unit == "1ns"
    assert liberty_included_files(top) == []


def test_a_compressed_file_has_its_include_names_renamed(tmp_path: Path) -> None:
    from xeda.platforms.derive import liberty_text_with_includes_renamed

    (tmp_path / "units.lib").write_text('time_unit : "1ns";\n')
    top = tmp_path / "top.lib.gz"
    with gzip.open(top, "wt") as stream:
        stream.write(
            f'/* include_file({tmp_path / "units.lib"}); */\ninclude_file({tmp_path / "units.lib"});\n'
        )
    text = liberty_text_with_includes_renamed(top, lambda included: f"/remote/{included.name}")
    assert text.count("include_file(/remote/units.lib);") == 1
    assert str(tmp_path) in text, "the include in the comment stays as it is"


def test_an_escaped_quote_does_not_end_a_string_in_any_scan(tmp_path: Path) -> None:
    """One rule for strings: a backslash escapes the next character. The statement splitter, the
    comment scan and the include scan agree on a string that holds `\\"`, `;`, `/*` and
    `include_file(...)`."""
    from xeda.platforms.derive import liberty_included_files

    top = tmp_path / "top.lib"
    top.write_text(
        'library (t) {\n  comment : "a \\" ; /* include_file(/gone.lib); b";\n'
        '  time_unit : "1ns";\n  cell (C) { area : 1; }\n}\n'
    )
    summary = read_liberty_summary(top)
    assert summary.time_unit == "1ns" and list(summary.cells) == ["C"]
    assert liberty_included_files(top) == []


def test_the_rewrite_scan_passes_over_a_string_with_an_escaped_quote(tmp_path: Path) -> None:
    from xeda.platforms.derive import liberty_text_with_includes_renamed

    top = tmp_path / "top.lib"
    top.write_text(
        'library (t) {\n  comment : "a \\" include_file(/fake.lib); b";\n'
        "  include_file(/real.lib);\n}\n"
    )
    text = liberty_text_with_includes_renamed(top, lambda included: f"/remote/{included.name}")
    assert 'include_file(/fake.lib); b"' in text
    assert text.count("include_file(/remote/real.lib);") == 1
