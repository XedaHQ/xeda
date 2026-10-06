"""Related programs come from the selected installation, not a second PATH lookup."""

import subprocess
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


def test_prefixed_sibling_follows_selected_yosys_through_public_symlink(tmp_path, monkeypatch):
    prefix = "yosys-0.63-"
    source = _program(tmp_path / "selected/bin" / f"{prefix}yosys")
    helper = _program(source.parent / f"{prefix}yosys-config", "selected-prefixed")
    public = tmp_path / "public"
    public.mkdir()
    (public / "yosys").symlink_to(source)
    _program(source.parent / "yosys-config", "wrong-unprefixed")
    _program(tmp_path / "other/bin" / f"{prefix}yosys-config", "other-prefixed")
    monkeypatch.setenv("PATH", f"{public}:{tmp_path / 'other/bin'}")
    tool = Tool(executable="yosys")
    related = tool.derive("yosys-config", sibling=True, source_name="yosys")
    assert related.executable == str(helper)
    assert related.probe_stdout("--datdir").strip() == "selected-prefixed"


def test_prefixed_yosys_requires_its_prefixed_config_not_plain_path_helper(tmp_path, monkeypatch):
    prefix = "yosys-0.63-"
    source = _program(tmp_path / "selected/bin" / f"{prefix}yosys")
    _program(source.parent / "yosys-config", "wrong-unprefixed")
    _program(tmp_path / "other/bin" / f"{prefix}yosys-config", "other-prefixed")
    monkeypatch.setenv("PATH", f"{source.parent}:{tmp_path / 'other/bin'}")
    with pytest.raises(ToolException, match="yosys-config.*yosys|yosys.*yosys-config"):
        Tool(executable=str(source)).derive("yosys-config", sibling=True, source_name="yosys")


def test_noncanonical_wrapper_keeps_exact_sibling_name(tmp_path):
    source = _program(tmp_path / "wrapper/bin/yosys-wrapper")
    helper = _program(source.parent / "yosys-config", "wrapper-paired")
    related = Tool(executable=str(source)).derive("yosys-config", sibling=True, source_name="yosys")
    assert related.executable == str(helper)
    assert related.probe_stdout("--datdir").strip() == "wrapper-paired"


def test_container_sibling_is_resolved_in_its_execution_environment(monkeypatch):
    calls = []

    def run(self, executable, *args, **kwargs):
        calls.append((executable, args))
        return "/opt/yosys/bin/yosys-config\n"

    monkeypatch.setattr(Docker, "run", run)
    monkeypatch.setattr(Tool, "executable_path", lambda self: pytest.fail("host lookup"))
    tool = Tool(executable="yosys", dockerized=True, docker=Docker(image="yosys/test"))
    related = tool.derive("yosys-config", sibling=True, source_name="yosys")
    assert related.executable == "/opt/yosys/bin/yosys-config"
    assert related.docker.command == [related.executable]
    assert tool.docker.command != related.docker.command
    assert calls[0][0] == "sh"
    assert calls[0][1][-3:] == ("yosys", "yosys-config", "yosys")


def test_container_prefixed_sibling_is_resolved_in_selected_image(monkeypatch):
    calls = []

    def run(self, executable, *args, **kwargs):
        calls.append((executable, args))
        return "/opt/yosys/bin/yosys-0.63-yosys-config\n"

    monkeypatch.setattr(Docker, "run", run)
    monkeypatch.setattr(Tool, "executable_path", lambda self: pytest.fail("host lookup"))
    tool = Tool(executable="yosys-0.63-yosys", dockerized=True, docker=Docker(image="yosys/test"))
    related = tool.derive("yosys-config", sibling=True, source_name="yosys")
    assert related.executable == "/opt/yosys/bin/yosys-0.63-yosys-config"
    assert calls[0][1][-3:] == ("yosys-0.63-yosys", "yosys-config", "yosys")


def test_container_prefix_resolution_executes_lookup_script_and_fails_closed(tmp_path, monkeypatch):
    prefix = "yosys-0.63-"
    bindir = tmp_path / "image/bin"
    source = _program(bindir / f"{prefix}yosys")
    helper = _program(bindir / f"{prefix}yosys-config", "selected-prefixed")
    _program(bindir / "yosys-config", "wrong-unprefixed")

    def run(_docker, executable, *args, **_kwargs):
        assert executable == "sh"
        result = subprocess.run(
            ["/bin/sh", *args],
            capture_output=True,
            text=True,
            check=False,
            env={"PATH": f"{bindir}:/usr/bin:/bin"},
        )
        if result.returncode:
            raise ToolException(result.stderr or "container sibling lookup failed")
        return result.stdout

    monkeypatch.setattr(Docker, "run", run)
    monkeypatch.setattr(Tool, "executable_path", lambda self: pytest.fail("host lookup"))
    tool = Tool(
        executable="different-host-command",
        dockerized=True,
        docker=Docker(image="yosys/test", command=[source.name]),
    )
    related = tool.derive("yosys-config", sibling=True, source_name="yosys")
    assert related.executable == str(helper)

    (helper).unlink()
    with pytest.raises(ToolException, match="Cannot locate sibling.*yosys-0\\.63-yosys"):
        tool.derive("yosys-config", sibling=True, source_name="yosys")

    # A wrapper whose name is not the canonical source suffix still uses the exact sibling name.
    wrapper = _program(bindir / "yosys-wrapper")
    plain_helper = _program(bindir / "yosys-config", "wrapper-paired")
    wrapper_tool = Tool(
        executable="host-wrapper",
        dockerized=True,
        docker=Docker(image="yosys/test", command=[wrapper.name]),
    )
    related_wrapper = wrapper_tool.derive("yosys-config", sibling=True, source_name="yosys")
    assert related_wrapper.executable == str(plain_helper)
