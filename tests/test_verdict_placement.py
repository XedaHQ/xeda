"""A flow judges its results in `check_results()` and calls `reject()`. `parse_reports()` only
reads reports.

A judgment left in `parse_reports()` makes a rejection look like a missing report, and a launch
never replays that. The sweep below finds a judgment in `parse_reports()` of every registered flow
and in the methods that it calls. The tests after the sweep run the moved judgments.
"""

import ast
import inspect
from pathlib import Path
from typing import Any, Callable, Dict, List, Set, Tuple

import pytest

from xeda import Design
from xeda.flow import Flow
from xeda.flows import (
    DiamondSynth,
    IseSynth,
    Nextpnr,
    Quartus,
    VivadoAltSynth,
    VivadoSynth,
)
from xeda.introspect import all_flow_classes
from xeda.run_dir import RunDirectory

from .settings_samples import minimal_settings

#: Settings that gate a verdict in the code of a flow.
SWITCHES = {"fail_timing", "timing_allow_fail", "allow_dsps", "allow_brams"}

#: Results that count a violation. A flow that reads one of them decides on it.
VIOLATION_COUNTS = {
    "setup_violations",
    "hold_violations",
    "_failing_endpoints",
    "_num_unrouted",
    "_timing_errors",
    "num_violating_paths",
    "hold_num_violating_paths",
}

ORDERINGS = (ast.Lt, ast.Gt, ast.LtE, ast.GtE, ast.NotEq)


def _tree(function: Callable[..., Any]) -> ast.AST:
    source = inspect.getsource(function)
    # The prefix lets a method parse without a dedent: a multi-line string in its body can start
    # in column 0.
    return ast.parse(source if source[0] not in " \t" else "if 1:\n" + source)


def judgments(function: Callable[..., Any]) -> List[str]:
    """Return the source of every expression in `function` that decides on a limit: a setting
    that gates a verdict, a read of a violation count, or an ordering against zero."""
    found = []
    for node in ast.walk(_tree(function)):
        if isinstance(node, ast.Attribute) and node.attr in SWITCHES:
            found.append(node.attr)
        if isinstance(node, ast.Compare):
            if any(isinstance(op, ORDERINGS) for op in node.ops) and any(
                isinstance(c, ast.Constant) and c.value == 0 for c in node.comparators
            ):
                found.append(ast.unparse(node))
        if isinstance(node, ast.Subscript) and _is_results(node.value):
            key = node.slice
            if isinstance(key, ast.Constant) and key.value in VIOLATION_COUNTS:
                found.append(ast.unparse(node))
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and _is_results(node.func.value)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value in VIOLATION_COUNTS
        ):
            found.append(ast.unparse(node))
    return found


def _is_results(node: ast.AST) -> bool:
    return ast.unparse(node) == "self.results"


def called_functions(owner: type, function: Callable[..., Any]) -> List[Tuple[str, Callable]]:
    """Return what `function` calls in the code of the flows, transitively, by name: the methods
    of `self` and the functions that receive `self`. Code outside `xeda.flows` is not swept."""
    found: List[Tuple[str, Callable]] = []
    seen: Set[str] = {function.__name__}
    pending = [function]
    while pending:
        current = pending.pop()
        for node in ast.walk(_tree(current)):
            if not isinstance(node, ast.Call):
                continue
            target = None
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"
            ):
                target = getattr(owner, node.func.attr, None)
            elif isinstance(node.func, ast.Name) and any(
                isinstance(a, ast.Name) and a.id == "self" for a in node.args
            ):
                target = current.__globals__.get(node.func.id)
            if (
                inspect.isfunction(target)
                and target.__module__.startswith("xeda.flows")
                and target.__name__ not in seen
            ):
                seen.add(target.__name__)
                found.append((target.__name__, target))
                pending.append(target)
    return found


def parse_reports_owners() -> Dict[str, type]:
    """Every class that defines a `parse_reports` used by a registered xeda flow, by qualname.

    An inherited method is swept once, on the class that defines it (`VivadoImplementation` for
    `vivado_synth`, `vivado_alt_synth` and `vivado_impl`)."""
    owners = {}
    for flow in all_flow_classes():
        if not flow.__module__.startswith("xeda."):
            continue
        owner = next(k for k in flow.__mro__ if "parse_reports" in vars(k))
        if owner is not Flow:
            owners[owner.__qualname__] = owner
    return owners


