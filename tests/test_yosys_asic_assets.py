"""The operator-wrapping script of OpenROAD-flow-scripts ships with its license.

The Tcl script only defines procedures when it is sourced. Its check is line based: every
non-blank line that is not a comment and starts in column 0 must start with `proc ` or be the
closing `}` of a procedure.
"""

import re
import tomllib
from fnmatch import fnmatchcase
from importlib import resources
from pathlib import Path

REPO = Path(__file__).parent.parent
ASSETS = REPO / "src" / "xeda" / "flows" / "yosys" / "data" / "asic"
ORFS_COMMIT = "6b305a712966be72d8908695c04398acdbe273ca"
ORFS_SCRIPTS = (
    "https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts/blob/"
    f"{ORFS_COMMIT}/flow/scripts/"
)
TCL = "synth_wrap_operators.tcl"
BOOTH = "synth_wrap_operators-booth.v"
NOTICE = "NOTICE.md"


def matches(pattern: str, relative: str) -> bool:
    """Whether the package-data glob `pattern` selects `relative` (a `*` stays in one directory)."""
    parts, wanted = relative.split("/"), pattern.split("/")
    return len(parts) == len(wanted) and all(map(fnmatchcase, parts, wanted))


def test_the_assets_and_the_notice_are_package_resources() -> None:
    for name in (TCL, BOOTH, NOTICE):
        resource = resources.files("xeda.flows.yosys").joinpath(f"data/asic/{name}")
        assert resource.is_file(), name


def test_each_asset_names_its_source_in_the_first_line() -> None:
    tcl_first = (ASSETS / TCL).read_text().splitlines()[0]
    booth_first = (ASSETS / BOOTH).read_text().splitlines()[0]
    assert tcl_first == (f"# Source: {ORFS_SCRIPTS}{TCL}; license: BSD-3-Clause (see NOTICE.md)")
    assert booth_first == (
        f"// Source: {ORFS_SCRIPTS}{BOOTH}; license: BSD-3-Clause (see NOTICE.md)"
    )


def test_the_packaging_selects_each_asset_and_the_notice() -> None:
    setuptools = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["setuptools"]
    include = setuptools["package-data"]["xeda.flows.yosys"]
    for name in (TCL, BOOTH, NOTICE):
        relative = f"data/asic/{name}"
        assert any(matches(pattern, relative) for pattern in include), relative


def test_no_asset_reads_an_environment_variable() -> None:
    for path in sorted(ASSETS.iterdir()):
        assert "::env" not in path.read_text(), path.name


def test_the_notice_names_both_files_and_holds_the_license() -> None:
    notice = (ASSETS / NOTICE).read_text()
    assert TCL in notice
    assert BOOTH in notice
    assert ORFS_COMMIT in notice
    assert "BSD 3-Clause" in notice
    # The license text of OpenROAD-flow-scripts, copied verbatim.
    assert "Copyright (c) 2018-2023, The Regents of the University of California" in notice
    assert "Redistribution and use in source and binary forms, with or without" in notice
    assert "THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS" in notice
    assert "OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE." in notice


def test_the_tcl_script_only_defines_procedures_at_top_level() -> None:
    text = (ASSETS / TCL).read_text()
    procs = {match.group(1) for match in re.finditer(r"^proc (\S+) \{", text, re.MULTILINE)}
    assert {"wrap_operators", "design_has_extracted_operators"} <= procs
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#") or line[0] in " \t":
            continue
        assert line.startswith("proc ") or line == "}", f"{TCL}:{number}: {line!r}"
