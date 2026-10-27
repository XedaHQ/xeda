"""`XedaProject.from_file` must reject ambiguous or malformed project files instead of silently
picking one of two accepted spellings or dropping bad entries.
"""

import json
import re
from pathlib import Path

import pytest

from xeda.xedaproject import (
    PROJECT_FILE_NAMES,
    ProjectFileError,
    XedaProject,
    find_default_xedaproject,
)

from .project_files import PROJECT_FILE, TOML_PROJECT_FILE

DESIGN = """
designs:
  name: d
  rtl: {sources: [top.vhd], top: top}
"""

#: the same design as `DESIGN`, in TOML
TOML_DESIGN = """
[designs]
name = 'd'
[designs.rtl]
sources = ['top.vhd']
top = 'top'
"""


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content)
    return path


@pytest.mark.parametrize("content", ["", "[]\n", "just text\n"])
def test_a_project_file_that_holds_no_mapping_is_refused_by_name(tmp_path, content):
    path = _write(tmp_path, PROJECT_FILE, content)
    message = re.escape(f"Invalid project file {path}: it must hold a nonempty mapping")
    with pytest.raises(ValueError, match=message):
        XedaProject.from_file(path)


def test_only_flows_key_works(tmp_path):
    path = _write(
        tmp_path,
        PROJECT_FILE,
        DESIGN + """
flows:
  ghdl_sim: {werror: true}
""",
    )

    project = XedaProject.from_file(path)

    assert project.flows == {"ghdl_sim": {"werror": True}}
    assert len(project.designs) == 1


def test_a_toml_project_file_is_still_accepted(tmp_path):
    """TOML stays supported: the same project, written as `xedaproject.toml`, is discovered
    and loads to the same flows and design as the YAML one."""
    (tmp_path / TOML_PROJECT_FILE).write_text(TOML_DESIGN + "[flows.ghdl_sim]\nwerror = true\n")
    path = find_default_xedaproject(tmp_path)
    assert path == tmp_path / TOML_PROJECT_FILE
    toml_project = XedaProject.from_file(path)
    (tmp_path / "y").mkdir()
    yaml_project = XedaProject.from_file(
        _write(tmp_path / "y", PROJECT_FILE, DESIGN + "flows:\n  ghdl_sim: {werror: true}\n")
    )
    assert toml_project.flows == yaml_project.flows == {"ghdl_sim": {"werror": True}}
    assert toml_project.designs == yaml_project.designs


def test_the_yaml_spelling_is_what_discovery_finds_first(tmp_path):
    assert PROJECT_FILE_NAMES[0] == PROJECT_FILE == "xedaproject.yaml"
    (tmp_path / PROJECT_FILE).write_text(DESIGN)
    assert find_default_xedaproject(tmp_path) == tmp_path / "xedaproject.yaml"


@pytest.mark.parametrize(
    ("singular", "plural"), [("flow", "flows"), ("design", "designs")], ids=["flow", "design"]
)
def test_the_singular_key_of_a_project_was_removed(tmp_path, singular, plural):
    path = _write(tmp_path, PROJECT_FILE, f"{singular}: {{}}\n")

    with pytest.raises(ProjectFileError, match=f"`{singular}` was removed: use `{plural}`"):
        XedaProject.from_file(path)


def test_a_non_mapping_design_entry_names_its_position_and_type(tmp_path):
    path = _write(
        tmp_path,
        PROJECT_FILE,
        """
designs: [a.yaml]
flows:
  ghdl_sim: {werror: true}
""",
    )

    with pytest.raises(ValueError, match=r"0.*str"):
        XedaProject.from_file(path)


def test_a_project_file_suffix_is_read_as_strictly_as_a_design_file_s(tmp_path):
    """One suffix table for both kinds of file: `.yml` is YAML here too, and a mis-cased suffix
    is rejected naming the right spelling rather than read as whatever it resembles."""
    yml = _write(tmp_path, "xedaproject.yml", "flows:\n  ghdl_sim:\n    werror: true\n")
    assert XedaProject.from_file(yml, skip_designs=True).flows == {"ghdl_sim": {"werror": True}}
    shouting = _write(tmp_path, "xedaproject.TOML", "[flows.ghdl_sim]\nwerror = true\n")
    with pytest.raises(ValueError, match=r"case-sensitive.*'\.toml'"):
        XedaProject.from_file(shouting, skip_designs=True)


