"""The launcher's stage modules keep their boundaries.

`flow_runner/default_runner.py` holds the launcher: its skeleton, its state and the request layer.
Each stage of a launch lives in a module of its own (`launch_*.py`, and `scrub.py` for removing
earlier runs) as free functions with explicit arguments. Two rules keep that true.

- No stage module imports `default_runner`, not at the top, not in a function, not under
  `TYPE_CHECKING`. A stage that needs the launcher takes what it needs as an argument, or types
  the call it makes back with a `Protocol` of its own (`launch_producers.LaunchFlow`). An import of
  the launcher would make a cycle, and a "type-only" one would hide that the boundary is wrong.
- Every name that `default_runner` defined before the split is still importable from it, and it
  is the object that now lives in its stage module (`MOVED`). Code outside the package, and tests,
  import these names there.

A name that tests replace on `default_runner` must be one the launcher itself looks up there:
`test_patch_targets.py` checks that.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

import xeda
from xeda.flow_runner import default_runner

PACKAGE = Path(xeda.__file__).parent
FLOW_RUNNER = PACKAGE / "flow_runner"
LAUNCHER = "xeda.flow_runner.default_runner"

#: The stage modules: every `launch_*.py`, and the scrub module.
STAGE_MODULES = sorted([*FLOW_RUNNER.glob("launch_*.py"), *FLOW_RUNNER.glob("scrub.py")])

#: Moved names the launcher module imports as they are (the launcher itself uses them): name in
#: `default_runner` -> (module that defines it now, name there). The rest are `MOVED_NAMES`.
IMPORTED: dict[str, tuple[str, str]] = {
    "RunDirPolicy": ("launch_identity", "RunDirPolicy"),
    "run_directory_names": ("scrub", "run_directory_names"),
    "scrub_runs": ("scrub", "scrub_runs"),
    "_drop_unwritten_artifacts": ("launch_report", "drop_unwritten"),
}

#: Every name that `default_runner` defined before the split into stages. Each must stay
#: importable from it.
NAMES_BEFORE_THE_SPLIT = (
    "COMPLETION_DOCUMENTS",
    "DefaultRunner",
    "DesignNotFoundError",
    "FlowLauncher",
    "FlowLauncherType",
    "FlowRunner",
    "LAUNCH_DOCUMENTS",
    "RunDirPolicy",
    "ScrubResult",
    "XedaOptions",
    "_Listed",
    "_Method",
    "_REMOVED_LAUNCHER_SETTINGS",
    "_RecordingProject",
    "_Removal",
    "_Request",
    "_Scan",
    "_Skipped",
    "__all__",
    "_choosing_once",
    "_completion_records",
    "_distinct",
    "_drop_unwritten_artifacts",
    "_flow_name_suggestions",
    "_free_working_locations",
    "_get_flow_class_if_known",
    "_is_gone",
    "_listed",
    "_lock_path",
    "_problem_now",
    "_refuse_a_change",
    "_refuse_inputs_inside",
    "_refuse_outputs_to_a_programmer",
    "_registered_flow_class",
    "_remove_confirmed",
    "_run_directories_in",
    "_say",
    "_say_skipped",
    "_setting_naming",
    "_target_parents",
    "_warn_outputs_to_delivered_nothing",
    "add_file_logger",
    "get_flow_class",
    "log",
    "remove_file_logger",
    "run_directory_names",
    "scrub_design",
    "scrub_runs",
)


def _names_the_launcher(source: str, package: str) -> list[str]:
    """Where `source`, a module of `package`, imports `default_runner` in any form."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [
                f"line {node.lineno}: import {a.name}" for a in node.names if a.name == LAUNCHER
            ]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")
                base = ".".join(parts[: len(parts) - node.level + 1] + ([base] if base else []))
            if base == LAUNCHER:
                found.append(
                    f"line {node.lineno}: from {'.' * node.level}{node.module or ''} import ..."
                )
            elif base == LAUNCHER.rsplit(".", 1)[0] and any(
                a.name == "default_runner" for a in node.names
            ):
                found.append(f"line {node.lineno}: from {base} import default_runner")
        elif isinstance(node, ast.Constant) and node.value in (LAUNCHER, ".default_runner"):
            found.append(f"line {node.lineno}: the module name as text ({node.value})")
    return found


