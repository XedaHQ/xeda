"""The module-level verbs are the `Workspace` methods with the workspace options added.

`xeda.run(spec)` is `Workspace().run(spec)`. `src/xeda/workspace_verbs.py` is generated from the
methods (`tools/generate_module_verbs.py`), so a type checker sees each signature.
"""

import inspect
import subprocess
import sys
import types
import typing
from pathlib import Path

import pytest

import xeda
import xeda.errors
import xeda.introspect
import xeda.workspace_verbs

ROOT = Path(__file__).resolve().parents[1]

#: The workspace options of each verb, in the order of the constructor.
VERBS = {
    "plan": ("run_root", "hashed_run_dirs", "remote", "local_settings"),
    "run": ("run_root", "hashed_run_dirs", "tool_output", "confirm", "remote", "local_settings"),
    "sweep": ("run_root", "hashed_run_dirs", "tool_output", "remote", "local_settings"),
    "load": (),
    "records": ("run_root",),
    "scrub": ("run_root", "confirm"),
}
#: The module-level `run` takes a `Spec` only, so its parameter has another name.
RENAMED = {("run", "what"): "spec"}
LAUNCH_OPTIONS = {
    "rebuild_all": False,
    "clean": False,
    "prune_variants": False,
    "outputs_to": None,
    "overwrite_outputs": False,
    "post_cleanup": False,
    "post_cleanup_purge": False,
}

CONSTRUCTOR = inspect.signature(xeda.Workspace).parameters


@pytest.mark.parametrize("name", VERBS)
def test_a_verb_is_the_method_plus_its_workspace_options(name: str) -> None:
    verb, method = getattr(xeda.workspace_verbs, name), getattr(xeda.Workspace, name)
    verb_parameters = inspect.signature(verb).parameters
    method_parameters = inspect.signature(method).parameters
    own = [key for key in verb_parameters if key not in VERBS[name]]
    assert own == [RENAMED.get((name, key), key) for key in method_parameters if key != "self"]
    for option in VERBS[name]:
        assert verb_parameters[option].kind is inspect.Parameter.KEYWORD_ONLY
        assert verb_parameters[option].default == CONSTRUCTOR[option].default
    for key in own:
        method_key = next(k for k in method_parameters if RENAMED.get((name, k), k) == key)
        assert verb_parameters[key].kind is method_parameters[method_key].kind
        if not (name == "run" and key in LAUNCH_OPTIONS):
            assert verb_parameters[key].default == method_parameters[method_key].default


@pytest.mark.parametrize("name", VERBS)
def test_the_annotations_are_the_methods(name: str) -> None:
    verb_hints = typing.get_type_hints(getattr(xeda.workspace_verbs, name))
    method_hints = typing.get_type_hints(
        getattr(xeda.Workspace, name),
        localns={"SweepResult": xeda.SweepResult, "ScrubResult": xeda.ScrubResult},
    )
    constructor_hints = typing.get_type_hints(xeda.Workspace.__init__)
    for key, hint in verb_hints.items():
        if key in VERBS[name]:
            assert hint == constructor_hints[key]
        elif (name, key) == ("run", "spec"):
            assert hint is xeda.Spec
        elif name == "run" and key in LAUNCH_OPTIONS:
            assert hint == typing.get_type_hints(xeda.Workspace.plan)[key]
        else:
            assert hint == method_hints[RENAMED_BACK.get((name, key), key)]


RENAMED_BACK = {(verb, new): old for (verb, old), new in RENAMED.items()}


def test_verb_and_workspace_option_names_are_disjoint() -> None:
    for name, options in VERBS.items():
        method = inspect.signature(getattr(xeda.Workspace, name)).parameters
        assert not set(method) & set(options)


class Recorder:
    """Stands in for `Workspace`: it records how a verb builds it and calls it."""

    calls: list[tuple[str, tuple, dict]]

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.calls = [("Workspace", args, kwargs)]
        type(self).last = self

    def __getattr__(self, verb: str):
        def call(*args: object, **kwargs: object) -> str:
            self.calls.append((verb, args, kwargs))
            return f"result of {verb}"

        return call


