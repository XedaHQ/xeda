"""Loading a design never changes the working directory and never depends on it.

A relative path in a design resolves against the design root. Each test loads a design in a child
Python process whose working directory is another directory (with files of the same names, so a
path resolved against the wrong directory finds the wrong file instead of none). The child
records every `os.chdir` through an audit hook. The tests start a child process because the
working directory belongs to the whole process, and a test must not change it.
"""

import json
import subprocess
import sys
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest

HARNESS = """\
import json, sys

chdirs = []


def _audit(event, args):
    if event == "os.chdir":
        chdirs.append(str(args[0]))


sys.addaudithook(_audit)

from pathlib import Path
from xeda import Design
"""


def _write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(text))
    return path


def _elsewhere(tmp_path: Path, *names: str) -> Path:
    """Return a directory to run the child in, holding a decoy for each of `names`."""
    here = tmp_path / "elsewhere"
    here.mkdir()
    for name in names:
        _write(here / name, "// the decoy, not the design's file\n")
    return here


def _load_in_child(here: Path, body: str, **names: Any) -> dict[str, Any]:
    """Run `body` in a child process started in `here`; return the `summary` it builds.

    `body` sees the paths in `names` as `Path` objects and sets `summary`. The result also holds
    every `os.chdir` the child made, as `chdirs`."""
    values = "".join(f"{key} = Path({str(value)!r})\n" for key, value in names.items())
    script = (
        HARNESS + values + dedent(body) + "\nprint(json.dumps({**summary, 'chdirs': chdirs}))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script], cwd=here, check=False, capture_output=True, text=True
    )
    assert done.returncode == 0, f"the child failed:\n{done.stderr}"
    summary = json.loads(done.stdout.strip().splitlines()[-1])
    assert isinstance(summary, dict)
    return summary


def _files(paths: list[str]) -> list[Path]:
    return [Path(p) for p in paths]


FROM_FILE = """
design = Design.from_file(design_file)
summary = {
    "rtl": [str(s.file) for s in design.rtl.sources],
    "tb": [str(s.file) for s in design.tb.sources],
    "root": str(design.design_root),
    "parameters": dict(design.rtl.parameters),
}
"""


def test_a_design_file_resolves_every_relative_path_against_its_directory(tmp_path):
    root = tmp_path / "design"
    design_file = _write(
        root / "design.yaml",
        """\
        name: demo
        rtl:
          sources:
            - src/a.vhd
            - $DESIGN_ROOT/src/b.v
            - {file: src/c.sv}
            - {path: src/later.v}
          top: a
          parameters:
            INIT: {file: data/init.hex}
        tb:
          sources: [tb/tb.vhd]
          top: tb
        """,
    )
    for name in ("src/a.vhd", "src/b.v", "src/c.sv", "src/later.v", "tb/tb.vhd", "data/init.hex"):
        _write(root / name, "-- design\n")
    here = _elsewhere(tmp_path, "src/a.vhd", "src/b.v", "src/c.sv", "tb/tb.vhd", "data/init.hex")
    root = root.resolve()

    summary = _load_in_child(here, FROM_FILE, design_file=design_file)

    assert summary["chdirs"] == []
    assert _files(summary["rtl"]) == [
        root / "src" / "a.vhd",
        root / "src" / "b.v",
        root / "src" / "c.sv",
        root / "src" / "later.v",
    ]
    assert _files(summary["tb"]) == [root / "tb" / "tb.vhd"]
    assert Path(summary["root"]) == root
    assert Path(summary["parameters"]["INIT"]) == root / "data" / "init.hex"


def test_source_patterns_match_in_the_design_directory(tmp_path):
    root = tmp_path / "design"
    design_file = _write(
        root / "design.yaml",
        """\
        name: patterns
        rtl:
          sources: ["src/*.v", "$DESIGN_ROOT/lib/*.vhd"]
          top: a
        tb:
          sources: ["tb/*.sv"]
          top: tb
        """,
    )
    for name in ("src/a.v", "src/b.v", "lib/x.vhd", "tb/t1.sv", "tb/t2.sv"):
        _write(root / name, "// design\n")
    here = _elsewhere(tmp_path, "src/a.v", "src/zz.v", "tb/t0.sv")
    root = root.resolve()

    summary = _load_in_child(here, FROM_FILE, design_file=design_file)

    assert summary["chdirs"] == []
    assert _files(summary["rtl"]) == [
        root / "src" / "a.v",
        root / "src" / "b.v",
        root / "lib" / "x.vhd",
    ]
    assert _files(summary["tb"]) == [root / "tb" / "t1.sv", root / "tb" / "t2.sv"]


def test_dependencies_resolve_against_their_own_directories(tmp_path):
    app = tmp_path / "app"
    design_file = _write(
        app / "app.yaml",
        """\
        name: app
        dependencies: [../lib/lib.yaml]
        rtl:
          sources: [top.v]
          top: top
        """,
    )
    _write(app / "top.v", "// app\n")
    _write(
        tmp_path / "lib" / "lib.yaml",
        """\
        name: lib
        dependencies: [../core/core.yaml]
        rtl:
          sources: [lib.v, "rtl/*.v"]
          top: lib
        """,
    )
    for name in ("lib.v", "rtl/r1.v"):
        _write(tmp_path / "lib" / name, "// lib\n")
    _write(
        tmp_path / "core" / "core.yaml",
        """\
        name: core
        rtl:
          sources: [core.v]
          top: core
        """,
    )
    _write(tmp_path / "core" / "core.v", "// core\n")
    here = _elsewhere(tmp_path, "top.v", "lib.v", "core.v")
    base = tmp_path.resolve()

    summary = _load_in_child(here, FROM_FILE, design_file=design_file)

    assert summary["chdirs"] == []
    assert Path(summary["root"]) == base / "app"
    assert _files(summary["rtl"]) == [
        base / "core" / "core.v",
        base / "lib" / "lib.v",
        base / "lib" / "rtl" / "r1.v",
        base / "app" / "top.v",
    ]


