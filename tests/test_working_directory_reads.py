"""Library code never changes the process's working directory, reads it only where a call
receives a path from its caller, and starts every subprocess in an explicit directory.

The scan lists each place in `src/xeda` that calls `os.chdir`, reads the working directory
(`Path.cwd()`, `os.getcwd()`) or starts a process without `cwd=`, by module and function. Each
place is reviewed below with the reason it may do so. A new place fails until it is reviewed; a
reviewed place that is gone fails until its entry is removed, so the lists only shrink."""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path
from typing import Iterator

import xeda

SRC = Path(xeda.__file__).parent

#: The calls that start a process. Each takes `cwd=`.
PROCESS_STARTS = {
    ("subprocess", "run"),
    ("subprocess", "call"),
    ("subprocess", "check_call"),
    ("subprocess", "check_output"),
    ("subprocess", "Popen"),
    ("asyncio", "create_subprocess_exec"),
    ("asyncio", "create_subprocess_shell"),
    (None, "run_process"),
    (None, "run_capture_pty"),
}

#: (module, function) -> why it changes the working directory.
REVIEWED_CHDIRS: dict[tuple[str, str], str] = {
    ("utils.py", "WorkingDirectory.__enter__"): "the launch still enters the run directory",
    ("utils.py", "WorkingDirectory.__exit__"): "the launch still enters the run directory",
    (
        "remote/launch_entry.py",
        "execute",
    ): "the entry point of the process that runs a remote launch",
}

#: (module, function) -> why it reads the working directory.
REVIEWED_CWD_READS: dict[tuple[str, str], str] = {
    ("agent_skill.py", "default_skill_dir"): "a command's default target directory",
    ("cli.py", "run"): "the default run root of a command",
    ("cocotb.py", "Cocotb.pycache_prefix"): "a cocotb runner outside a run directory",
    ("design.py", "design_relative_root"): "a design file or resource given outside a design load",
    (
        "flow_runner/default_runner.py",
        "FlowLauncher.__init__",
    ): "the directory a launcher starts in",
    ("flow_runner/default_runner.py", "FlowLauncher._design_from_project"): "an error message",
    ("flow_runner/default_runner.py", "FlowLauncher._launch"): "relative setting paths",
    ("flow_runner/default_runner.py", "FlowLauncher._request"): "a design with no root",
    ("flow_runner/default_runner.py", "FlowLauncher._validate_plan"): "relative setting paths",
    ("flow_runner/default_runner.py", "FlowLauncher.plan_with_freshness"): "relative setting paths",
    ("flow_runner/default_runner.py", "FlowLauncher.resolve"): "relative setting paths",
    ("flows/yosys/cxx_rtl.py", "YosysSim.run"): "the launch still enters the run directory",
    ("remote/client.py", "_run"): "the default run root of a command",
    ("remote/packaging.py", "package_inputs"): "relative setting paths",
    ("remote/packaging.py", "package_inputs.packaged_liberty"): "relative setting paths",
    ("remote/packaging.py", "package_inputs.packaged_path"): "relative setting paths",
    ("run_root.py", "ensure_run_root"): "a run root given relative to the caller",
    ("tool.py", "Docker.run"): "a container started outside a tool",
    ("tool.py", "Tool.working_directory"): "a tool outside a flow",
    ("utils.py", "WorkingDirectory.__enter__"): "the launch still enters the run directory",
}

#: (module, function) -> why a process it starts needs no explicit working directory.
REVIEWED_PROCESS_STARTS: dict[tuple[str, str], str] = {
    ("flow_runner/trace.py", "_image_id"): "`docker image inspect` names no file",
    ("platforms/providers.py", "_check_ciel"): "`ciel --version` names no file",
    ("platforms/providers.py", "_run"): "an install command names absolute paths only",
    ("remote/agent.py", "_kill_user_processes"): "names processes, not files",
    ("remote/agent.py", "_where_found"): "names processes, not files",
    ("remote/client.py", "machine_id"): "names no file",
    ("remote/ssh.py", "SshSession._close"): "a relative `-F` file is the caller's",
    ("remote/ssh.py", "SshSession._start_master"): "a relative `-F` file is the caller's",
    ("remote/ssh.py", "SshSession.check"): "a relative `-F` file is the caller's",
    ("remote/ssh.py", "SshSession.popen"): "a relative `-F` file is the caller's",
    ("remote/ssh.py", "SshSession.run"): "a relative `-F` file is the caller's",
    ("remote/ssh.py", "resolve_destination"): "a relative `-F` file is the caller's",
    ("tool.py", "Docker.run.kill_container"): "`docker kill` names a container, not a file",
}


