"""`SettingsError.origin` names the layer that gave the refused value."""

from __future__ import annotations

from pathlib import Path

import pytest

from xeda import Design, LocalSettings, Spec, Workspace
from xeda.errors import SettingsError

FLOW = "ghdl_sim"


def _design(root: Path, flows: str = "", dependency: bool = False) -> Design:
    root.mkdir(parents=True, exist_ok=True)
    (root / "t.v").write_text("module t; endmodule\n")
    listed = "dependencies: [../lib/lib.yaml]\n" if dependency else ""
    (root / "d.yaml").write_text(f"name: d\n{listed}rtl:\n  sources: [t.v]\n  top: t\n{flows}")
    return Design.load(root / "d.yaml")


def _findings(
    tmp_path: Path, design: Design, settings=None, local: LocalSettings | None = None
) -> dict[str, str | None]:
    workspace = Workspace(tmp_path / "run", local_settings=local)
    with pytest.raises(SettingsError) as raised:
        workspace.plan(Spec(FLOW, design, settings=settings or {}))
    return {item.setting or "": item.origin for item in raised.value.errors}


def test_a_value_of_the_design_file_names_the_design_file(tmp_path):
    design = _design(tmp_path / "d", f"flows:\n  {FLOW}:\n    nosuch: 1\n")
    assert _findings(tmp_path, design) == {"nosuch": str(tmp_path / "d" / "d.yaml")}


def test_a_value_of_the_spec_names_the_command_line_and_api_layer(tmp_path):
    design = _design(tmp_path / "d")
    found = _findings(tmp_path, design, {f"{FLOW}.nosuch": 1})
    assert found == {"nosuch": "the command line (`-s`) or `flow_settings`"}


def test_the_text_of_a_setting_that_is_no_boolean_names_the_command_line_layer(tmp_path):
    design = _design(tmp_path / "d")
    found = _findings(tmp_path, design, {f"{FLOW}.relaxed": "maybe"})
    assert set(found.values()) == {"the command line (`-s`) or `flow_settings`"}


def test_an_unscoped_key_names_the_command_line_layer(tmp_path):
    design = _design(tmp_path / "d")
    found = _findings(tmp_path, design, {"nosuch": 1})
    assert set(found.values()) == {"the command line (`-s`) or `flow_settings`"}


def test_a_value_of_the_user_file_names_it(tmp_path):
    design = _design(tmp_path / "d")
    user_file = tmp_path / "user.yaml"
    local = LocalSettings(user={FLOW: {"nosuch": 1}}, user_file=user_file)
    assert _findings(tmp_path, design, local=local) == {"nosuch": f"the user config {user_file}"}


def test_a_value_of_the_project_local_file_names_it(tmp_path):
    design = _design(tmp_path / "d")
    local_file = tmp_path / "local.yaml"
    local = LocalSettings(local={FLOW: {"nosuch": 1}}, local_file=local_file)
    assert _findings(tmp_path, design, local=local) == {
        "nosuch": f"the project-local file {local_file}"
    }


def test_the_highest_layer_that_gave_the_value_is_the_origin(tmp_path):
    design = _design(tmp_path / "d", f"flows:\n  {FLOW}:\n    relaxed: 7\n")
    found = _findings(tmp_path, design, {f"{FLOW}.relaxed": 8})
    assert found == {"relaxed": "the command line (`-s`) or `flow_settings`"}


def test_the_early_check_of_a_design_with_dependencies_names_the_layer_too(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "t.v").write_text("module t; endmodule\n")
    (tmp_path / "lib" / "lib.yaml").write_text("name: lib\nrtl:\n  sources: [t.v]\n  top: t\n")
    design = _design(tmp_path / "d", f"flows:\n  {FLOW}:\n    nosuch: 1\n", dependency=True)
    assert _findings(tmp_path, design) == {"nosuch": str(tmp_path / "d" / "d.yaml")}
    found = _findings(tmp_path, design, {f"{FLOW}.other": 1})
    assert found["other"] == "the command line (`-s`) or `flow_settings`"


def test_a_finding_that_no_layer_gave_has_no_origin(tmp_path):
    design = _design(tmp_path / "d")
    workspace = Workspace(tmp_path / "run")
    with pytest.raises(SettingsError) as raised:
        workspace.plan(Spec("vivado_synth", design))  # a required setting is missing
    assert [item.origin for item in raised.value.errors] == [None] * len(raised.value.errors)
