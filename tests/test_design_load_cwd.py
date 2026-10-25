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

from .test_git_dependencies import remote_lib  # noqa: F401 (the fixture)

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

out = Path(os.environ["XEDA_GENERATED"])
(out / "ran_in.txt").write_text(os.getcwd())
(out / "out").mkdir(exist_ok=True)
(out / "out" / "top.v").write_text("module top; endmodule\\n")
"""

#: How a design states a generator that writes `out/top.v` from its own directory.
GENERATORS = {
    "command": "generator: {command: '{python} $DESIGN_ROOT/make_src.py', sources: [make_src.py]}",
    "executable": (
        "generator: {executable: '{python}', args: ['$DESIGN_ROOT/make_src.py'], "
        "sources: ['make_src.py', 'specs/*.txt']}"
    ),
    "shell": 'generator: "\'{python}\' \\"$DESIGN_ROOT/make_src.py\\""',
    "argv": "generator: ['{python}', '$DESIGN_ROOT/make_src.py']",
}

#: Loads the design and runs its generator into a storage under a new run root.
GENERATE = """
import os
from xeda.flow_runner.launch_generation import generate

design = Design.from_file(design_file)
run_root.mkdir()
generation = generate(design, run_root=run_root.resolve(), environ=dict(os.environ))
summary = {
    "declared": [str(s.path) for s in design.rtl.sources],
    "storage": str(generation.storage),
    "matches": {name: list(files) for name, files in generation.matches.items()},
}
"""


@pytest.mark.parametrize("spelling", GENERATORS)
def test_a_generator_runs_in_its_storage_whatever_the_working_directory(tmp_path, spelling):
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

    summary = _load_in_child(
        here, GENERATE, design_file=design_file, run_root=tmp_path / "xeda_run"
    )

    storage = Path(summary["storage"])
    assert summary["chdirs"] == []
    assert summary["declared"] == ["out/top.v"]
    assert summary["matches"] == {"out/top.v": ["out/top.v"]}
    assert (storage / "out" / "top.v").read_text() == "module top; endmodule\n"
    assert Path((storage / "ran_in.txt").read_text()).resolve() == storage.resolve()
    assert not (root / "out").exists() and not (here / "out").exists()


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
            sources: [tools/make.py]
            cwd: tools
        """,
    )
    _write(
        root / "tools" / "make.py",
        """\
        import os
        from pathlib import Path

        out = Path(os.environ["XEDA_GENERATED"])
        (out / "out").mkdir(exist_ok=True)
        (out / "out" / "top.v").write_text("module top; endmodule\\n")
        (out / "ran_in.txt").write_text(os.getcwd())
        """,
    )
    here = _elsewhere(tmp_path, "tools/make.py")
    root = root.resolve()

    summary = _load_in_child(
        here, GENERATE, design_file=design_file, run_root=tmp_path / "xeda_run"
    )

    storage = Path(summary["storage"])
    assert summary["chdirs"] == []
    assert (storage / "out" / "top.v").is_file()
    assert Path((storage / "ran_in.txt").read_text()).resolve() == root / "tools"
    assert not (here / "out").exists() and not (root / "out").exists()


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


def test_a_relative_design_root_in_a_design_file_is_relative_to_that_file(tmp_path):
    project = tmp_path / "project"
    design_file = _write(
        project / "designs" / "design.yaml",
        """\
        name: rooted
        design_root: ../src
        rtl:
          sources: [a.v, "more/*.v"]
          top: a
        """,
    )
    for name in ("a.v", "more/b.v"):
        _write(project / "src" / name, "// design\n")
    here = _elsewhere(tmp_path, "src/a.v", "src/more/b.v")
    root = (project / "src").resolve()

    summary = _load_in_child(here, FROM_FILE, design_file=design_file)

    assert summary["chdirs"] == []
    assert Path(summary["root"]) == root
    assert _files(summary["rtl"]) == [root / "a.v", root / "more" / "b.v"]


def test_a_relative_design_root_in_a_dependency_file_is_relative_to_the_dependency_file(tmp_path):
    design_file = _write(
        tmp_path / "app" / "main.yaml",
        """\
        name: app
        dependencies: [../lib/deep/dep.yaml]
        rtl:
          sources: [top.v]
          top: top
        """,
    )
    _write(tmp_path / "app" / "top.v", "// app\n")
    _write(
        tmp_path / "lib" / "deep" / "dep.yaml",
        """\
        name: dep
        design_root: ../src
        rtl:
          sources: [dep.v]
          top: dep
        """,
    )
    _write(tmp_path / "lib" / "src" / "dep.v", "// dependency\n")
    # Where the root would be if it were relative to the parent's root or the working directory.
    _write(tmp_path / "src" / "dep.v", "// decoy: the parent's root\n")
    here = _elsewhere(tmp_path, "src/dep.v")
    base = tmp_path.resolve()

    summary = _load_in_child(here, FROM_FILE, design_file=design_file)

    assert summary["chdirs"] == []
    assert _files(summary["rtl"]) == [base / "lib" / "src" / "dep.v", base / "app" / "top.v"]


def test_a_missing_local_dependency_names_where_it_looked(tmp_path):
    from xeda.design import Design, DesignValidationError

    root = tmp_path / "design"
    _write(root / "top.v", "// design\n")

    with pytest.raises(DesignValidationError) as raised:
        Design(
            design_root=root,
            name="needy",
            dependencies=["../lib/missing.yaml"],
            rtl={"sources": ["top.v"], "top": "top"},
        )

    assert str(root.resolve() / ".." / "lib" / "missing.yaml") in str(raised.value)


@pytest.mark.usefixtures("remote_lib")
def test_a_git_dependency_clones_relative_to_the_design_root(tmp_path):
    root = tmp_path / "design"
    _write(root / "top.v", "// design\n")
    design_file = _write(
        root / "design.yaml",
        """\
        name: cloning
        rtl:
          sources: [top.v]
          top: top
        dependencies:
          - repo_url: git@fake.invalid:org/lib.git
            design_file: lib.yaml
            clone_dir: clones/lib
        """,
    )
    here = _elsewhere(tmp_path, "top.v")

    summary = _load_in_child(here, FROM_FILE, design_file=design_file)

    assert summary["chdirs"] == []
    assert (root / "clones" / "lib" / ".git").is_dir()
    assert not (here / "clones").exists()
    assert [Path(p).name for p in summary["rtl"]] == ["lib.v", "top.v"]
    assert _files(summary["rtl"])[0] == (root / "clones" / "lib" / "lib.v").resolve()


@pytest.mark.usefixtures("remote_lib")
def test_a_git_cache_clones_relative_to_the_design_root(tmp_path, monkeypatch):
    # The cache path names the host of the URL, so the URL is in the `https` form.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{tmp_path / 'remotes'}/.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://fake.invalid/")
    root = tmp_path / "design"
    _write(root / "top.v", "// design\n")
    design_file = _write(
        root / "design.yaml",
        """\
        name: caching
        rtl:
          sources: [top.v]
          top: top
        dependencies:
          - uri: git+https://fake.invalid/org/lib.git#lib.yaml
            local_cache: cache
        """,
    )
    here = _elsewhere(tmp_path, "top.v")

    summary = _load_in_child(here, FROM_FILE, design_file=design_file)

    assert summary["chdirs"] == []
    clones = list((root / "cache").glob("*/*/.git"))
    assert len(clones) == 1
    assert not (here / "cache").exists()
    assert [Path(p).name for p in summary["rtl"]] == ["lib.v", "top.v"]
