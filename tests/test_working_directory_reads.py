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

#: Calls by their full dotted name, as the module imported them (aliases resolved), and the kind
#: of use each is. A process start counts only without an explicit `cwd=` (a literal `cwd=None` is
#: no directory). A lookup counts whether it is called or passed on (`factory=Path.cwd`).
LOOKUPS = {
    "os.chdir": "chdir",
    "os.fchdir": "chdir",
    "contextlib.chdir": "chdir",
    "pathlib.Path.cwd": "cwd",
    "os.getcwd": "cwd",
    "os.getcwdb": "cwd",
}
PROCESS_STARTS = {
    "subprocess.run",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
    "subprocess.Popen",
    "subprocess.getoutput",
    "subprocess.getstatusoutput",
    "asyncio.create_subprocess_exec",
    "asyncio.create_subprocess_shell",
    "xeda.proc_utils.run_process",
    "xeda.proc_utils.run_capture_pty",
}
#: Process starts that take no working directory at all: always a finding.
NO_CWD_STARTS = (
    {"os.system", "os.popen", "pty.spawn"}
    | {
        f"os.{name}{suffix}"
        for name in ("execv", "execl", "spawnv", "spawnl")
        for suffix in ("", "e", "p", "pe")
    }
    | {"os.posix_spawn", "os.posix_spawnp"}
    # Process pools: their workers start in the working directory of the caller.
    | {
        "pebble.ProcessPool",
        "pebble.pool.process.ProcessPool",
        "concurrent.futures.ProcessPoolExecutor",
        "multiprocessing.Pool",
        "multiprocessing.Process",
        "multiprocessing.pool.Pool",
    }
)

#: (module, function) -> why it changes the working directory.
REVIEWED_CHDIRS: dict[tuple[str, str], str] = {
    (
        "remote/launch_entry.py",
        "execute",
    ): "the entry point of a dedicated process that runs one remote launch: it changes the state "
    "of no other process",
}

#: (module, function) -> why it reads the working directory.
REVIEWED_CWD_READS: dict[tuple[str, str], str] = {
    ("agent_skill.py", "default_skill_dir"): "a command's default target directory",
    ("cli.py", "run"): "the default run root of a command",
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
    ("remote/client.py", "_run"): "the default run root of a command",
    ("remote/packaging.py", "package_inputs"): "relative setting paths",
    ("remote/packaging.py", "package_inputs.packaged_liberty"): "relative setting paths",
    ("remote/packaging.py", "package_inputs.packaged_path"): "relative setting paths",
    ("run_root.py", "ensure_run_root"): "a run root given relative to the caller",
    ("tool.py", "Docker.run"): "a container started outside a tool",
    ("tool.py", "Tool.working_directory"): "a tool outside a flow",
}

#: (module, function) -> why a process it starts needs no explicit working directory.
REVIEWED_PROCESS_STARTS: dict[tuple[str, str], str] = {
    ("flow_runner/dse/dse_runner.py", "Dse.run_flow"): "a pool worker is a process of its own "
    "that runs launches: it starts where the caller is, where the caller's relative paths point",
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
}


def _imports(tree: ast.AST, module: str) -> dict[str, str]:
    """The full dotted name of each name that `module` imports (`import a.b as c`, `from a import
    b as c`, relative imports within `xeda`). The module's own top-level functions count as
    imported from it."""
    names: dict[str, str] = {}
    package = ["xeda", *module.split("/")[:-1]]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    names[alias.asname] = alias.name
                else:
                    top = alias.name.split(".")[0]
                    names[top] = top
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = ".".join([*package[: len(package) - node.level + 1], *filter(None, [base])])
            for alias in node.names:
                names[alias.asname or alias.name] = f"{base}.{alias.name}"
    own = "xeda." + module.removesuffix(".py").replace("/", ".")
    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name not in names:
            names[node.name] = f"{own}.{node.name}"
    return names


def _dotted(node: ast.expr, names: dict[str, str]) -> str | None:
    """The full dotted name of `a.b.c` or `a`, through the imports in `names`."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name) or node.id not in names:
        return None
    return ".".join([names[node.id], *reversed(parts)])


def _scan(tree: ast.AST, module: str = "xeda/m.py") -> Iterator[tuple[str, str]]:
    """(kind, function qualname) for each reviewed kind of use in `tree`."""
    names = _imports(tree, module)

    def walk(node: ast.AST, scope: list[str]) -> Iterator[tuple[str, str]]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                yield from walk(child, [*scope, child.name])
                continue
            where = ".".join(scope) or "<module>"
            if isinstance(child, ast.Call):
                called = _dotted(child.func, names)
                if called in NO_CWD_STARTS:
                    yield "process", where
                elif called in PROCESS_STARTS:
                    cwd = [k.value for k in child.keywords if k.arg == "cwd"]
                    if not cwd or (isinstance(cwd[0], ast.Constant) and cwd[0].value is None):
                        yield "process", where
                if called in LOOKUPS:
                    yield LOOKUPS[called], where
                    # The called name itself is not visited again as a lookup.
                    for part in [*child.args, *(k.value for k in child.keywords)]:
                        yield from walk_expression(part, scope)
                    continue
            elif isinstance(child, (ast.Attribute, ast.Name)) and isinstance(child.ctx, ast.Load):
                looked_up = _dotted(child, names)
                if looked_up in LOOKUPS:
                    yield LOOKUPS[looked_up], where
                    continue
            yield from walk(child, scope)

    def walk_expression(node: ast.AST, scope: list[str]) -> Iterator[tuple[str, str]]:
        wrapper = ast.Expr(value=node)  # type: ignore[arg-type]
        yield from walk(wrapper, scope)

    yield from walk(tree, [])


def _found() -> dict[str, Counter]:
    found: dict[str, Counter] = {"chdir": Counter(), "cwd": Counter(), "process": Counter()}
    for file in sorted(SRC.rglob("*.py")):
        module = file.relative_to(SRC).as_posix()
        for kind, where in _scan(ast.parse(file.read_text(), str(file)), module):
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
    """The teeth of the scan: each spelling it looks for is found, in the right function, and a
    process started in an explicit directory is not."""
    source = """
import contextlib
import os, subprocess
import os as o
import pathlib
import subprocess as sp
from contextlib import chdir
from os import getcwd as here
from pathlib import Path
from pathlib import Path as P
from subprocess import Popen, run
from ..proc_utils import run_process
from pebble.pool.process import ProcessPool
class C:
    def f(self):
        os.chdir("x")
        o.chdir("x")
        contextlib.chdir("x")
        chdir("x")
    def g(self):
        Path.cwd()
        P.cwd()
        pathlib.Path.cwd()
        os.getcwd()
        here()
        attrs.field(factory=Path.cwd)
        Path(Path.cwd())
    def h(self):
        subprocess.run(["true"])
        sp.run(["true"])
        run(["true"])
        Popen(["true"], cwd=None)
        run_process("true", [])
        os.system("true")
        os.execvp("true", ["true"])
        ProcessPool(max_workers=2)
    def fine(self):
        subprocess.run(["true"], cwd="/")
        run(["true"], cwd=Path("/"))
        run_process("true", [], cwd="/")
        cwd()
"""
    seen = Counter(_scan(ast.parse(source), "flow_runner/m.py"))
    assert seen == Counter({("chdir", "C.f"): 4, ("cwd", "C.g"): 7, ("process", "C.h"): 8}), seen
