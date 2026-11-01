"""The public API against the newest release baseline.

`tools/api_baseline.py` snapshots the public modules. A change that only adds passes. A change that
removes, renames or retypes anything public is a break, and it needs a new major version.
"""

import copy
import inspect
import json
import shutil
import types
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal, Optional, Union

import pytest

import xeda
import xeda.introspect
from xeda import Event, Workspace

pytestmark = pytest.mark.python_compat

ROOT = Path(__file__).parent.parent
TOOL = ROOT / "tools" / "api_baseline.py"


def _load_tool() -> types.ModuleType:
    """The script as a module. `tools/` is no package, and importing the file would write its
    bytecode into the checkout, so its source runs in a module of its own."""
    module = types.ModuleType("api_baseline")
    module.__file__ = str(TOOL)
    exec(compile(TOOL.read_text(encoding="utf-8"), str(TOOL), "exec"), module.__dict__)
    return module


tool = _load_tool()


def _baseline_snapshot() -> dict[str, Any]:
    return json.loads((tool.newest_baseline() / "public_api.json").read_text(encoding="utf-8"))


def test_the_public_api_is_compatible_with_the_newest_baseline() -> None:
    additive, breaking = tool.classify(_baseline_snapshot(), tool.public_api_snapshot(), kind="api")
    assert not breaking, "\n".join(breaking)


def test_the_current_snapshot_is_committed() -> None:
    assert tool.main(["--check"]) == 0


def test_the_newest_baseline_has_the_package_major() -> None:
    major = xeda.__version__.split(".")[0]
    if not major.isdigit() or int(major) < 1:
        pytest.skip("a development version below 1.0 has no release baseline of its major")
    assert int(major) == int(tool.newest_baseline().name.split(".")[0])


# The `__all__` lists of the release contract. `xeda` does not export `plan`, `records`, `sweep`
# and `local_settings` yet: their names clash with submodules until the internal modules move into
# a private package.
CONTRACT_XEDA = {
    "Design", "DesignPart", "Rtl", "Testbench", "Source", "Project", "Spec", "LocalSettings",
    "local_settings", "Plan", "PlanNode", "PlanInput", "PlanDelivery", "Workspace", "Remote",
    "CancelToken", "Event", "plan", "run", "sweep", "load", "records", "scrub", "Result",
    "NodeResult", "RunRecord", "OutputRecord", "InputRecord", "InputSource", "DeliveryRecord",
    "MetricInfo", "ErrorInfo", "Outcome", "Disposition", "OpenRun", "SweepResult", "SweepPoint",
    "RunListing", "RecordProblem", "ScrubResult", "UNSET", "Unset", "__version__",
}  # fmt: skip
NOT_EXPORTED_YET = {"plan", "records", "sweep", "local_settings"}
# `xeda` exports `Question` too: it is the type that `confirm` takes.
ALSO_EXPORTED = {"Question"}
CONTRACT_ERRORS = {
    "XedaError", "SpecError", "SettingsError", "DesignError", "ProjectError", "FlowNotFoundError",
    "ChainError", "RunRootError", "StalePlanError", "RecordError", "RunReplacedError",
    "RemoteError", "RunFailedError", "LeaseHeldError",
}  # fmt: skip
CONTRACT_INTROSPECT = {
    "flows_info", "flow_info", "settings_info", "results_info", "design_schema", "boards_info",
    "platforms_info",
}  # fmt: skip