def swept(owner: type) -> List[Tuple[str, Callable]]:
    parse = vars(owner)["parse_reports"]
    return [("parse_reports", parse), *called_functions(owner, parse)]


#: Classes whose judgment stays in `parse_reports()`, with the reason (keyed by qualname).
JUDGES_IN_PARSE_REPORTS = {
    "Dc": "judges its results in parse_reports(), so its failures are never replayed",
}

#: Expressions that look like a judgment and only decide how to read a report or what to store,
#: by qualname of the class and the name of the function, with the reason.
READS_ONLY: Dict[Tuple[str, str], Dict[str, str]] = {
    ("VivadoImplementation", "parse_reports"): {
        "len(e.args) > 0": "picks the text of a debug message",
    },
    ("VivadoImplementation", "parse_timing_report"): {
        "clock_period <= 0": "rejects a clock period that cannot give a Fmax",
    },
    ("Nextpnr", "_parse_fmax"): {
        "wns >= 0": "stores `timing_met`, a result; the verdict is in `check_results()`",
        "wns < 0": "logs the missed constraint with the numbers that explain it",
    },
    ("Yosys", "get_utilization"): {
        "json_start > 0": "finds where the JSON text starts in the tool output",
    },
    ("YosysFpga", "get_utilization"): {
        "json_start > 0": "finds where the JSON text starts in the tool output",
    },
}


def unexplained(owner: type) -> List[str]:
    found = []
    qualname = owner.__qualname__
    for name, function in swept(owner):
        allowed = READS_ONLY.get((qualname, name), {})
        found += [f"{name}: {j}" for j in judgments(function) if j not in allowed]
    return found


@pytest.mark.parametrize("name", sorted(parse_reports_owners()))
def test_parse_reports_reads_and_check_results_judges(name):
    if name in JUDGES_IN_PARSE_REPORTS:
        pytest.skip(JUDGES_IN_PARSE_REPORTS[name])
    assert unexplained(parse_reports_owners()[name]) == []


def test_a_class_that_judges_in_parse_reports_still_does():
    owners = parse_reports_owners()
    assert set(JUDGES_IN_PARSE_REPORTS) <= set(owners)
    for name in JUDGES_IN_PARSE_REPORTS:
        assert unexplained(owners[name]), f"{name} no longer judges: drop its entry"


def test_every_reads_only_entry_is_still_found():
    owners = {owner.__qualname__: owner for owner in parse_reports_owners().values()}
    for (qualname, function_name), expressions in READS_ONLY.items():
        found = dict(swept(owners[qualname]))[function_name]
        assert set(expressions) <= set(judgments(found)), (qualname, function_name)


def test_the_sweep_finds_a_judgment():
    class Bad:
        results: Dict[str, Any] = {}

        def parse_reports(self):
            return not (self.results["wns"] < 0)

    assert judgments(Bad.parse_reports) == ["self.results['wns'] < 0"]


class _Kinds:
    results: Dict[str, Any] = {}
    settings: Any = None

    def switch(self):
        return self.settings.fail_timing

    def count_read_by_get(self):
        return self.results.get("setup_violations")

    def count_read_by_index(self):
        return self.results["_timing_errors"]

    def none_check(self):
        return self.results["wns"] is None

    def length(self):
        return len(self.results) > 1


@pytest.mark.parametrize(
    "name,expected",
    [
        ("switch", ["fail_timing"]),
        ("count_read_by_get", ["self.results.get('setup_violations')"]),
        ("count_read_by_index", ["self.results['_timing_errors']"]),
        ("none_check", []),
        ("length", []),
    ],
)
def test_the_sweep_finds_each_kind_of_judgment(name, expected):
    assert judgments(getattr(_Kinds, name)) == expected


# --------------------------------------------------------------------------------------------
# The judgments, flow by flow. Each flow is built as the launcher builds it, and gets the
# results that its `parse_reports()` stores.
# --------------------------------------------------------------------------------------------

_DESIGN_FIELDS: Dict[str, Any] = {"sources": [], "top": "dummy", "clock": "clk"}


