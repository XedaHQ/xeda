"""A bundled platform can ship a Yosys techmap that its PDK lacks.

`sky130hd` has no clock gate map in `open_pdks` or in `lambdapdk`, so xeda ships
`platforms/sky130hd/cells_clkgate.v` and names it in the key `shipped` of its residue file. The
file is in the package and not in the PDK store. These tests check the key (what it accepts and
what a user file does with it), that both providers of `sky130hd` find the file, that a role of
`files` replaces it, and that the `yosys` flow reads it with the defines of the design and
registers it as an input. The real `yosys` and the real PDK files are in
`test_platform_conformance.py`.
"""

import re
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
import yaml

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.trace_inputs import located_input_files, registered_input_files
from xeda.flows import Yosys
from xeda.platforms import Residue, load_residue, locate
from xeda.platforms.providers import _relative_files, git_checkout_directory, platform_directory
from xeda.platforms.residue import ResidueError, effective_residue

from .tool_utils import use_fake_asic_tools

#: The installed package, as the loader finds it: the source tree with an editable install, the
#: environment's site-packages with an sdist install (as in the full CI suite).
PACKAGE = Path(str(resources.files("xeda.platforms")))
GATE = PACKAGE / "sky130hd" / "cells_clkgate.v"
PROVIDERS = ("ciel", "lambdapdk")

CLOCK_GATE = (
    "module top(input clk, input en, input [3:0] d, output reg [3:0] q);\n"
    "  wire gclk;\n"
    "  OPENROAD_CLKGATE gate (.CK(clk), .E(en), .GCK(gclk));\n"
    "  always @(posedge gclk) q <= d;\n"
    "endmodule\n"
)


def sky130hd() -> Residue:
    return load_residue("sky130hd")