PATH = "str | pathlib.Path | os.PathLike"
OPTIONS = ["rebuild_all: bool = False", "clean: bool = False", "prune_variants: bool = False"]
DELIVERY = [
    f"outputs_to: {PATH} | None = None",
    "overwrite_outputs: bool = False",
    "post_cleanup: bool = False",
    "post_cleanup_purge: bool = False",
]
RUN_ROOT = [f"run_root: {PATH} | xeda.Unset = UNSET", "hashed_run_dirs: bool = False"]
TOOL_OUTPUT = 'tool_output: Literal["log", "terminal"] = \'log\''
CONFIRM = "confirm: Callable[[xeda.Question], bool] | None = None"
REMOTE = ["remote: xeda.Remote | None = None", "local_settings: xeda.LocalSettings | None = None"]
CANCEL = [
    "cancel: xeda.CancelToken | None = None",
    "on_event: Callable[[xeda.Event], None] | None = None",
]
FILTERS = [
    "design_name: str | xeda.Unset = UNSET",
    "flow: str | xeda.Unset = UNSET",
    "target: str | None | xeda.Unset = UNSET",
]
#: The six verbs of the release contract, as `(parameters, return annotation)` texts.
CONTRACT_VERBS = {
    "plan": (["spec: xeda.Spec", "/", "*", *RUN_ROOT, *REMOTE, *OPTIONS, *DELIVERY], "xeda.Plan"),
    "run": (
        [
            "spec: xeda.Spec", "/", "*", *RUN_ROOT, TOOL_OUTPUT, CONFIRM, *REMOTE, *OPTIONS,
            *DELIVERY, *CANCEL,
        ],
        "xeda.Result",
    ),
    "sweep": (
        [
            "specs: Iterable[xeda.Spec]", "/", "*", *RUN_ROOT, TOOL_OUTPUT, *REMOTE,
            "max_workers: int = 1", "rebuild_all: bool = False", "clean: bool = False",
            "post_cleanup: bool = False", "post_cleanup_purge: bool = False", *CANCEL,
        ],
        "xeda.SweepResult",
    ),
    "load": ([f"run_dir: {PATH}", "/"], "xeda.RunRecord"),
    "records": (["*", f"run_root: {PATH} | xeda.Unset = UNSET", *FILTERS], "xeda.RunListing"),
    "scrub": (
        [
            "*", "design_name: str", "flow: str | xeda.Unset = UNSET",
            "target: str | None | xeda.Unset = UNSET",
            f"run_root: {PATH} | xeda.Unset = UNSET", CONFIRM,
        ],
        "xeda.ScrubResult",
    ),
}  # fmt: skip


def signature_parts(entry: dict[str, Any]) -> tuple[list[str], str]:
    """The parameters of a snapshot entry as texts, with `/` and `*` where they belong."""
    parts: list[str] = []
    for position, p in enumerate(entry["parameters"]):
        kinds = [q["kind"] for q in entry["parameters"]]
        if p["kind"] == "keyword_only" and "keyword_only" not in kinds[:position]:
            parts.append("*")
        text = f"{p['name']}: {p['annotation']}"
        parts.append(text + (f" = {p['default']}" if "default" in p else ""))
        if p["kind"] == "positional_only" and "positional_only" not in kinds[position + 1 :]:
            parts.append("/")
    return parts, entry["return"]


def test_the_1_0_baseline_matches_the_contract() -> None:
    modules = json.loads((ROOT / "tests/baselines/1.0/public_api.json").read_text("utf-8"))[
        "modules"
    ]
    assert set(modules["xeda"]["names"]) == (CONTRACT_XEDA - NOT_EXPORTED_YET | ALSO_EXPORTED)
    assert set(modules["xeda.errors"]["names"]) == CONTRACT_ERRORS
    assert set(modules["xeda.introspect"]["names"]) == CONTRACT_INTROSPECT
    verbs = modules["xeda.workspace_verbs"]["names"]
    assert {name: signature_parts(verbs[name]) for name in CONTRACT_VERBS} == CONTRACT_VERBS


@pytest.mark.parametrize(
    "annotation, text",
    [
        (Optional[int], "int | None"),
        (int | None, "int | None"),
        (Union[int, str], "int | str"),
        (Literal["a"], 'Literal["a"]'),
        (Mapping[str, Any], "Mapping[str, Any]"),
        (Callable[[Event], None] | None, "Callable[[xeda.Event], None] | None"),
        (list[Union[int, None]], "list[int | None]"),
        (tuple[int, ...], "tuple[int, ...]"),
        (Path, "pathlib.Path"),
        ("Spec", "Spec"),
    ],
)
def test_the_snapshot_is_the_same_on_every_python(annotation: Any, text: str) -> None:
    assert tool.render_annotation(annotation) == text