def test_there_are_stage_modules():
    """The sweep below is not vacuous."""
    assert len(STAGE_MODULES) >= 2, STAGE_MODULES


@pytest.mark.parametrize("path", STAGE_MODULES, ids=lambda p: p.name)
def test_no_stage_module_imports_the_launcher(path: Path):
    found = _names_the_launcher(path.read_text(), "xeda.flow_runner")
    assert not found, f"{path.name} imports default_runner: {found}"


@pytest.mark.parametrize(
    ("source", "why"),
    [
        ("from .default_runner import FlowLauncher\n", "relative"),
        ("from xeda.flow_runner.default_runner import FlowLauncher\n", "absolute"),
        ("from xeda.flow_runner import default_runner\n", "the module itself"),
        ("from . import default_runner\n", "the module itself, relative"),
        ("import xeda.flow_runner.default_runner\n", "import"),
        ("def f():\n    from .default_runner import x\n", "inside a function"),
        (
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from .default_runner import X\n",
            "under TYPE_CHECKING",
        ),
        (
            "import importlib\nimportlib.import_module('xeda.flow_runner.default_runner')\n",
            "by text",
        ),
    ],
)
def test_the_check_sees_every_way_to_import_the_launcher(source: str, why: str):
    """The check's own teeth: each form is found, so a stage module cannot hide the import."""
    assert _names_the_launcher(source, "xeda.flow_runner"), why


@pytest.mark.parametrize(
    "source",
    [
        "from .launch_run import execute\n",
        "from .scrub import scrub_runs\n",
        "from . import scrub\n",
        '"""Never imports `default_runner`."""\n',
    ],
)
def test_the_check_ignores_other_imports(source: str):
    assert _names_the_launcher(source, "xeda.flow_runner") == []


ALL_MOVED = {**IMPORTED, **default_runner.MOVED_NAMES}


@pytest.mark.parametrize("old", sorted(ALL_MOVED))
def test_a_moved_name_is_the_object_that_lives_in_its_stage_module(old: str):
    module, new = ALL_MOVED[old]
    home = importlib.import_module(f"xeda.flow_runner.{module}")
    assert getattr(default_runner, old) is getattr(home, new)


@pytest.mark.parametrize("name", NAMES_BEFORE_THE_SPLIT)
def test_every_name_defined_before_the_split_is_still_importable(name: str):
    assert hasattr(default_runner, name), f"{name} can no longer be imported from default_runner"


def test_a_moved_name_the_launcher_does_not_use_is_not_also_defined_there():
    """`MOVED_NAMES` is for names only importers use; a name the launcher binds itself is in
    `IMPORTED`, and a stale entry would hide the binding."""
    assert not set(default_runner.MOVED_NAMES) & set(vars(default_runner))
    assert not set(IMPORTED) & set(default_runner.MOVED_NAMES)


@pytest.mark.parametrize("name", sorted(default_runner.MOVED_NAMES))
def test_a_moved_name_cannot_be_replaced_on_the_launcher_module(name: str, monkeypatch):
    """Nothing in the launcher looks a moved name up, so replacing it there would change nothing
    and leave a stale global behind. The module refuses, naming the module to patch."""
    module = default_runner.MOVED_NAMES[name][0]
    with pytest.raises(AttributeError, match=rf"replace it on `xeda\.flow_runner\.{module}`"):
        setattr(default_runner, name, None)
    with pytest.raises(AttributeError, match=module):
        monkeypatch.setattr(default_runner, name, None)
    with pytest.raises(AttributeError, match=module):
        delattr(default_runner, name)
    assert name not in vars(default_runner), "a refused replacement left a global behind"


@pytest.mark.parametrize("name", ["_drop_unwritten_artifacts", "scrub_runs", "expectation"])
def test_a_name_the_launcher_looks_up_can_still_be_replaced(name: str, monkeypatch):
    original = getattr(default_runner, name)
    with monkeypatch.context() as patching:
        patching.setattr(default_runner, name, id)
        assert getattr(default_runner, name) is id
    assert getattr(default_runner, name) is original
