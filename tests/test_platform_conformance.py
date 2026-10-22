"""Each bundled platform keeps working against the PDK files it pins.

The checks that need the real PDK files are an opt-in layer: `XEDA_TESTS_PDK_ROOT` names a PDK
store and `XEDA_TESTS_PLATFORMS` the bundled platforms that store holds (`require_pdk_store` and
`pdk_test_platforms` in `tool_utils.py`). A job that holds only `nangate45` lists only
`nangate45`. A platform that the list names and the store lacks fails its tests. The variable
`XEDA_TESTS_PDK_PROVIDERS` names more pairs, such as `sky130hd:lambdapdk`: the same store holds
the platform from a second provider, and the checks below run for that provider too.

For each listed platform the store must resolve every role of the residue, the pin must verify,
the platform model must build from the real files, the residue must have no line that a lower
layer makes useless, and real `yosys` must map a small counter to the cells of the platform.

The checks that need no PDK files run always: every bundled residue loads, and the residue
snippets of `docs/platforms.rst` are residue files.
"""

import gzip
import re
import sys
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Optional
from xml.etree import ElementTree

import pytest

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows import Yosys
from xeda.flows.openroad.final import klayout_tech_with_lef_files
from xeda.platforms import PdkNotInstalled, Residue, build_platform, bundled_platform_names
from xeda.platforms import load_residue, locate
from xeda.platforms.build import residue_problems
from xeda.platforms.derive import derive
from xeda.platforms.providers import verify_pin
from xeda.platforms.upstream import read_upstream

from .tool_utils import (
    pdk_pair_id,
    pdk_test_pairs,
    pdk_test_platforms,
    require_pdk_store,
    require_yosys,
)

ROOT = Path(__file__).parent.parent
PLATFORMS_RST = ROOT / "docs" / "platforms.rst"

COUNTER = (
    "module top(input clk, input rst, output reg [3:0] q);\n"
    "  always @(posedge clk) if (rst) q <= 4'd0; else q <= q + 4'd1;\n"
    "endmodule\n"
)

# ---------------------------------------------------------------------------- the test variables


def test_without_a_store_the_providers_with_a_layout_of_their_own_are_collected(monkeypatch):
    monkeypatch.delenv("XEDA_TESTS_PDK_ROOT", raising=False)
    pairs = pdk_test_pairs()
    assert ("sky130hd", None) in pairs and ("sky130hd", "lambdapdk") in pairs
    assert ("nangate45", None) in pairs and not [p for p in pairs if p[0] == "nangate45" and p[1]]
    assert pdk_pair_id(("sky130hd", "lambdapdk")) == "sky130hd-lambdapdk"
    assert pdk_pair_id(("sky130hd", None)) == "sky130hd"


def test_the_extra_pairs_are_named_by_platform_and_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("XEDA_TESTS_PDK_ROOT", str(tmp_path))
    monkeypatch.setenv("XEDA_TESTS_PLATFORMS", "nangate45,sky130hd")
    monkeypatch.delenv("XEDA_TESTS_PDK_PROVIDERS", raising=False)
    assert pdk_test_pairs() == [("nangate45", None), ("sky130hd", None)]
    monkeypatch.setenv("XEDA_TESTS_PDK_PROVIDERS", "sky130hd:lambdapdk")
    assert pdk_test_pairs() == [("nangate45", None), ("sky130hd", None), ("sky130hd", "lambdapdk")]


@pytest.mark.parametrize(
    "listed",
    [
        "sky130hd",  # no provider
        "sky130hd:lambdapdk:x",
        "sky130hd:ciel",  # the default provider
        "sky130hd:directory",
        "sky130hd:orfs",  # does not serve sky130hd
        "nangate45:lambdapdk",  # nangate45 is not listed below, or not served
        "sky130hd:lambdapdk,sky130hd:lambdapdk",
        "sky130hd:lambdapdk,",
    ],
)
def test_a_wrong_pair_fails(monkeypatch, tmp_path, listed):
    monkeypatch.setenv("XEDA_TESTS_PDK_ROOT", str(tmp_path))
    monkeypatch.setenv("XEDA_TESTS_PLATFORMS", "sky130hd")
    monkeypatch.setenv("XEDA_TESTS_PDK_PROVIDERS", listed)
    with pytest.raises(pytest.fail.Exception, match="XEDA_TESTS_PDK_PROVIDERS"):
        pdk_test_pairs()