def test_the_call_delegates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(xeda.workspace_verbs, "Workspace", Recorder)
    spec = object.__new__(xeda.Spec)
    cases = {
        "plan": ((spec,), {"run_root": tmp_path, "clean": True}),
        "run": ((spec,), {"hashed_run_dirs": True, "tool_output": "terminal", "clean": True}),
        "sweep": (([spec],), {"remote": None, "max_workers": 2}),
        "records": ((), {"run_root": tmp_path, "flow": "x"}),
        "scrub": ((), {"design_name": "d", "confirm": None, "run_root": tmp_path}),
    }
    for name, (args, kwargs) in cases.items():
        assert getattr(xeda.workspace_verbs, name)(*args, **kwargs) == f"result of {name}"
        made, called = Recorder.last.calls
        constructor = inspect.signature(xeda.Workspace).bind(*made[1], **made[2])
        constructor.apply_defaults()
        for option in VERBS[name]:
            expected = kwargs.get(option, CONSTRUCTOR[option].default)
            assert constructor.arguments[option] == expected
        assert called[0] == name
        given_to_method = {k: v for k, v in kwargs.items() if k not in VERBS[name]}
        assert called[1] == args
        assert {k: v for k, v in called[2].items() if k in given_to_method} == given_to_method

    calls = []
    monkeypatch.setattr(
        Recorder, "load", lambda path: calls.append(path) or "record", raising=False
    )
    assert xeda.workspace_verbs.load(tmp_path) == "record"
    assert calls == [tmp_path]


def test_the_module_level_run_takes_a_spec_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(xeda.workspace_verbs, "Workspace", Recorder)
    Recorder.last = None
    with pytest.raises(TypeError, match=r"ws\.run\(plan\)"):
        xeda.workspace_verbs.run(object.__new__(xeda.Plan))
    assert Recorder.last is None


def test_the_module_level_run_has_ordinary_launch_defaults() -> None:
    parameters = inspect.signature(xeda.workspace_verbs.run).parameters
    for name, default in LAUNCH_OPTIONS.items():
        assert parameters[name].default is default
    assert [k for k, p in parameters.items() if p.default is xeda.UNSET] == ["run_root"]


def test_the_docstrings_share_one_source() -> None:
    for name in VERBS:
        method_doc = inspect.getdoc(getattr(xeda.Workspace, name))
        verb_doc = inspect.getdoc(getattr(xeda.workspace_verbs, name))
        assert method_doc and verb_doc
        if name == "run":
            assert (
                verb_doc.startswith("Run a `Spec`, and return") and "`Plan` brings" not in verb_doc
            )
            assert method_doc.split("\n\n")[2] in verb_doc
        else:
            assert verb_doc.startswith(method_doc.splitlines()[0])
        assert "`run_root`" in verb_doc or name == "load"


def test_the_generated_verbs_are_current() -> None:
    done = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "generate_module_verbs.py"), "--check"],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr


PUBLIC_NAMES = {
    "xeda": [
        "Design", "DesignPart", "Rtl", "Testbench", "Source", "Project", "Spec", "LocalSettings",
        # Export `local_settings`, `plan`, `records` and `sweep` later: each names a submodule.
        "Plan", "PlanNode", "PlanInput", "PlanDelivery",
        "Workspace", "Remote", "CancelToken", "Event",
        "run", "load", "scrub",
        "Result", "NodeResult", "RunRecord", "OutputRecord", "InputRecord", "InputSource",
        "DeliveryRecord", "MetricInfo", "ErrorInfo", "Outcome", "Disposition", "OpenRun",
        "SweepResult", "SweepPoint", "RunListing", "RecordProblem", "ScrubResult",
        "UNSET", "Unset", "__version__",
    ],
    "xeda.errors": [
        "XedaError", "SpecError", "SettingsError", "DesignError", "ProjectError",
        "FlowNotFoundError", "ChainError", "RunRootError", "StalePlanError", "RecordError",
        "RunReplacedError", "RemoteError", "RunFailedError", "LeaseHeldError",
    ],
    "xeda.introspect": [
        "flows_info", "flow_info", "settings_info", "results_info", "design_schema",
        "boards_info", "platforms_info",
    ],
}  # fmt: skip
#: Public names that the list above does not repeat: `confirm` gets a `Question`.
EXTRA = {"xeda": {"Question"}, "xeda.errors": set(), "xeda.introspect": set()}


@pytest.mark.parametrize("module", PUBLIC_NAMES)
def test_the_public_names_are_exported(module: str) -> None:
    exported = set(sys.modules[module].__all__)
    assert set(PUBLIC_NAMES[module]) <= exported
    assert exported - set(PUBLIC_NAMES[module]) == EXTRA[module]
    for name in exported:
        assert hasattr(sys.modules[module], name)


def test_package_verbs_and_submodules() -> None:
    for name in ("run", "load", "scrub"):
        assert getattr(xeda, name) is getattr(xeda.workspace_verbs, name)
    for name in ("plan", "records", "sweep"):
        assert isinstance(getattr(xeda, name), types.ModuleType)


def test_load_needs_no_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unrelated `xeda_run` in the current directory does not stop `xeda.load`."""
    from .test_records_api import stamped

    workspace, spec = stamped(tmp_path / "work")
    record = workspace.run(spec).record
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "notes.txt").write_text("not a run root")
    (elsewhere / "xeda_run").symlink_to(unrelated, target_is_directory=True)
    monkeypatch.chdir(elsewhere)
    with pytest.raises(xeda.errors.RunRootError):
        xeda.Workspace()
    assert xeda.load(record.run_dir) == record
