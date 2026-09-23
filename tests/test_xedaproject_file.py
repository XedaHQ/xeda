"""`XedaProject.from_file` must reject ambiguous or malformed project files instead of silently
picking one of two accepted spellings or dropping bad entries.
"""

from pathlib import Path

import pytest

from xeda.xedaproject import XedaProject

DESIGN = """
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
        "xedaproject.toml",
        DESIGN + """
[flow.ghdl_sim]
warn_error = true

[flows.ghdl_sim]
warn_error = true
""",
    )

    with pytest.raises(ValueError, match=r"flows.*flow"):
        XedaProject.from_file(path)


def test_only_flows_key_works(tmp_path):
    path = _write(
        tmp_path,
        "xedaproject.toml",
        DESIGN + """
[flows.ghdl_sim]
warn_error = true
""",
    )

    project = XedaProject.from_file(path)

    assert project.flows == {"ghdl_sim": {"warn_error": True}}
    assert len(project.designs) == 1


def test_only_flow_key_works(tmp_path):
    path = _write(
        tmp_path,
        "xedaproject.toml",
        DESIGN + """
[flow.ghdl_sim]
warn_error = true
""",
    )

    project = XedaProject.from_file(path)

    assert project.flows == {"ghdl_sim": {"warn_error": True}}


def test_both_design_and_designs_is_a_value_error(tmp_path):
    path = _write(
        tmp_path,
        "xedaproject.toml",
        """
[[design]]
name = 'd'
[design.rtl]
sources = ['top.vhd']
top = 'top'

[[designs]]
name = 'd2'
[designs.rtl]
sources = ['top.vhd']
top = 'top'
""",
    )

    with pytest.raises(ValueError, match=r"designs.*design"):
        XedaProject.from_file(path)


def test_a_non_mapping_design_entry_names_its_position_and_type(tmp_path):
    path = _write(
        tmp_path,
        "xedaproject.toml",
        """
design = ["a.toml"]

[flows.ghdl_sim]
warn_error = true
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