def test_the_store_is_required_by_name(monkeypatch):
    monkeypatch.delenv("XEDA_TESTS_PDK_ROOT", raising=False)
    with pytest.raises(pytest.skip.Exception, match="XEDA_TESTS_PDK_ROOT"):
        require_pdk_store()


def test_a_store_that_is_no_directory_fails(monkeypatch, tmp_path):
    missing = tmp_path / "no-such-directory"
    monkeypatch.setenv("XEDA_TESTS_PDK_ROOT", str(missing))
    with pytest.raises(pytest.fail.Exception, match="XEDA_TESTS_PDK_ROOT"):
        require_pdk_store()


def test_the_store_is_the_directory_the_variable_names(monkeypatch, tmp_path):
    monkeypatch.setenv("XEDA_TESTS_PDK_ROOT", str(tmp_path))
    assert require_pdk_store() == tmp_path


def test_a_store_without_a_platform_list_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("XEDA_TESTS_PDK_ROOT", str(tmp_path))
    monkeypatch.delenv("XEDA_TESTS_PLATFORMS", raising=False)
    with pytest.raises(pytest.fail.Exception, match="XEDA_TESTS_PLATFORMS"):
        pdk_test_platforms()


@pytest.mark.parametrize("listed", ["nangate45,nosuchplatform", "nangate45,,sky130hd", ""])
def test_a_platform_list_names_bundled_platforms_only(monkeypatch, tmp_path, listed):
    monkeypatch.setenv("XEDA_TESTS_PDK_ROOT", str(tmp_path))
    monkeypatch.setenv("XEDA_TESTS_PLATFORMS", listed)
    with pytest.raises(pytest.fail.Exception, match="XEDA_TESTS_PLATFORMS"):
        pdk_test_platforms()


def test_the_platform_list_is_comma_separated(monkeypatch, tmp_path):
    monkeypatch.setenv("XEDA_TESTS_PDK_ROOT", str(tmp_path))
    monkeypatch.setenv("XEDA_TESTS_PLATFORMS", "nangate45")
    assert pdk_test_platforms() == ["nangate45"]


def test_without_a_store_every_bundled_platform_is_listed_and_skips(monkeypatch):
    """The tests are collected for every platform, and each skips naming the variable."""
    monkeypatch.delenv("XEDA_TESTS_PDK_ROOT", raising=False)
    assert pdk_test_platforms() == bundled_platform_names()


def test_the_yosys_test_skips_for_the_store_before_it_asks_for_yosys(monkeypatch, tmp_path):
    """With `XEDA_TESTS_REQUIRE_TOOLS` set, a missing yosys fails a test. A test that needs the
    store must skip for the missing store first, whether or not yosys is there."""

    def missing_yosys() -> None:
        pytest.fail("yosys was asked for before the store")

    monkeypatch.delenv("XEDA_TESTS_PDK_ROOT", raising=False)
    monkeypatch.setattr(sys.modules[__name__], "require_yosys", missing_yosys)
    with pytest.raises(pytest.skip.Exception, match="XEDA_TESTS_PDK_ROOT"):
        test_real_yosys_maps_a_counter_to_the_cells_of_the_platform(("nangate45", None), tmp_path)


# ---------------------------------------------------------------------------- always on


@pytest.mark.parametrize("name", bundled_platform_names())
def test_a_bundled_residue_loads_and_its_lines_are_in_the_right_place(name):
    """Without PDK files no lower layer exists, so the lint can find only an override that
    overrides nothing. The residue must still load, and the lint must run on it."""
    residue = load_residue(name)
    assert residue.name == name
    problems = residue_problems(residue, {}, {})
    assert all(
        problem.endswith("no lower layer gives it; move it to values") for problem in problems
    )
    assert len(problems) == len(residue.overrides)


# ---------------------------------------------------------------------------- the docs


