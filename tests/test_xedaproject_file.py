"""`XedaProject.from_file` must reject ambiguous or malformed project files instead of silently
picking one of two accepted spellings or dropping bad entries.
"""

from pathlib import Path

import pytest

from xeda.xedaproject import PROJECT_FILE_NAMES, XedaProject, find_default_xedaproject

from .project_files import PROJECT_FILE, TOML_PROJECT_FILE

DESIGN = """
design:
  name: d
  rtl: {sources: [top.vhd], top: top}
"""

#: the same design as `DESIGN`, in TOML
TOML_DESIGN = """
[design]
name = 'd'
[design.rtl]
sources = ['top.vhd']
top = 'top'
"""


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content)
    return path


def test_both_flow_and_flows_is_a_value_error(tmp_path):
    path = _write(
        tmp_path,
        PROJECT_FILE,
        DESIGN + """
flow:
  ghdl_sim: {warn_error: true}
flows:
  ghdl_sim: {warn_error: true}
""",
    )

    with pytest.raises(ValueError, match=r"flows.*flow"):
        XedaProject.from_file(path)


def test_only_flows_key_works(tmp_path):
    path = _write(
        tmp_path,
        PROJECT_FILE,
        DESIGN + """
flows:
  ghdl_sim: {warn_error: true}
""",
    )

    project = XedaProject.from_file(path)

    assert project.flows == {"ghdl_sim": {"warn_error": True}}
    assert len(project.designs) == 1


def test_a_toml_project_file_is_still_accepted(tmp_path):
    """TOML stays supported: the same project, written as `xedaproject.toml`, is discovered
    and loads to the same flows and design as the YAML one."""
    (tmp_path / TOML_PROJECT_FILE).write_text(TOML_DESIGN + "[flows.ghdl_sim]\nwarn_error = true\n")
    path = find_default_xedaproject(tmp_path)
    assert path == tmp_path / TOML_PROJECT_FILE
    toml_project = XedaProject.from_file(path)
    (tmp_path / "y").mkdir()
    yaml_project = XedaProject.from_file(
        _write(tmp_path / "y", PROJECT_FILE, DESIGN + "flows:\n  ghdl_sim: {warn_error: true}\n")
    )
    assert toml_project.flows == yaml_project.flows == {"ghdl_sim": {"warn_error": True}}
    assert toml_project.designs == yaml_project.designs


def test_the_yaml_spelling_is_what_discovery_finds_first(tmp_path):
    assert PROJECT_FILE_NAMES[0] == PROJECT_FILE == "xedaproject.yaml"
    (tmp_path / PROJECT_FILE).write_text(DESIGN)
    assert find_default_xedaproject(tmp_path) == tmp_path / "xedaproject.yaml"


def test_only_flow_key_works(tmp_path):
    path = _write(
        tmp_path,
        PROJECT_FILE,
        DESIGN + """
flow:
  ghdl_sim: {warn_error: true}
""",
    )

    project = XedaProject.from_file(path)

    assert project.flows == {"ghdl_sim": {"warn_error": True}}


def test_both_design_and_designs_is_a_value_error(tmp_path):
    path = _write(
        tmp_path,
        PROJECT_FILE,
        """
design:
  - {name: d, rtl: {sources: [top.vhd], top: top}}
designs:
  - {name: d2, rtl: {sources: [top.vhd], top: top}}
""",
    )

    with pytest.raises(ValueError, match=r"designs.*design"):
        XedaProject.from_file(path)


def test_a_non_mapping_design_entry_names_its_position_and_type(tmp_path):
    path = _write(
        tmp_path,
        PROJECT_FILE,
        """
design: [a.yaml]
flows:
  ghdl_sim: {warn_error: true}
""",
    )

    with pytest.raises(ValueError, match=r"0.*str"):
        XedaProject.from_file(path)


def test_a_project_file_suffix_is_read_as_strictly_as_a_design_file_s(tmp_path):
    """One suffix table for both kinds of file: `.yml` is YAML here too, and a mis-cased suffix
    is rejected naming the right spelling rather than read as whatever it resembles."""
    yml = _write(tmp_path, "xedaproject.yml", "flows:\n  ghdl_sim:\n    warn_error: true\n")
    assert XedaProject.from_file(yml, skip_designs=True).flows == {"ghdl_sim": {"warn_error": True}}
    shouting = _write(tmp_path, "xedaproject.TOML", "[flows.ghdl_sim]\nwarn_error = true\n")
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
