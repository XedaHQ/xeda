"""Related programs come from the selected installation, not a second PATH lookup."""

from pathlib import Path

import pytest

from xeda.tool import Docker, Tool
from xeda.utils import ToolException


def _program(path: Path, text: str = "ok") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{text}'\n")
    path.chmod(0o755)
    return path


def test_sibling_uses_selected_installation_through_a_public_symlink(tmp_path, monkeypatch):
    source = _program(tmp_path / "selected/bin/yosys")
    helper = _program(source.parent / "yosys-config", "selected")
    public = tmp_path / "public"
    public.mkdir()
    (public / "yosys").symlink_to(source)
    _program(tmp_path / "other/bin/yosys-config", "other")
    monkeypatch.setenv("PATH", f"{public}:{tmp_path / 'other/bin'}")
    tool = Tool(executable="yosys")
    related = tool.derive("yosys-config", sibling=True)
    assert related.executable == str(helper)
    assert related.probe_stdout("--datdir").strip() == "selected"
    assert tool.executable == "yosys"


def test_missing_sibling_does_not_fall_back_to_another_installation(tmp_path, monkeypatch):
    source = _program(tmp_path / "selected/bin/yosys")
    _program(tmp_path / "other/bin/yosys-config", "other")
    monkeypatch.setenv("PATH", str(tmp_path / "other/bin"))
    with pytest.raises(ToolException, match="yosys-config.*yosys|yosys.*yosys-config"):
        Tool(executable=str(source)).derive("yosys-config", sibling=True)


def test_container_sibling_is_resolved_in_its_execution_environment(monkeypatch):
    calls = []

    def run(self, executable, *args, **kwargs):
        calls.append((executable, args))
        return "/opt/yosys/bin/yosys-config\n"

    monkeypatch.setattr(Docker, "run", run)
    monkeypatch.setattr(Tool, "executable_path", lambda self: pytest.fail("host lookup"))
    tool = Tool(executable="yosys", dockerized=True, docker=Docker(image="yosys/test"))
    related = tool.derive("yosys-config", sibling=True)
    assert related.executable == "/opt/yosys/bin/yosys-config"
    assert related.docker.command == [related.executable]
    assert tool.docker.command != related.docker.command
    assert calls[0][0] == "sh"
    assert calls[0][1][-2:] == ("yosys", "yosys-config")