def _yaml_blocks() -> list[str]:
    text = PLATFORMS_RST.read_text(encoding="utf-8")
    found = re.findall(r"^\.\. code-block:: yaml\n\n((?:(?:    .*)?\n)+)", text, re.M)
    return [re.sub(r"(?m)^    ", "", body).rstrip("\n") for body in found]


def test_the_platforms_page_gives_residue_files(tmp_path):
    blocks = _yaml_blocks()
    assert blocks, "docs/platforms.rst has no YAML example"
    for number, body in enumerate(blocks):
        assert not re.match(r"# \S+\.yaml\b", body), "a design fixture name starts the block"
        path = tmp_path / f"example-{number}.yaml"
        path.write_text(body + "\n", encoding="utf-8")
        assert isinstance(load_residue(path), Residue), body


def _within(shown: Any, whole: Any) -> bool:
    """Check that `shown` is `whole` with some table keys left out."""
    if isinstance(shown, dict):
        return isinstance(whole, dict) and all(
            key in whole and _within(value, whole[key]) for key, value in shown.items()
        )
    return bool(shown == whole)


def test_the_example_residue_of_the_page_is_part_of_the_bundled_one(tmp_path):
    """The first example is the file of `nangate45`, with long values left out. What it shows
    must be what the bundled file says."""
    path = tmp_path / "shown.yaml"
    path.write_text(_yaml_blocks()[0] + "\n", encoding="utf-8")
    shown, bundled = load_residue(path), load_residue("nangate45")
    assert shown.name == bundled.name
    for part in ("providers", "upstream", "corners", "files"):
        given = getattr(shown, part).model_dump(exclude_unset=True)
        whole = getattr(bundled, part).model_dump(exclude_unset=True)
        assert _within(given, whole), part
    for key, value in shown.value_leaves().items():
        assert bundled.value_leaves()[key] == value, key
    for key, value in shown.override_leaves().items():
        assert bundled.override_leaves()[key] == value, key


# ---------------------------------------------------------------------------- the PDK files


PAIRS = pdk_test_pairs()
IDS = [pdk_pair_id(pair) for pair in PAIRS]


def _layers(
    name: str, provider: Optional[str] = None
) -> tuple[Path, Residue, Any, dict[str, Any], dict[str, Any]]:
    """The residue as the provider sees it, where the provider put the files, and the derived and
    upstream layers. A provider with a layout of its own may have no upstream configuration."""
    store = require_pdk_store()
    residue = load_residue(name).for_provider(provider)
    located = locate(residue, provider, None, store, None)
    derived = derive(located.files, located.corner)
    upstream: dict[str, Any] = {}
    if residue.upstream is not None:
        upstream = read_upstream(
            residue.upstream.adapter, located.upstream_files, located.directory, located.library
        )
    return store, residue, located, derived, upstream


@pytest.mark.parametrize("pair", PAIRS, ids=IDS)
def test_the_store_resolves_every_role_and_the_pin_verifies(pair):
    name, provider = pair
    store = require_pdk_store()
    residue = load_residue(name).for_provider(provider)
    try:
        located = locate(residue, provider, None, store, None)
    except PdkNotInstalled as error:
        pytest.fail(f"the store lacks {pdk_pair_id(pair)}:\n{error}")
    verify_pin(located, residue)
    shipped = residue.shipped_paths()
    expected = {**shipped, **residue.files.role_paths(located.corner)}
    assert set(located.files) == set(expected)
    for role, paths in located.files.items():
        assert len(paths) == len(expected[role]), role
        assert all(path.is_file() for path in paths), role
    for role, paths in shipped.items():
        if role not in residue.files.role_paths(located.corner):
            assert located.files[role] == paths, "xeda's own file is in the package, not the store"
    if residue.upstream is not None:
        assert located.upstream_files and all(path.is_file() for path in located.upstream_files)
    else:
        assert located.upstream_files == []


@pytest.mark.parametrize("pair", PAIRS, ids=IDS)
def test_the_store_resolves_every_corner(pair):
    """Each corner of the residue names Liberty files that the store holds, and the aliases of
    the corners resolve to them."""
    name, provider = pair
    store = require_pdk_store()
    residue = load_residue(name).for_provider(provider)
    for corner in [*residue.files.liberty, *residue.corners.aliases]:
        located = locate(residue, provider, None, store, corner)
        assert located.corner == residue.resolve_corner(corner)
        assert located.files["liberty"] and all(path.is_file() for path in located.files["liberty"])