def build(tmp_path: Path, flow_class: type, **settings: Any) -> Any:
    design = Design(name="dummy", rtl=_DESIGN_FIELDS)  # type: ignore[arg-type]
    run_path = tmp_path / "run"
    run_path.mkdir()
    values = {**minimal_settings(flow_class), **settings}  # type: ignore[arg-type]
    return flow_class(
        values,
        design,
        run_path,
        runner_cwd=tmp_path,
        run_directory=RunDirectory.claimed(run_path, tmp_path),
    )


def assert_rejected(flow: Any, *causes: str) -> None:
    assert flow.check_results() is False
    assert flow.rejection is not None
    for cause in causes:
        assert cause in flow.rejection
    assert flow.results["error"]["message"].endswith(flow.rejection)


def assert_not_rejected(flow: Any) -> None:
    assert flow.check_results() is True
    assert flow.rejection is None


VIVADO_FLOWS = [VivadoSynth, VivadoAltSynth]
MET = {"wns": 0.5, "whs": 0.1, "setup_violations": 0, "hold_violations": 0}


@pytest.mark.parametrize("flow_class", VIVADO_FLOWS)
def test_vivado_accepts_met_timing(tmp_path, flow_class):
    flow = build(tmp_path, flow_class)
    flow.results.update(MET)
    assert_not_rejected(flow)


@pytest.mark.parametrize("flow_class", VIVADO_FLOWS)
@pytest.mark.parametrize(
    "failing,cause",
    [
        ({"wns": -0.123}, "WNS -0.123"),
        ({"whs": -0.045}, "WHS -0.045"),
        ({"setup_violations": 3}, "3 setup violations"),
        ({"hold_violations": 2}, "2 hold violations"),
        ({"_failing_endpoints": 4}, "4 failing endpoints"),
    ],
)
def test_vivado_rejects_missed_timing(tmp_path, flow_class, failing, cause):
    flow = build(tmp_path, flow_class)
    flow.results.update({**MET, **failing})
    assert_rejected(flow, "timing not met", cause)
    assert flow.results["wns"] == {**MET, **failing}["wns"]  # the numbers stay in the results


@pytest.mark.parametrize("flow_class", VIVADO_FLOWS)
def test_vivado_names_every_cause_in_one_message(tmp_path, flow_class):
    flow = build(tmp_path, flow_class)
    flow.results.update({**MET, "wns": -0.1, "whs": -0.2})
    assert_rejected(flow, "WNS -0.1", "WHS -0.2")
    assert flow.rejection.count("timing not met") == 1


@pytest.mark.parametrize("flow_class", VIVADO_FLOWS)
def test_vivado_accepts_missed_timing_without_fail_timing(tmp_path, flow_class):
    flow = build(tmp_path, flow_class, fail_timing=False)
    flow.results.update({**MET, "wns": -0.1, "whs": -0.2, "setup_violations": 3})
    assert_not_rejected(flow)


@pytest.mark.parametrize("flow_class", VIVADO_FLOWS)
def test_vivado_judges_only_what_was_parsed(tmp_path, flow_class):
    flow = build(tmp_path, flow_class)
    assert_not_rejected(flow)


NEXTPNR_MISSED = {"fmax": {"clk": {"achieved": 100.0, "constraint": 200.0}}}


def _nextpnr(tmp_path, report, **settings):
    import json

    flow = build(tmp_path, Nextpnr, **settings)
    (flow.run_path / str(flow.settings.report)).write_text(json.dumps(report))
    return flow


def test_nextpnr_rejects_missed_timing(tmp_path):
    flow = _nextpnr(tmp_path, NEXTPNR_MISSED)
    assert flow.parse_reports() is True
    assert_rejected(flow, "timing not met", "100", "200")
    assert flow.results["fmax"] == pytest.approx(100.0)


def test_nextpnr_accepts_met_timing(tmp_path):
    flow = _nextpnr(tmp_path, {"fmax": {"clk": {"achieved": 250.0, "constraint": 200.0}}})
    assert flow.parse_reports() is True
    assert_not_rejected(flow)


def test_nextpnr_accepts_missed_timing_with_timing_allow_fail(tmp_path):
    flow = _nextpnr(tmp_path, NEXTPNR_MISSED, timing_allow_fail=True)
    assert flow.parse_reports() is True
    assert_not_rejected(flow)
    assert flow.results["timing_met"] is False


def test_nextpnr_without_a_report_rejects_nothing(tmp_path):
    flow = build(tmp_path, Nextpnr)
    assert flow.parse_reports() is False
    assert_not_rejected(flow)