MAKE_SOURCE = """\
import os
from pathlib import Path

Path(os.environ["DESIGN_ROOT"], "ran_in.txt").write_text(os.getcwd())
Path("out").mkdir(exist_ok=True)
Path("out", "top.v").write_text("module top; endmodule\\n")
"""

#: How a design states a generator that writes `out/top.v` from its own directory.
GENERATORS = {
    "command": "generator: {command: '{python} make_src.py', sources: [make_src.py]}",
    "executable": (
        "generator: {executable: '{python}', args: [make_src.py], sources: ['make_src.py', "
        "'specs/*.txt']}"
    ),
    "shell": "generator: \"'{python}' make_src.py\"",
    "argv": "generator: ['{python}', make_src.py]",
}


@pytest.mark.parametrize("spelling", GENERATORS)
def test_a_generator_runs_in_the_design_root(tmp_path, spelling):
    root = tmp_path / "design"
    generator = GENERATORS[spelling].replace("{python}", sys.executable)
    design_file = _write(
        root / "design.yaml",
        f"""\
        name: generated
        rtl:
          sources: [{{path: out/top.v}}]
          top: top
          {generator}
        """,
    )
    _write(root / "make_src.py", MAKE_SOURCE)
    _write(root / "specs" / "one.txt", "one\n")
    here = _elsewhere(tmp_path, "specs/zero.txt")
    root = root.resolve()

    summary = _load_in_child(here, FROM_FILE, design_file=design_file)

    assert summary["chdirs"] == []
    assert _files(summary["rtl"]) == [root / "out" / "top.v"]
    assert (root / "out" / "top.v").read_text() == "module top; endmodule\n"
    assert Path((root / "ran_in.txt").read_text()).resolve() == root
    assert not (here / "out").exists()


def test_a_generator_with_a_relative_cwd_runs_below_the_design_root(tmp_path):
    root = tmp_path / "design"
    design_file = _write(
        root / "design.yaml",
        f"""\
        name: generated
        rtl:
          sources: [{{path: out/top.v}}]
          top: top
          generator:
            executable: '{sys.executable}'
            args: [make.py]
            cwd: tools
        """,
    )
    _write(
        root / "tools" / "make.py",
        """\
        from pathlib import Path

        Path("../out").mkdir(exist_ok=True)
        Path("../out/top.v").write_text("module top; endmodule\\n")
        """,
    )
    here = _elsewhere(tmp_path, "tools/make.py")
    root = root.resolve()

    summary = _load_in_child(here, FROM_FILE, design_file=design_file)

    assert summary["chdirs"] == []
    assert _files(summary["rtl"]) == [root / "out" / "top.v"]
    assert not (here / "out").exists()


def test_a_design_built_from_a_dict_resolves_against_its_design_root(tmp_path):
    root = tmp_path / "design"
    for name in ("a.v", "src/b.v", "src/c.v", "tb.sv"):
        _write(root / name, "// design\n")
    here = _elsewhere(tmp_path, "a.v", "src/b.v", "tb.sv")
    root = root.resolve()
    body = """
    designs = [
        Design(
            design_root=str(design_root),
            name="direct",
            rtl={"sources": ["a.v", "src/*.v"], "top": "a"},
            tb={"sources": ["tb.sv"], "top": "tb"},
        ),
        Design(
            design_root="../design",
            name="relative_root",
            rtl={"sources": ["a.v", "src/*.v"], "top": "a"},
            tb={"sources": ["tb.sv"], "top": "tb"},
        ),
    ]
    summary = {
        "designs": [
            {
                "rtl": [str(s.file) for s in d.rtl.sources],
                "tb": [str(s.file) for s in d.tb.sources],
                "root": str(d.design_root),
            }
            for d in designs
        ]
    }
    """

    summary = _load_in_child(here, body, design_root=root)

    assert summary["chdirs"] == []
    assert len(summary["designs"]) == 2
    for loaded in summary["designs"]:
        assert Path(loaded["root"]) == root
        assert _files(loaded["rtl"]) == [root / "a.v", root / "src" / "b.v", root / "src" / "c.v"]
        assert _files(loaded["tb"]) == [root / "tb.sv"]


def test_a_load_leaves_no_root_behind_even_when_it_fails(tmp_path):
    from xeda.design import Design, DesignValidationError, design_relative_root

    root = tmp_path / "design"
    _write(root / "a.v", "// design\n")
    before = design_relative_root()

    Design(design_root=root, name="ok", rtl={"sources": ["a.v"], "top": "a"})
    assert design_relative_root() == before
    with pytest.raises(DesignValidationError):
        Design(design_root=root, name="bad", rtl={"sources": ["missing.v"], "top": "a"})
    assert design_relative_root() == before