@pytest.mark.skipif("sky130hd" not in pdk_test_platforms(), reason="sky130hd is not in the store")
def test_the_directory_provider_reads_the_open_pdks_build_of_ciel():
    """A directory with the layout `open_pdks` is the version directory of `ciel`: it holds
    `sky130A` and `sky130B`. The residue names the same files for both providers."""
    store = require_pdk_store()
    residue = load_residue("sky130hd")
    ciel = locate(residue, "ciel", None, store, None)
    build = ciel.directory.parent
    found = locate(residue, "directory", None, build, None)
    assert found.directory == ciel.directory
    assert found.files == ciel.files
    verify_pin(found, residue)


@pytest.mark.parametrize("pair", PAIRS, ids=IDS)
def test_the_model_builds_from_the_real_files(pair):
    name, provider = pair
    _, residue, located, derived, upstream = _layers(name, provider)
    model = build_platform(located, residue, None, derived, upstream)
    assert model.name == name
    assert model.corner == located.corner
    assert model.files == located.files
    assert model.site, "the site comes from the LEF"
    assert model.tracks, "the tracks come from the LEF"
    assert model.sources["site"] in ("derived", "upstream")
    assert all(model.sources[f"files.{role}"] == "residue" for role in model.files)


@pytest.mark.parametrize("pair", PAIRS, ids=IDS)
def test_the_residue_has_no_line_that_a_lower_layer_makes_useless(pair):
    name, provider = pair
    _, residue, _, derived, upstream = _layers(name, provider)
    assert residue_problems(residue, derived, upstream) == []


@pytest.mark.parametrize("pair", PAIRS, ids=IDS)
def test_the_klayout_technology_file_takes_one_list_of_lef_files(pair):
    """The merge of the layout writes the technology file of the platform with its own list of LEF
    files. The result is XML with that list once, and the rest of the file is unchanged."""
    _, _, located, _, _ = _layers(*pair)
    template = located.files["klayout_tech"][0]
    lefs = [Path("/x/tech.lef"), Path("/x/cells.lef")]
    original = template.read_text()
    text = klayout_tech_with_lef_files(original, lefs)
    assert text is not None
    listed = [element.text for element in ElementTree.fromstring(text).iter("lef-files")]
    assert listed == [str(lef) for lef in lefs]

    def without_lists(source: str) -> str:
        return re.sub(r"\s+", "", re.sub(r"<lef-files>.*?</lef-files>", "", source, flags=re.S))

    assert without_lists(text) == without_lists(original)


def _liberty_cells(files: list[Path]) -> dict[str, bool]:
    """The cells of Liberty files (plain or `.gz`), each with whether it holds a flip-flop (`ff`
    group)."""
    cells: dict[str, bool] = {}
    for liberty in files:
        if liberty.suffix == ".gz":
            text = gzip.decompress(liberty.read_bytes()).decode("utf-8", errors="replace")
        else:
            text = liberty.read_text()
        parts = re.split(r"^\s*cell\s*\(\s*\"?([\w$.\[\]]+)\"?\s*\)", text, flags=re.M)
        for name, body in zip(parts[1::2], parts[2::2]):
            cells[name] = re.search(r"^\s*ff\s*\(", body, re.M) is not None
    return cells


def _synthesize(
    name: str,
    provider: Optional[str],
    tmp_path: Path,
    source: str,
    given: Optional[dict[str, Any]] = None,
) -> tuple[Any, list[str]]:
    """Run the Yosys flow on `source` (module `top`, clock `clk`) for the platform, with the
    settings in `given` added, and return the flow and the cells of its netlist."""
    store = require_pdk_store()
    design_root = tmp_path / "design"
    design_root.mkdir()
    (design_root / "top.v").write_text(source)
    design = Design(
        name="top",
        design_root=design_root,
        rtl={"sources": ["top.v"], "top": "top", "clock": {"port": "clk"}},
    )
    settings: dict[str, Any] = {
        "platform": name,
        "pdk_root": str(store),
        "clock": {"period": "10 ns"},
        **(given or {}),
    }
    if provider is not None:
        settings["pdk_provider"] = provider
    flow = DefaultRunner(tmp_path / "xeda_run", display_results=False).launch_flow(
        Yosys, design, settings
    )
    assert flow.succeeded
    netlist = (flow.run_path / "netlist.v").read_text()
    used = re.findall(r"^\s*(\w+)\s+\S+\s*\(", netlist, re.M)
    return flow, [cell for cell in used if cell != "module"]