def test_nextpnr_judges_only_the_last_parse(tmp_path):
    flow = _nextpnr(tmp_path, NEXTPNR_MISSED)
    flow.parse_reports()
    (flow.run_path / str(flow.settings.report)).write_text(
        '{"fmax": {"clk": {"achieved": 250.0, "constraint": 200.0}}}'
    )
    flow.parse_reports()
    assert_not_rejected(flow)


def test_quartus_rejects_negative_slack(tmp_path):
    flow = build(tmp_path, Quartus)
    flow.results.update({"wns": -0.4, "whs": -0.2, "fmax": 100.0})
    assert_rejected(flow, "setup slack", "-0.4", "hold slack", "-0.2")
    assert flow.results["fmax"] == 100.0


def test_quartus_rejects_only_the_failed_slack(tmp_path):
    flow = build(tmp_path, Quartus)
    flow.results.update({"wns": 0.4, "whs": -0.2})
    assert_rejected(flow, "hold slack")
    assert "setup slack" not in flow.rejection


def test_quartus_accepts_met_timing(tmp_path):
    flow = build(tmp_path, Quartus)
    flow.results.update({"wns": 0.0, "whs": 0.2})
    assert_not_rejected(flow)


def test_quartus_ignores_a_slack_that_is_no_number(tmp_path):
    flow = build(tmp_path, Quartus)
    flow.results.update({"wns": "n/a"})
    assert_not_rejected(flow)


def test_ise_rejects_negative_slack(tmp_path):
    flow = build(tmp_path, IseSynth)
    flow.results["wns"] = "-0.25"
    assert_rejected(flow, "-0.25")


def test_ise_accepts_met_timing(tmp_path):
    flow = build(tmp_path, IseSynth)
    flow.results["wns"] = "1.5"
    assert_not_rejected(flow)


DIAMOND_MET = {
    "wns": 0.2,
    "whs": 0.1,
    "_num_unrouted": 0,
    "_timing_errors": 0,
    "_status": "Completed",
    "dsp": 0,
    "bram": 0,
}


def test_diamond_accepts_a_met_run(tmp_path):
    flow = build(tmp_path, DiamondSynth)
    flow.results.update(DIAMOND_MET)
    assert_not_rejected(flow)


@pytest.mark.parametrize(
    "failing,cause",
    [
        ({"wns": -0.1}, "WNS -0.1"),
        ({"whs": -0.1}, "WHS -0.1"),
        ({"_num_unrouted": 2}, "2 unrouted"),
        ({"_timing_errors": 3}, "3 timing errors"),
    ],
)
def test_diamond_rejects_a_missed_limit(tmp_path, failing, cause):
    flow = build(tmp_path, DiamondSynth)
    flow.results.update({**DIAMOND_MET, **failing})
    assert_rejected(flow, cause)


@pytest.mark.parametrize("setting,resource", [("allow_dsps", "dsp"), ("allow_brams", "bram")])
def test_diamond_rejects_a_forbidden_resource(tmp_path, setting, resource):
    flow = build(tmp_path, DiamondSynth, **{setting: False})
    flow.results.update({**DIAMOND_MET, resource: 4})
    assert_rejected(flow, resource, "4")


@pytest.mark.parametrize("setting,resource", [("allow_dsps", "dsp"), ("allow_brams", "bram")])
def test_diamond_accepts_an_allowed_resource(tmp_path, setting, resource):
    flow = build(tmp_path, DiamondSynth, **{setting: True})
    flow.results.update({**DIAMOND_MET, resource: 4})
    assert_not_rejected(flow)


def test_diamond_names_every_cause_in_one_message(tmp_path):
    flow = build(tmp_path, DiamondSynth, allow_dsps=False)
    flow.results.update({**DIAMOND_MET, "dsp": 1, "wns": -0.1, "_timing_errors": 5})
    assert_rejected(flow, "dsp", "WNS -0.1", "5 timing errors")
    assert flow.rejection.count("; ") == 2


def test_diamond_reads_a_status_that_is_not_completed_as_a_failed_report(tmp_path):
    """A status other than `completed` says the tool did not finish: no verdict, a failed run."""
    flow = build(tmp_path, DiamondSynth)
    flow.results.update({**DIAMOND_MET, "_status": "Failed"})
    assert_not_rejected(flow)
