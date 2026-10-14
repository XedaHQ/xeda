"""Each bundled platform keeps working against the PDK files it pins.

The checks that need the real PDK files are an opt-in layer: `XEDA_TESTS_PDK_ROOT` names a PDK
store and `XEDA_TESTS_PLATFORMS` the bundled platforms that store holds (`require_pdk_store` and
`pdk_test_platforms` in `tool_utils.py`). A job that holds only `nangate45` lists only
`nangate45`. A platform that the list names and the store lacks fails its tests.

For each listed platform the store must resolve every role of the residue, the pin must verify,
the platform model must build from the real files, the residue must have no line that a lower
layer makes useless, and real `yosys` must map a small counter to the cells of the platform.

The checks that need no PDK files run always: every bundled residue loads, and the residue
snippets of `docs/platforms.rst` are residue files.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows import Yosys
from xeda.platforms import PdkNotInstalled, Residue, build_platform, bundled_platform_names
from xeda.platforms import load_residue, locate
from xeda.platforms.build import residue_problems
from xeda.platforms.derive import derive
from xeda.platforms.providers import verify_pin
from xeda.platforms.upstream import read_upstream

from .tool_utils import pdk_test_platforms, require_pdk_store, require_yosys

ROOT = Path(__file__).parent.parent
PLATFORMS_RST = ROOT / "docs" / "platforms.rst"

COUNTER = (
    "module top(input clk, input rst, output reg [3:0] q);\n"
    "  always @(posedge clk) if (rst) q <= 4'd0; else q <= q + 4'd1;\n"
    "endmodule\n"
)

# ---------------------------------------------------------------------------- the test variables


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


def _layers(name: str) -> tuple[Path, Residue, Any, dict[str, Any], dict[str, Any]]:
    store = require_pdk_store()
    residue = load_residue(name)
    located = locate(residue, None, None, store, None)
    assert residue.upstream is not None, "a bundled platform names its upstream configuration"
    derived = derive(located.files, located.corner)
    upstream = read_upstream(residue.upstream.adapter, located.upstream_files, located.directory)
    return store, residue, located, derived, upstream


@pytest.mark.parametrize("name", pdk_test_platforms())
def test_the_store_resolves_every_role_and_the_pin_verifies(name):
    store = require_pdk_store()
    residue = load_residue(name)
    try:
        located = locate(residue, None, None, store, None)
    except PdkNotInstalled as error:
        pytest.fail(f"XEDA_TESTS_PLATFORMS lists {name}, but the store lacks it:\n{error}")
    verify_pin(located, residue)
    expected = residue.files.role_paths(located.corner)
    assert set(located.files) == set(expected)
    for role, paths in located.files.items():
        assert len(paths) == len(expected[role]), role
        assert all(path.is_file() for path in paths), role
    assert located.upstream_files and all(path.is_file() for path in located.upstream_files)


@pytest.mark.parametrize("name", pdk_test_platforms())
def test_the_model_builds_from_the_real_files(name):
    _, residue, located, derived, upstream = _layers(name)
    model = build_platform(located, residue, None, derived, upstream)
    assert model.name == name
    assert model.corner == located.corner
    assert model.files == located.files
    assert model.site, "the site comes from the LEF"
    assert model.tracks, "the tracks come from the LEF"
    assert model.sources["site"] in ("derived", "upstream")
    assert all(model.sources[f"files.{role}"] == "residue" for role in model.files)


@pytest.mark.parametrize("name", pdk_test_platforms())
def test_the_residue_has_no_line_that_a_lower_layer_makes_useless(name):
    _, residue, _, derived, upstream = _layers(name)
    assert residue_problems(residue, derived, upstream) == []


def _liberty_cells(files: list[Path]) -> dict[str, bool]:
    """The cells of Liberty files, each with whether it holds a flip-flop (`ff` group)."""
    cells: dict[str, bool] = {}
    for liberty in files:
        parts = re.split(
            r"^\s*cell\s*\(\s*\"?([\w$.\[\]]+)\"?\s*\)", liberty.read_text(), flags=re.M
        )
        for name, body in zip(parts[1::2], parts[2::2]):
            cells[name] = re.search(r"^\s*ff\s*\(", body, re.M) is not None
    return cells


@pytest.mark.parametrize("name", pdk_test_platforms())
def test_real_yosys_maps_a_counter_to_the_cells_of_the_platform(name, tmp_path):
    require_yosys()
    store, _, located, derived, upstream = _layers(name)
    design_root = tmp_path / "design"
    design_root.mkdir()
    (design_root / "top.v").write_text(COUNTER)
    design = Design(
        name="top",
        design_root=design_root,
        rtl={"sources": ["top.v"], "top": "top", "clock": {"port": "clk"}},
    )
    settings = {"platform": name, "pdk_root": str(store), "clock": {"period": 10.0}}
    flow = DefaultRunner(tmp_path / "xeda_run", display_results=False).launch_flow(
        Yosys, design, settings
    )
    assert flow.succeeded
    results = json.loads((flow.run_path / "results.json").read_text())
    assert results["platform"]["name"] == name
    netlist = (flow.run_path / "netlist.v").read_text()
    used = re.findall(r"^\s*(\w+)\s+\S+\s*\(", netlist, re.M)
    used = [cell for cell in used if cell != "module"]
    library = _liberty_cells(located.files["liberty"])
    assert used and set(used) <= set(library), sorted(set(used) - set(library))
    assert "$_" not in netlist, "an unmapped generic cell is left in the netlist"
    assert (
        sum(library[cell] for cell in used) == 4
    ), "one flip-flop cell for each bit of the counter"