def user_file(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "user.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def store_with_sky130hd(tmp_path: Path, provider: str) -> Path:
    """A store that holds the files of `sky130hd` for the provider, as empty files."""
    residue = sky130hd().for_provider(provider)
    root = tmp_path / f"store-{provider}"
    if provider == "ciel":
        entry = residue.providers.ciel
        directory = root / "ciel" / entry.family / "versions" / entry.version / entry.variant
    else:
        entry = residue.providers.lambdapdk
        directory = git_checkout_directory(root, "lambdapdk", entry.version) / entry.directory
    for relative in _relative_files(residue, residue.resolve_corner(None)):
        (directory / relative).parent.mkdir(parents=True, exist_ok=True)
        (directory / relative).write_text("\n", encoding="utf-8")
    return root


# ------------------------------------------------------------------------------ the file


def test_the_clock_gate_map_has_the_module_and_the_ports_of_the_flow_that_xeda_follows():
    text = GATE.read_text(encoding="utf-8")
    code = "\n".join(line.split("//")[0] for line in text.splitlines())
    assert re.search(r"module\s+OPENROAD_CLKGATE\s*\(\s*CK\s*,\s*E\s*,\s*GCK\s*\)", code)
    for port, direction in (("CK", "input"), ("E", "input"), ("GCK", "output")):
        assert re.search(rf"^\s*{direction}\s+{port}\s*;", code, re.M), port
    on, _, off = code.partition("`else")
    assert "`ifdef OPENROAD_CLKGATE" in on
    assert re.search(
        r"sky130_fd_sc_hd__dlclkp_1\s+\w+\s*\(\s*\.CLK\(CK\)\s*,\s*\.GATE\(E\)\s*,\s*\.GCLK\(GCK\)\s*\)",
        on,
    )
    assert re.search(r"assign\s+GCK\s*=\s*CK\s*;", off)
    assert "`endif" in off


def test_the_file_is_in_the_package_data_of_xeda():
    import tomllib

    root = Path(__file__).parent.parent
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = data["tool"]["setuptools"]["package-data"]["xeda.platforms"]
    assert any(GATE.relative_to(PACKAGE).match(pattern) for pattern in patterns), patterns


def test_the_notice_names_the_file_as_a_file_of_xeda():
    notice = (PACKAGE / "NOTICE.md").read_text(encoding="utf-8")
    assert "sky130hd/cells_clkgate.v" in notice
    assert "a file written for xeda" in notice


# ------------------------------------------------------------------------------ the residue


def test_sky130hd_ships_the_map_and_names_no_such_file_in_its_files():
    residue = sky130hd()
    assert residue.shipped == {"clkgate_map": "cells_clkgate.v"}
    assert residue.files.clkgate_map is None
    assert residue.shipped_paths() == {"clkgate_map": [GATE]}


def test_a_residue_that_ships_nothing_gives_no_shipped_paths():
    assert load_residue("nangate45").shipped_paths() == {}


@pytest.mark.parametrize("provider", PROVIDERS)
def test_both_providers_of_sky130hd_find_the_file_in_the_package(tmp_path, provider):
    root = store_with_sky130hd(tmp_path, provider)
    located = locate(sky130hd(), provider, None, root, None)
    assert located.files["clkgate_map"] == [GATE]
    assert GATE.is_file() and not str(GATE).startswith(str(root))
    assert located.files["liberty"][0].is_relative_to(root), "the other roles stay in the store"


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_user_file_over_sky130hd_keeps_the_shipped_map(tmp_path, provider):
    user = load_residue(user_file(tmp_path, "name: sky130hd\nextends: sky130hd\n"))
    root = store_with_sky130hd(tmp_path, provider)
    residue = effective_residue(sky130hd(), user, provider)
    assert locate(residue, provider, None, root, None).files["clkgate_map"] == [GATE]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_role_in_files_replaces_the_shipped_map(tmp_path, provider):
    user = load_residue(
        user_file(
            tmp_path, "name: sky130hd\nextends: sky130hd\nfiles: {clkgate_map: my/clkgate.v}\n"
        )
    )
    root = store_with_sky130hd(tmp_path, provider)
    residue = effective_residue(sky130hd(), user, provider)
    assert residue.shipped == {}
    mine = platform_directory(residue, provider, None, root) / "my" / "clkgate.v"
    mine.parent.mkdir()
    mine.write_text("\n", encoding="utf-8")
    assert locate(residue, provider, None, root, None).files["clkgate_map"] == [mine]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_null_role_in_files_leaves_the_platform_without_a_map(tmp_path, provider):
    user = load_residue(
        user_file(tmp_path, "name: sky130hd\nextends: sky130hd\nfiles: {clkgate_map: null}\n")
    )
    root = store_with_sky130hd(tmp_path, provider)
    residue = effective_residue(sky130hd(), user, provider)
    assert "clkgate_map" not in locate(residue, provider, None, root, None).files


def test_a_user_file_cannot_ship_a_file(tmp_path):
    path = user_file(tmp_path, "name: sky130hd\nextends: sky130hd\nshipped: {clkgate_map: x.v}\n")
    with pytest.raises(ResidueError, match="bundled platforms"):
        load_residue(path)


@pytest.mark.parametrize(
    ("shipped", "message"),
    [
        ("{liberty: x.lib}", "not a role that xeda ships"),
        ("{clkgate_map: dir/x.v}", "not a single path component"),
        ("{clkgate_map: ../x.v}", "not a single path component"),
    ],
)
def test_the_key_takes_the_name_of_a_techmap_beside_the_residue(shipped, message):
    table: dict[str, Any] = yaml.safe_load(
        (PACKAGE / "sky130hd" / "platform.yaml").read_text(encoding="utf-8")
    )
    table["shipped"] = yaml.safe_load(shipped)
    with pytest.raises(Exception, match=message):
        Residue.model_validate(table)


def test_a_role_is_in_files_or_in_shipped_never_in_both():
    table: dict[str, Any] = yaml.safe_load(
        (PACKAGE / "sky130hd" / "platform.yaml").read_text(encoding="utf-8")
    )
    table["files"]["clkgate_map"] = "libs.tech/x.v"
    with pytest.raises(Exception, match="given in both `files` and `shipped`"):
        Residue.model_validate(table)


def test_a_residue_that_lists_a_shipped_file_and_has_no_package_directory_refuses_to_locate():
    table: dict[str, Any] = yaml.safe_load(
        (PACKAGE / "sky130hd" / "platform.yaml").read_text(encoding="utf-8")
    )
    with pytest.raises(ValueError, match="only a bundled platform"):
        Residue.model_validate(table).shipped_paths()


# ------------------------------------------------------------------------------ the flow


def fake_bundled_tiny(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `tiny` a bundled platform that ships the clock gate map of `sky130hd`."""
    from xeda.flow import asic
    from xeda.platforms import residue as residue_module

    from .tiny_platform import TINY_RESIDUE

    table = yaml.safe_load(TINY_RESIDUE.read_text(encoding="utf-8"))
    del table["files"]["clkgate_map"]
    table["shipped"] = {"clkgate_map": "cells_clkgate.v"}
    bundled = residue_module.Residue.model_validate(table)
    bundled._package_directory = GATE.parent
    monkeypatch.setattr(asic, "_bundled_residue", lambda name: bundled)


def test_the_flow_reads_the_shipped_map_with_the_defines_and_registers_it(tmp_path, monkeypatch):
    from .tiny_platform import tiny_store

    use_fake_asic_tools(monkeypatch, tmp_path / "bin")
    fake_bundled_tiny(monkeypatch)
    store = tiny_store(tmp_path)
    design_root = tmp_path / "design"
    design_root.mkdir()
    (design_root / "top.v").write_text(CLOCK_GATE)
    design = Design(
        name="top",
        design_root=design_root,
        rtl={"sources": ["top.v"], "top": "top", "clocks": [{"port": "clk"}]},
    )
    user = user_file(tmp_path, "name: tiny\nextends: tiny\n")
    settings = {
        "platform_file": str(user),
        "pdk_root": str(store),
        "clocks": {"main_clock": {"period": "2 ns"}},
        "defines": {"OPENROAD_CLKGATE": None},
    }
    flow = DefaultRunner(tmp_path / "xeda_run", display_results=False).launch_flow(
        Yosys, design, settings
    )
    assert flow.succeeded
    script = (flow.run_path / "yosys_synth.ys").read_text()
    assert f"read_verilog -defer -DOPENROAD_CLKGATE {GATE}\n" in script
    assert GATE.resolve() in registered_input_files(flow)
    assert located_input_files(flow) == registered_input_files(flow)