def _owner(node: ast.expr) -> str | None:
    """The last name of `a.b` or `b`: what an attribute is looked up on."""
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else None


def _called(node: ast.Call) -> tuple[str | None, str]:
    """(`owner`, `name`) of `owner.name(...)`, or (None, `name`) of a bare `name(...)`."""
    func = node.func
    if isinstance(func, ast.Attribute):
        return _owner(func.value), func.attr
    if isinstance(func, ast.Name):
        return None, func.id
    return None, ""


#: Lookups that change or read the working directory, called or passed on (`factory=Path.cwd`).
DIRECTORY_LOOKUPS = {("os", "chdir"): "chdir", ("Path", "cwd"): "cwd", ("os", "getcwd"): "cwd"}


def _scan(tree: ast.AST) -> Iterator[tuple[str, str]]:
    """(kind, function qualname) for each reviewed kind of use in `tree`."""

    def walk(node: ast.AST, scope: list[str]) -> Iterator[tuple[str, str]]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                yield from walk(child, [*scope, child.name])
                continue
            where = ".".join(scope) or "<module>"
            if isinstance(child, ast.Attribute):
                kind = DIRECTORY_LOOKUPS.get((_owner(child.value), child.attr))  # type: ignore[arg-type]
                if kind:
                    yield kind, where
            elif isinstance(child, ast.ImportFrom) and child.module == "os":
                for alias in child.names:
                    if ("os", alias.name) in DIRECTORY_LOOKUPS:
                        yield DIRECTORY_LOOKUPS[("os", alias.name)], where
            elif isinstance(child, ast.Call):
                owner, name = _called(child)
                if (owner, name) in PROCESS_STARTS or (None, name) in PROCESS_STARTS:
                    if not any(k.arg == "cwd" for k in child.keywords):
                        yield "process", where
            yield from walk(child, scope)

    yield from walk(tree, [])


def _found() -> dict[str, Counter]:
    found: dict[str, Counter] = {"chdir": Counter(), "cwd": Counter(), "process": Counter()}
    for file in sorted(SRC.rglob("*.py")):
        module = file.relative_to(SRC).as_posix()
        for kind, where in _scan(ast.parse(file.read_text(), str(file))):
            found[kind][(module, where)] += 1
    return found


def _check(kind: str, reviewed: dict[tuple[str, str], str]) -> None:
    found = set(_found()[kind])
    new = sorted(found - set(reviewed))
    gone = sorted(set(reviewed) - found)
    assert not new, f"not reviewed ({kind}): {new}"
    assert not gone, f"reviewed but gone; remove the entries ({kind}): {gone}"


def test_no_library_code_changes_the_working_directory():
    _check("chdir", REVIEWED_CHDIRS)


def test_the_working_directory_is_read_only_where_a_call_receives_a_path():
    _check("cwd", REVIEWED_CWD_READS)


def test_every_process_starts_in_an_explicit_directory():
    _check("process", REVIEWED_PROCESS_STARTS)


def test_the_scan_sees_each_kind_of_call():
    """The teeth of the scan: each spelling it looks for is found, in the right function."""
    source = """
import os, subprocess
from pathlib import Path
class C:
    def f(self):
        os.chdir("x")
        Path.cwd()
        os.getcwd()
        pathlib.Path.cwd()
        attrs.field(factory=Path.cwd)
        subprocess.run(["true"])
        subprocess.run(["true"], cwd="/")
        run_process("true", [])
"""
    seen = Counter((kind, where) for kind, where in _scan(ast.parse(source)))
    assert seen == Counter({("chdir", "C.f"): 1, ("cwd", "C.f"): 4, ("process", "C.f"): 2}), seen
