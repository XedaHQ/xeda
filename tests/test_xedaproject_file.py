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