def test_default_project_discovery_rejects_multiple_formats(tmp_path, monkeypatch):
    from xeda.design import Design
    from xeda.flow_runner import DefaultRunner
    from xeda.flow_runner.default_runner import ProjectFileError

    (tmp_path / PROJECT_FILE).write_text("flows: {}\n")
    (tmp_path / TOML_PROJECT_FILE).write_text("workspace = {}\n")
    monkeypatch.chdir(tmp_path)
    runner = DefaultRunner(tmp_path / "run")

    with pytest.raises(ProjectFileError) as raised:
        runner._request("ghdl_sim", Design(name="d"))

    message = str(raised.value)
    assert PROJECT_FILE in message
    assert TOML_PROJECT_FILE in message
    assert "keep one" in message


@pytest.mark.parametrize("name", PROJECT_FILE_NAMES)
def test_default_project_discovery_accepts_each_supported_suffix(tmp_path, name):
    path = tmp_path / name
    path.write_text("flows: {}\n" if path.suffix != ".toml" else "workspace = {}\n")

    assert find_default_xedaproject(tmp_path) == path


@pytest.mark.parametrize(
    ("text", "suggestion"),
    [
        ("flowz: {}\n", "flows"),
        ("desings:\n  - name: d\n", "designs"),
        ("flow_settings: {}\n", None),
        ("workspace: {}\n", None),
    ],
)
def test_a_project_key_that_nothing_reads_is_refused_naming_the_accepted_keys(
    tmp_path, text, suggestion
):
    """A key at the root of a project file that is not one of its keys used to be dropped without
    a word."""
    path = _write(tmp_path, PROJECT_FILE, DESIGN + text)

    with pytest.raises(ValueError) as raised:
        XedaProject.from_file(path)

    message = str(raised.value)
    assert "unknown key(s)" in message and "'designs', 'flows'" in message
    assert text.split(":")[0] in message
    if suggestion:
        assert f"did you mean '{suggestion}'?" in message


def test_the_accepted_keys_of_a_project_are_its_designs_and_its_flows(tmp_path):
    body = "designs:\n  - name: d\n    rtl: {sources: [top.vhd], top: top}\n"
    body += "flows:\n  ghdl_sim: {werror: true}\n"
    project = XedaProject.from_file(_write(tmp_path, PROJECT_FILE, body))
    assert project.flows == {"ghdl_sim": {"werror": True}}


def test_a_dotted_key_at_the_root_of_a_project_reaches_its_flows(tmp_path):
    path = _write(
        tmp_path, PROJECT_FILE, DESIGN + "flows.vivado_synth.clocks.main_clock.period: 5 ns\n"
    )

    assert XedaProject.from_file(path).flows == {
        "vivado_synth": {"clocks": {"main_clock": {"period": "5 ns"}}}
    }


CONFLICT = {"name": "d", "rtl": {"sources": ["top.vhd"], "top": "top"}, "tb": 3, "tb.top": "x"}


def _project_with(tmp_path: Path, entry: dict, *, as_list: bool) -> Path:
    return _write(
        tmp_path,
        PROJECT_FILE,
        json.dumps({"designs": [entry] if as_list else entry, "flows": {"ghdl_sim": {}}}),
    )


@pytest.mark.parametrize("as_list", [False, True], ids=["one design", "list of designs"])
def test_a_design_entry_is_read_only_when_designs_are_wanted(tmp_path, as_list):
    """Without the designs (`skip_designs`), an entry is not looked at, whichever form it has."""
    path = _project_with(tmp_path, CONFLICT, as_list=as_list)

    project = XedaProject.from_file(path, skip_designs=True)

    assert project.designs == [] and project.flows == {"ghdl_sim": {}}


@pytest.mark.parametrize("as_list", [False, True], ids=["one design", "list of designs"])
def test_a_key_conflict_in_a_design_entry_names_the_file_and_the_entry(tmp_path, as_list):
    path = _project_with(tmp_path, CONFLICT, as_list=as_list)

    with pytest.raises(ValueError) as raised:
        XedaProject.from_file(path)

    message = str(raised.value)
    assert str(path) in message and "design entry 0 (d)" in message
    assert "`tb` is set to a value, and `tb.top`" in message


def test_the_runner_reports_that_conflict_as_a_project_file_error(tmp_path):
    from xeda.flow_runner import DefaultRunner
    from xeda.xedaproject import ProjectFileError

    path = _project_with(tmp_path, CONFLICT, as_list=True)
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)

    with pytest.raises(ProjectFileError, match=r"Cannot load project file .*design entry 0 \(d\)"):
        runner.plan("ghdl_sim", None, xedaproject=str(path))


def test_a_dotted_key_cannot_reach_into_the_design_entries(tmp_path):
    path = _write(tmp_path, PROJECT_FILE, DESIGN + "designs.description: text\n")

    with pytest.raises(ValueError, match=r"cannot reach into the design entries"):
        XedaProject.from_file(path)