def _parameter(
    name: str, kind: str = "positional_or_keyword", default: str | None = None
) -> dict[str, str]:
    entry = {"name": name, "kind": kind, "annotation": "int"}
    if default is not None:
        entry["default"] = default
    return entry


def _snapshot() -> dict[str, Any]:
    function = {
        "kind": "function",
        "parameters": [
            _parameter("a"),
            _parameter("b", default="1"),
            _parameter("k", "keyword_only", "None"),
        ],
        "return": "int",
    }
    cls = {
        "kind": "class",
        "bases": [],
        "constructor": {"parameters": [_parameter("x")]},
        "methods": {"go": {"kind": "method", "parameters": [_parameter("n")], "return": "int"}},
        "attributes": [
            {"name": "run_root", "annotation": "pathlib.Path"},
            {"name": "field", "annotation": "int", "default": "1"},
        ],
        "special_methods": ["__len__"],
    }
    outcome = {
        "kind": "enum", "bases": [], "members": {"PASSED": "'passed'"}, "methods": {},
        "attributes": [], "special_methods": [],
    }  # fmt: skip
    return {"modules": {"m": {"names": {"f": function, "C": cls, "Outcome": outcome}}}}


def _names(snapshot: dict[str, Any]) -> dict[str, Any]:
    return snapshot["modules"]["m"]["names"]


def _add_keyword(s: dict[str, Any]) -> None:
    _names(s)["f"]["parameters"].append(_parameter("z", "keyword_only", "0"))


def _add_field(s: dict[str, Any]) -> None:
    cls = _names(s)["C"]
    cls["constructor"]["parameters"].append(_parameter("y", default="0"))
    cls["attributes"].append({"name": "y", "annotation": "int", "default": "0"})


def _add_name(s: dict[str, Any]) -> None:
    _names(s)["g"] = copy.deepcopy(_names(s)["f"])


def _add_method(s: dict[str, Any]) -> None:
    _names(s)["C"]["methods"]["stop"] = copy.deepcopy(_names(s)["C"]["methods"]["go"])


def _rename(s: dict[str, Any]) -> None:
    _names(s)["f"]["parameters"][0]["name"] = "c"


def _new_default(s: dict[str, Any]) -> None:
    _names(s)["f"]["parameters"][1]["default"] = "2"


def _add_positional(s: dict[str, Any]) -> None:
    _names(s)["f"]["parameters"].insert(2, _parameter("c", default="0"))


def _remove_name(s: dict[str, Any]) -> None:
    del _names(s)["f"]


def _return(s: dict[str, Any]) -> None:
    _names(s)["f"]["return"] = "int | None"


def _attribute(s: dict[str, Any]) -> None:
    _names(s)["C"]["attributes"][0]["annotation"] = "str"


def _remove_field(s: dict[str, Any]) -> None:
    del _names(s)["C"]["attributes"][1]


def _remove_special(s: dict[str, Any]) -> None:
    _names(s)["C"]["special_methods"].remove("__len__")


def _add_member(s: dict[str, Any]) -> None:
    _names(s)["Outcome"]["members"]["BLOCKED"] = "'blocked'"


def _annotation(s: dict[str, Any]) -> None:
    _names(s)["f"]["parameters"][0]["annotation"] = "str"


def _swap_positional(s: dict[str, Any]) -> None:
    a, b = _names(s)["f"]["parameters"][:2]
    _names(s)["f"]["parameters"][:2] = [b, a]


