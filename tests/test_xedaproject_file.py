"""Finding the project file: the conventional names, YAML first, and one file at a time.

`tests/test_project.py` holds the tests of reading it.
"""

import pytest

from xeda.errors import ProjectError
from xeda.project import Project
from xeda.xedaproject import PROJECT_FILE_NAMES, find_default_xedaproject

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


def test_a_toml_project_file_is_still_accepted(tmp_path):
    """TOML stays supported: the same project, written as `xedaproject.toml`, is discovered
    and loads to the same flows and design as the YAML one."""
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / TOML_PROJECT_FILE).write_text(
        TOML_DESIGN + "[flows.ghdl_sim]\nwerror = true\n"
    )
    path = find_default_xedaproject(tmp_path / "t")
    assert path == tmp_path / "t" / TOML_PROJECT_FILE
    toml_project = Project.load(path)
    (tmp_path / "y").mkdir()
    (tmp_path / "y" / PROJECT_FILE).write_text(DESIGN + "flows:\n  ghdl_sim: {werror: true}\n")
    yaml_project = Project.load(tmp_path / "y" / PROJECT_FILE)
    assert toml_project.flows == yaml_project.flows == {"ghdl_sim": {"werror": True}}
    assert toml_project.designs == yaml_project.designs == ("d",)


def test_the_yaml_spelling_is_what_discovery_finds_first(tmp_path):
    assert PROJECT_FILE_NAMES[0] == PROJECT_FILE == "xedaproject.yaml"
    (tmp_path / PROJECT_FILE).write_text(DESIGN)
    assert find_default_xedaproject(tmp_path) == tmp_path / "xedaproject.yaml"


def test_default_project_discovery_rejects_multiple_formats(tmp_path, monkeypatch):
    from xeda.design import Design
    from xeda.flow_runner import DefaultRunner

    (tmp_path / PROJECT_FILE).write_text("flows: {}\n")
    (tmp_path / TOML_PROJECT_FILE).write_text("[flows]\n")
    monkeypatch.chdir(tmp_path)
    runner = DefaultRunner(tmp_path / "run")

    with pytest.raises(ProjectError) as raised:
        runner._request("ghdl_sim", Design(name="d", root=tmp_path, rtl={"sources": []}))

    message = str(raised.value)
    assert PROJECT_FILE in message
    assert TOML_PROJECT_FILE in message
    assert "keep one" in message
    assert raised.value.code == "several_project_files"
