"""The operator-wrapping script of OpenROAD-flow-scripts ships with its license.

The Tcl script only defines procedures when it is sourced. Its check tracks the brace depth: every
non-blank, non-comment line that starts at depth 0 must start with `proc `, and a closing `}`
must be the only text of the line that closes a procedure.
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


LIBRELANE_COMMIT = "e39720fff016c354d822885b2d1acd5508010653"
LIBRELANE_FILE = "librelane/scripts/pyosys/construct_abc_script.py"


def test_the_notice_names_librelane_its_file_its_commit_and_holds_the_apache_license() -> None:
    notice = (ASSETS / NOTICE).read_text()
    assert LIBRELANE_FILE in notice
    assert LIBRELANE_COMMIT in notice
    assert "Copyright 2020-2024 Efabless Corporation" in notice
    assert "Apache License, Version 2.0" in notice
    # the license text of LibreLane, copied verbatim
    assert "                                 Apache License\n" in notice
    assert "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION" in notice
    assert "4. Redistribution." in notice
    assert "END OF TERMS AND CONDITIONS" in notice
    assert "limitations under the License." in notice


def test_the_module_that_composes_the_strategies_names_its_source_and_its_license() -> None:
    module = (REPO / "src" / "xeda" / "flows" / "yosys" / "abc_strategies.py").read_text()
    assert LIBRELANE_FILE in module and LIBRELANE_COMMIT in module
    assert "Apache License 2.0" in module and "NOTICE.md" in module
    assert "Efabless Corporation" in module


def test_the_notice_names_the_parts_of_the_scripts_that_the_python_modules_carry() -> None:
    notice = (ASSETS / NOTICE).read_text()
    for name in ("abc_speed_gia_only.script", "abc_retime.script", "get_module_names"):
        assert name in notice, name


def tcl_top_level_problems(text: str) -> list[str]:
    """The lines of a Tcl text that run outside a procedure, as `line number: text` strings.

    The depth is the count of unescaped `{` minus `}` before a line. A character after a
    backslash is skipped, so `\\{` and `\\$` do not count. A comment line at depth 0 is skipped.
    A line that starts at depth 0 is a problem unless it starts with `proc ` (any column), and a
    `}` that closes depth 1 to 0 is the only text of its line.
    """
    depth = 0
    problems = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or (depth == 0 and stripped.startswith("#")):
            continue
        start = depth
        index = 0
        while index < len(line):
            character = line[index]
            if character == "\\":
                index += 2
                continue
            if character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
            index += 1
        if start == 0 and not stripped.startswith("proc "):
            problems.append(f"{number}: {line}")
        elif start == 1 and depth == 0 and stripped != "}":
            problems.append(f"{number}: {line}")
    return problems


def test_the_tcl_script_only_defines_procedures_at_top_level() -> None:
    """The Tcl script only defines procedures: see `tcl_top_level_problems` for the rule."""
    text = (ASSETS / TCL).read_text()
    procs = {match.group(1) for match in re.finditer(r"^proc (\S+) \{", text, re.MULTILINE)}
    assert {"wrap_operators", "design_has_extracted_operators"} <= procs
    assert tcl_top_level_problems(text) == []


def test_the_top_level_check_reports_an_indented_command_after_a_procedure() -> None:
    text = "proc p { a } {\n  set y [list {a} \\{]\n}\n  set x 1\n"
    assert tcl_top_level_problems(text) == ["4:   set x 1"]
    assert tcl_top_level_problems("proc p {} {\n  set y 2\n}\n") == []