@pytest.mark.parametrize("pair", PAIRS, ids=IDS)
def test_real_yosys_maps_a_counter_to_the_cells_of_the_platform(pair, tmp_path):
    name, provider = pair
    require_pdk_store()
    require_yosys()
    _, _, located, derived, upstream = _layers(name, provider)
    flow, used = _synthesize(name, provider, tmp_path, COUNTER)
    results = flow.results  # the record holds the described metrics only
    assert results["platform"]["name"] == name
    assert results["platform"]["provider"] == located.provider
    netlist = (flow.run_path / "netlist.v").read_text()
    library = _liberty_cells(located.files["liberty"])
    assert used and set(used) <= set(library), sorted(set(used) - set(library))
    assert not re.search(r"^\s*\\\$_", netlist, re.M), "a generic cell is left in the netlist"
    assert (
        sum(library[cell] for cell in used) == 4
    ), "one flip-flop cell for each bit of the counter"


# ---------------------------------------------------------------------------- the dont-use cells

ADDER_AND_LATCH = (
    "module top(input clk, input [7:0] a, b, input g, output reg [8:0] s, output reg l);\n"
    "  always @(posedge clk) s <= a + b;\n"
    "  always @* if (g) l = a[0] ^ b[0];\n"
    "endmodule\n"
)


@pytest.mark.parametrize("pair", PAIRS, ids=IDS)
def test_real_yosys_uses_no_cell_that_the_platform_excludes(pair, tmp_path):
    name, provider = pair
    require_pdk_store()
    require_yosys()
    model = load_model(name, provider)
    excluded = model.dont_use_cells
    assert excluded, "the platform excludes cells"
    flow, used = _synthesize(name, provider, tmp_path, ADDER_AND_LATCH)
    assert used
    results = flow.results  # the record holds the described metrics only
    assert results["platform"]["sources"]["dont_use_cells"] == model.sources["dont_use_cells"]
    hit = sorted({cell for cell in used for pattern in excluded if fnmatchcase(cell, pattern)})
    assert not hit, hit


CLOCK_GATE = (
    "module top(input clk, input en, input [3:0] d, output reg [3:0] q);\n"
    "  wire gclk;\n"
    "  OPENROAD_CLKGATE gate (.CK(clk), .E(en), .GCK(gclk));\n"
    "  always @(posedge gclk) q <= d;\n"
    "endmodule\n"
)


#: The pairs of a platform that ships a clock gate map (`shipped` in its residue), so that no case
#: is generated for a platform that ships none. A store of other platforms defines no test.
SHIPPING_PAIRS = [pair for pair in PAIRS if "clkgate_map" in load_residue(pair[0]).shipped]

if SHIPPING_PAIRS:

    @pytest.mark.parametrize("pair", SHIPPING_PAIRS, ids=[pdk_pair_id(p) for p in SHIPPING_PAIRS])
    def test_real_yosys_maps_the_shipped_clock_gate_module_to_the_integrated_clock_gate(
        pair, tmp_path
    ):
        """A design that instantiates `OPENROAD_CLKGATE` gets the integrated clock gate cell of
        the platform (`sky130_fd_sc_hd__dlclkp_1`) when the macro `OPENROAD_CLKGATE` is defined, as
        in OpenROAD-flow-scripts, and a wire when it is not."""
        name, provider = pair
        require_pdk_store()
        require_yosys()
        (tmp_path / "gated").mkdir()
        (tmp_path / "plain").mkdir()
        gated, used = _synthesize(
            name,
            provider,
            tmp_path / "gated",
            CLOCK_GATE,
            {"defines": {"OPENROAD_CLKGATE": None}},
        )
        assert used.count("sky130_fd_sc_hd__dlclkp_1") == 1
        assert used.count("sky130_fd_sc_hd__dfxtp_1") == 4
        netlist = (gated.run_path / "netlist.v").read_text()
        assert re.search(r"\.CLK\(clk\),\s*\.GATE\(en\)", netlist), "clock and enable are wired"
        _, plain = _synthesize(name, provider, tmp_path / "plain", CLOCK_GATE)
        assert "sky130_fd_sc_hd__dlclkp_1" not in plain
        assert plain.count("sky130_fd_sc_hd__dfxtp_1") == 4