CHANGES = {
    "new keyword-only parameter with a default": (_add_keyword, "additive"),
    "new optional dataclass field at the end": (_add_field, "additive"),
    "new public name": (_add_name, "additive"),
    "new method": (_add_method, "additive"),
    "renamed parameter": (_rename, "breaking"),
    "new default value": (_new_default, "breaking"),
    "new positional parameter": (_add_positional, "breaking"),
    "removed public name": (_remove_name, "breaking"),
    "changed return annotation": (_return, "breaking"),
    "changed attribute annotation": (_attribute, "breaking"),
    "removed field": (_remove_field, "breaking"),
    "removed special method": (_remove_special, "breaking"),
    "new member of Outcome": (_add_member, "breaking"),
    "changed annotation": (_annotation, "breaking"),
    "changed positional order": (_swap_positional, "breaking"),
}


@pytest.mark.parametrize("change", CHANGES)
def test_the_classifier_tells_additive_from_breaking(change: str) -> None:
    mutate, kind = CHANGES[change]
    new = _snapshot()
    mutate(new)
    additive, breaking = tool.classify(_snapshot(), new, kind="api")
    assert bool(breaking) == (kind == "breaking"), (additive, breaking)
    assert bool(additive) == (kind == "additive"), (additive, breaking)


def test_the_classifier_finds_no_difference_in_equal_snapshots() -> None:
    assert tool.classify(_snapshot(), _snapshot(), kind="api") == ([], [])


def test_a_snapshot_kind_without_a_classifier_is_refused() -> None:
    with pytest.raises(NotImplementedError):
        tool.classify({}, {}, kind="unknown")  # type: ignore[arg-type]


def _drop_a_parameter(monkeypatch: pytest.MonkeyPatch) -> None:
    def design_schema() -> dict[str, Any]:
        return {}

    monkeypatch.setattr(xeda.introspect, "design_schema", design_schema)


def test_a_regeneration_cannot_hide_a_break(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _drop_a_parameter(monkeypatch)
    assert tool.main(["--check"], root=tmp_path) == 1
    assert tool.main([], root=tmp_path) == 0
    assert tool.main(["--check"], root=tmp_path) == 0
    additive, breaking = tool.compare()
    assert any("design_schema" in item and "input_syntax" in item for item in breaking)
    assert tool.main(["--compare"]) == 1


def test_an_additive_change_passes_against_the_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    signature = inspect.signature(Workspace.run)
    extra = inspect.Parameter(
        "extra", inspect.Parameter.KEYWORD_ONLY, default=False, annotation=bool
    )
    original = Workspace.run

    def run(self: Workspace, *args: Any, **kwargs: Any) -> Any:
        return original(self, *args, **kwargs)

    run.__signature__ = signature.replace(  # type: ignore[attr-defined]
        parameters=[*signature.parameters.values(), extra]
    )
    run.__module__ = Workspace.__module__  # its annotations are texts of that module
    monkeypatch.setattr(Workspace, "run", run)
    monkeypatch.setattr(Workspace, "added", lambda self: None, raising=False)
    additive, breaking = tool.compare()
    assert not breaking, breaking
    assert any("Workspace.run" in item and "extra" in item for item in additive)
    assert any("Workspace.added" in item for item in additive)


def test_a_break_needs_a_new_major_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutil.copytree(ROOT / "tests" / "baselines", tmp_path / "tests" / "baselines")
    baselines = tmp_path / "tests" / "baselines"
    assert tool.main(["--release", "1.0"], root=tmp_path) == 1  # a baseline is written once
    _drop_a_parameter(monkeypatch)
    assert tool.main(["--release", "1.1"], root=tmp_path) == 1
    assert tool.main(["--release", "2.0"], root=tmp_path) == 1
    assert tool.main(["--release", "1.1", "--accept-breaking"], root=tmp_path) == 1
    assert not (baselines / "1.1").exists()
    assert not (baselines / "2.0").exists()
    assert tool.main(["--release", "2.0", "--accept-breaking"], root=tmp_path) == 0
    breaking = json.loads((baselines / "2.0" / "BREAKING.json").read_text(encoding="utf-8"))
    assert any("design_schema" in item for item in breaking["breaking"])
    assert (baselines / "2.0" / "MANIFEST.sha256").is_file()
    assert tool.newest_baseline(tmp_path) == baselines / "2.0"
