"""Loading a project file never changes the working directory.

A relative path in a project file resolves against the directory of that file. These tests start
a child Python process whose working directory is another directory, and an audit hook in the
child records every `os.chdir`.
"""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

PROJECT = """\
designs:
  - name: plain
    rtl.sources: [src/top.v]
    rtl.top: top
  - name: nested
    design_root: sub
    rtl.sources: [a.v]
    rtl.top: a
flows:
  yosys:
    clocks.main_clock.period: 4 ns
"""

CHILD = textwrap.dedent("""
    import json, os, sys
    from pathlib import Path

    changes = []
    sys.addaudithook(lambda event, args: changes.append((event, [str(a) for a in args]))
                     if event == "os.chdir" else None)
    from xeda.xedaproject import XedaProject

    project_file = Path(sys.argv[1])
    start = os.getcwd()
    project = XedaProject.from_file(project_file)
    during_load = list(changes)
    reached = {"cwd_after_load": os.getcwd()}
    reached["root_path"] = str(project.root_path)
    reached["names"] = project.design_names
    # Building a design is not the loader's part: only the load is asserted to change nothing.
    for name in ("plain", "nested"):
        try:
            design = project.get_design(name)
            reached[name] = {
                "design_root": str(design.root),
                "sources": [str(source.path) for source in design.rtl.sources],
            }
        except Exception as error:
            reached[name] = {"error": str(error)}
    Path(sys.argv[2]).write_text(json.dumps({"start": start, "during_load": during_load, **reached}))
    """)


def _load_from_elsewhere(tmp_path: Path, project_file: Path) -> dict:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = tmp_path / "result.json"
    done = subprocess.run(
        [sys.executable, "-c", CHILD, str(project_file), str(result)],
        cwd=elsewhere,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(result.read_text())


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "sub").mkdir()
    (root / "src" / "top.v").write_text("module top; endmodule\n")
    (root / "sub" / "a.v").write_text("module a; endmodule\n")
    (root / "xedaproject.yaml").write_text(PROJECT)
    return root / "xedaproject.yaml"


def test_loading_a_project_from_another_directory_changes_no_directory(tmp_path):
    reached = _load_from_elsewhere(tmp_path, _project(tmp_path))
    assert reached["during_load"] == []
    assert reached["cwd_after_load"] == reached["start"]
    assert Path(reached["start"]).name == "elsewhere"


def test_the_root_of_a_project_is_the_directory_of_its_file(tmp_path):
    project_file = _project(tmp_path)
    reached = _load_from_elsewhere(tmp_path, project_file)
    assert Path(reached["root_path"]) == project_file.parent.resolve()
    assert reached["names"] == ["plain", "nested"]


def test_the_sources_of_a_design_resolve_against_the_directory_of_the_project_file(tmp_path):
    project_file = _project(tmp_path)
    root = project_file.parent.resolve()
    reached = _load_from_elsewhere(tmp_path, project_file)
    assert Path(reached["plain"]["design_root"]) == root
    assert [Path(p) for p in reached["plain"]["sources"]] == [root / "src" / "top.v"]


def test_a_relative_design_root_of_an_entry_resolves_against_the_directory_of_the_project_file(
    tmp_path,
):
    project_file = _project(tmp_path)
    root = project_file.parent.resolve()
    reached = _load_from_elsewhere(tmp_path, project_file)
    assert Path(reached["nested"]["design_root"]) == root / "sub"
    assert [Path(p) for p in reached["nested"]["sources"]] == [root / "sub" / "a.v"]


def test_a_relative_root_path_is_read_once_when_the_project_is_built(tmp_path):
    """`XedaProject(root_path=...)` reads the working directory once, when it receives a relative
    root: a design taken later from another directory still resolves against that root."""
    (tmp_path / "first" / "proj" / "src").mkdir(parents=True)
    (tmp_path / "first" / "proj" / "src" / "top.v").write_text("module top; endmodule\n")
    (tmp_path / "second").mkdir()
    script = textwrap.dedent("""
        import json, os, sys
        from pathlib import Path
        from xeda.xedaproject import XedaProject

        os.chdir(sys.argv[1])  # this child process only
        project = XedaProject(
            designs=[{"name": "d", "rtl": {"sources": ["src/top.v"], "top": "top"}}],
            root_path=Path("proj"),
        )
        os.chdir(sys.argv[2])
        design = project.get_design("d")
        print(json.dumps([str(project.root_path), str(design.root)]))
        """)
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "first"), str(tmp_path / "second")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    root = str((tmp_path / "first" / "proj").resolve())
    assert [str(Path(p).resolve()) for p in json.loads(result.stdout)] == [root, root]
    assert Path(json.loads(result.stdout)[0]).is_absolute()


@pytest.mark.parametrize("value", [123, False, 0, ""])
def test_a_design_root_that_is_not_a_path_is_reported_as_a_design_error(tmp_path, value):
    """A `design_root` of a project entry that is not a path is a design error that names the
    key: not a bare `TypeError`, and not the project's directory in its place."""
    from xeda.design import DesignValidationError
    from xeda.xedaproject import XedaProject

    project = XedaProject(
        designs=[{"name": "d", "design_root": value, "rtl": {"sources": [], "top": "top"}}],
        root_path=tmp_path,
    )
    with pytest.raises(DesignValidationError, match="design_root"):
        project.get_design("d")