def load_model(name: str, provider: Optional[str] = None) -> Any:
    _, residue, located, derived, upstream = _layers(name, provider)
    return build_platform(located, residue, None, derived, upstream)


@pytest.mark.skipif("sky130hd" not in pdk_test_platforms(), reason="sky130hd is not in the store")
def test_sky130hd_excludes_the_cells_of_the_drc_exclude_list_and_records_where_they_came_from():
    """`dont_use_cells` is the list that `DRC_EXCLUDE_CELL_LIST` names, in the order of the
    file, and the model says that the upstream configuration gave it."""
    _, residue, located, derived, upstream = _layers("sky130hd")
    model = build_platform(located, residue, None, derived, upstream)
    listed = (
        located.directory / "libs.tech/openlane/sky130_fd_sc_hd/drc_exclude.cells"
    ).read_text()
    names = listed.split()
    assert len(names) == 53
    assert model.dont_use_cells == names
    assert model.sources["dont_use_cells"] == "upstream"
    assert located.upstream_files[-1].name == "drc_exclude.cells"


# ---------------------------------------------------------------------------- two providers

LAMBDAPDK_LEAVES = {
    "tap_cell": "sky130_fd_sc_hd__tapvpwrvgnd_1",
    "endcap_cell": "sky130_fd_sc_hd__decap_4",
    "tapcell.distance": 14,
    "antenna_cell": {"cell": "sky130_fd_sc_hd__diode_2", "port": "DIODE"},
    "min_buffer": {"cell": "sky130_fd_sc_hd__buf_4", "input": "A", "output": "X"},
    "abc_driver_cell": "sky130_fd_sc_hd__buf_4",
    "abc_load_in_ff": 11,
    "routing.min_layer": "met1",
    "routing.max_layer": "met5",
    "io_layers.horizontal": "met3",
    "io_layers.vertical": "met2",
    "wire_rc.signal_layer": "met3",
    "wire_rc.clock_layer": "met5",
}
CIEL_LEAVES = {
    "tap_cell": "sky130_fd_sc_hd__tapvpwrvgnd_1",
    "endcap_cell": "sky130_fd_sc_hd__decap_3",
    "tapcell.distance": 13.0,
    "routing.clock_min_layer": "met3",
}


@pytest.mark.skipif(
    ("sky130hd", "lambdapdk") not in PAIRS, reason="the store has no sky130hd from lambdapdk"
)
def test_the_two_providers_of_sky130hd_give_their_own_values_for_the_same_library(capsys):
    """Both models describe the same library. Each gives the values of its own source, and the
    differences are printed, so that a change of a pin shows what moves."""
    ciel = load_model("sky130hd", None).leaf_values()
    lambdapdk = load_model("sky130hd", "lambdapdk").leaf_values()
    for key, value in LAMBDAPDK_LEAVES.items():
        assert lambdapdk[key] == value, key
    for key, value in CIEL_LEAVES.items():
        assert ciel[key] == value, key
    for key in ("site", "supply.voltage", "tie_high", "tie_low", "pdn.power_net"):
        assert ciel[key] == lambdapdk[key], key
    shown = sorted(
        key
        for key in {*ciel, *lambdapdk}
        if not key.startswith("files.") and ciel.get(key) != lambdapdk.get(key)
    )
    with capsys.disabled():
        print("sky130hd, ciel against lambdapdk, leaves that differ:", ", ".join(shown))
    assert "endcap_cell" in shown
