"""The public errors and the stable codes of the failures of a launch."""

import ast
import json
import pickle
from pathlib import Path

import pytest

from xeda import errors
from xeda.deliver import DeliveryError
from xeda.flow import (
    Flow,
    FlowDependencyFailure,
    FlowFatalError,
    FlowSettingsError,
    FlowSettingsException,
    registered_flows,
)
from xeda.flow.flow import FlowException
from xeda.flow.sim import SimFlow, judge_evidence
from xeda.flow_runner import DefaultRunner, chains
from xeda.flow_runner.chains import ChainRefusal, parse_request
from xeda.flow_runner.error_codes import (
    error_info,
    missing_report_info,
    public_error,
    settings_codes,
)
from xeda.flow_runner.launch_report import adoptable_document
from xeda.flow_runner.resolver import check_launchable
from xeda.flow_runner.settings_layers import FlowNotFoundError, FlowRemovedError
from xeda.flows import Nextpnr
from xeda.proc_utils import ProcessTimeout
from xeda.records import ErrorInfo
from xeda.run_dir import RunDirectoryError
from xeda.run_root import RunRootError, ensure_run_root
from xeda.utils import ExecutableNotFound, NonZeroExitCode, ToolException

from . import test_failure_results as failing
from .flow_registry import preserved_flow_registry
from .test_sim_evidence import _Stub


def test_the_tree_is_the_public_contract():
    assert issubclass(errors.SettingsError, errors.SpecError)
    for name in ("DesignError", "ProjectError", "FlowNotFoundError", "ChainError", "RunRootError"):
        assert issubclass(getattr(errors, name), errors.SpecError)
    for name in (
        "StalePlanError",
        "RecordError",
        "RunReplacedError",
        "RemoteError",
        "RunFailedError",
        "LeaseHeldError",
    ):
        assert issubclass(getattr(errors, name), errors.XedaError)
        assert not issubclass(getattr(errors, name), errors.SpecError)
    assert errors.__all__ == [
        "XedaError",
        "SpecError",
        "SettingsError",
        "DesignError",
        "ProjectError",
        "FlowNotFoundError",
        "ChainError",
        "RunRootError",
        "StalePlanError",
        "RecordError",
        "RunReplacedError",
        "RemoteError",
        "RunFailedError",
        "LeaseHeldError",
    ]


def test_every_attribute_after_the_message_is_keyword_only():
    for name in errors.__all__:
        with pytest.raises(TypeError):
            getattr(errors, name)("message", "code")


def test_an_error_holds_its_code_and_message_and_survives_pickling():
    error = errors.RunRootError("not a run root", code="invalid_run_root", path=Path("x"))
    again = pickle.loads(pickle.dumps(error))
    assert (again.code, again.message, again.path, str(again)) == (
        "invalid_run_root",
        "not a run root",
        Path("x"),
        "not a run root",
    )
    assert isinstance(again, errors.SpecError) and again.points == ()
    unknown = pickle.loads(pickle.dumps(FlowNotFoundError("nosuch", ["nosuch_flow"])))
    assert isinstance(unknown, errors.FlowNotFoundError)
    assert (unknown.code, unknown.name, unknown.suggestions) == (
        "flow_not_found",
        "nosuch",
        ("nosuch_flow",),
    )


def test_the_internal_errors_are_public_errors_with_a_code(tmp_path):
    assert FlowNotFoundError("x").code == "flow_not_found"
    assert FlowNotFoundError("x").name == "x"
    removed = FlowRemovedError("openxc7", "fpga_pack")
    assert isinstance(removed, errors.FlowNotFoundError) and removed.code == "flow_not_found"
    (tmp_path / "file").write_text("x")
    with pytest.raises(errors.RunRootError) as refused:
        ensure_run_root(tmp_path / "file", start=tmp_path)
    assert isinstance(refused.value, RunRootError)
    assert (refused.value.code, refused.value.path) == ("invalid_run_root", tmp_path / "file")


def test_an_error_record_round_trips_through_json():
    info = ErrorInfo(code="nonzero_exit", message="m", node="n", exit_status=3, log="log")
    assert ErrorInfo.from_json(json.loads(json.dumps(info.as_json_value()))) == info
    assert list(info.as_json_value()) == ["code", "message", "node", "exit_status", "log"]
    for bad in (
        {"message": "m"},
        {"code": "c", "message": 1},
        {"code": "c", "message": "m", "exit_status": "3"},
        "text",
        None,
    ):
        with pytest.raises(ValueError):
            ErrorInfo.from_json(bad)  # type: ignore[arg-type]


# Each exception has its real constructor.
@pytest.mark.parametrize(
    "exc, code, exit_status",
    [
        (NonZeroExitCode(["tool"], 3), "nonzero_exit", 3),
        (ProcessTimeout(["tool"], 5.0), "timeout", None),
        (ExecutableNotFound("yosys"), "executable_not_found", None),
        (
            FlowDependencyFailure("producer yosys_fpga failed", producer="yosys_fpga"),
            "producer_failed",
            None,
        ),
        (FlowFatalError("boom"), "reported_failure", None),
        (RuntimeError("bug"), "internal_error", None),
    ],
)
def test_a_node_failure_has_its_code(exc, code, exit_status):
    info = error_info(exc, node="n", log="log")
    assert (info.code, info.node, info.exit_status, info.log) == (code, "n", exit_status, "log")
    assert info.message == str(exc)


@pytest.mark.parametrize(
    "exc, code",
    [
        (OSError("disk full"), "storage_error"),
        (PermissionError(13, "read-only"), "storage_error"),
        (RunDirectoryError("leads out of the run directory"), "storage_error"),
        (FlowSettingsException("needs a setting"), "reported_failure"),
        (FlowException("flow"), "reported_failure"),
        (ToolException("tool too old"), "reported_failure"),
        (DeliveryError("refused"), "reported_failure"),
        (DeliveryError("gone", output_problem=True), "missing_output"),
        (DeliveryError("early", before_run=True), "reported_failure"),
    ],
)
def test_a_failure_the_launcher_expects_is_not_an_internal_error(exc, code):
    assert error_info(exc, node="n").code == code


def test_a_missing_executable_wins_over_an_operating_system_error():
    class NotFound(ExecutableNotFound, OSError):
        pass

    assert error_info(NotFound("yosys"), node=None).code == "executable_not_found"


def test_only_a_bug_is_an_internal_error():
    for exc in (RuntimeError("bug"), KeyError("k"), AssertionError("a")):
        assert error_info(exc, node=None).code == "internal_error"


def test_no_code_is_derived_from_a_class_name():
    class ProcessTimeoutLike(RuntimeError):
        pass

    for exc in (ProcessTimeoutLike("x"), KeyError("x"), ValueError("x")):
        assert error_info(exc, node=None).code == "internal_error"
    for exc in (NonZeroExitCode(["t"], 1), FlowFatalError("x"), ExecutableNotFound("y")):
        assert error_info(exc, node=None).code != type(exc).__name__.lower()


def _error(kind, location="x", message="problem"):
    return (location, message, None, kind)


def test_settings_errors_map_by_the_documented_table():
    kinds = {
        "extra_forbidden": "unknown_setting",
        "missing": "required_setting",
        "quantity": "invalid_quantity",
        "boolean_text": "boolean_text",
        "list_text": "list_text",
        "deliverable_switch": "deliverable_switch",
        "int_parsing": "invalid_value",
        "literal_error": "invalid_value",
        "value_error": "invalid_value",
        "shared_setting_conflict": "shared_setting_conflict",
        "conflicting_spellings": "conflicting_keys",
        "removed_setting": "removed_setting",
        "settings_group": "unsupported_setting",
        None: "invalid_value",
    }
    raised = FlowSettingsError([_error(kind) for kind in kinds], Nextpnr.Settings)
    assert settings_codes(raised) == list(kinds.values())


def test_a_removed_setting_is_refused_with_its_code():
    class Removed(Flow):
        class Settings(Flow.Settings):
            new: int = 1

            removed_settings = {"old": "new"}

        def run(self) -> None:
            pass

    with preserved_flow_registry():
        with pytest.raises(FlowSettingsError) as refused:
            Removed.Settings.from_input({"old": 1})
    assert settings_codes(refused.value) == ["removed_setting"]
    assert public_error(refused.value).message.endswith("`old` was removed: use new")


def test_an_unknown_setting_suggests_the_close_name():
    with pytest.raises(FlowSettingsError) as refused:
        Nextpnr.Settings.from_input({"sed": 1})
    error = public_error(refused.value)
    assert isinstance(error, errors.SettingsError)
    assert (error.code, error.node, error.setting) == ("unknown_setting", "nextpnr", "sed")
    assert error.suggestions == ("seed",)
    assert error.errors == (error,)


def test_the_first_finding_holds_all_the_findings_in_address_order():
    with pytest.raises(FlowSettingsError) as refused:
        Nextpnr.Settings.from_input({"sed": 1, "seed": "x"})
    error = public_error(refused.value)
    assert [finding.setting for finding in error.errors] == ["sed", "seed"]
    assert error.errors[0] is error and error.setting == "sed"
    assert [finding.code for finding in error.errors] == ["unknown_setting", "invalid_value"]
    with pytest.raises(Exception) as bare:
        Nextpnr.Settings(sed=1)
    assert public_error(bare.value).code == "unknown_setting"


def test_a_missing_required_setting_and_an_unsupported_one_have_their_codes(tmp_path):
    required = FlowSettingsException("needs", code="required_setting")
    assert public_error(required).code == "required_setting"
    assert public_error(FlowSettingsException("no")).code == "unsupported_setting"


@pytest.mark.parametrize(
    "request_text, code",
    [
        ("yosys_fpga+", "invalid_chain"),
        ("yosys_fpga.a.b+nextpnr", "invalid_chain"),
        ("nextpnr.config", "invalid_chain"),
        ("yosys_fpga+yosys_fpga", "repeated_flow"),
        ("yosys_fpga+nextpnr+yosys_fpga", "repeated_flow"),
        ("openfpgaloader+nextpnr", "action_not_last"),
        ("ghdl_sim+nextpnr", "no_edge"),
        ("yosys_fpga.bogus+nextpnr", "unknown_output"),
    ],
)
def test_a_refused_chain_has_its_code(request_text, code):
    with pytest.raises(ChainRefusal) as refused:
        parse_request(request_text)
    error = public_error(refused.value)
    assert isinstance(error, errors.ChainError)
    assert (error.code, error.chain) == (code, request_text)
    assert error.problem == str(refused.value)
    assert pickle.loads(pickle.dumps(refused.value)).code == code


def test_every_refusal_in_the_chain_module_names_its_code():
    """A chain refusal is a `ChainRefusal` with a code: none is a bare settings exception."""
    module = ast.parse(Path(chains.__file__).read_text())
    for node in ast.walk(module):
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            called = node.exc.func
            assert not (
                isinstance(called, ast.Name) and called.id == "FlowSettingsException"
            ), node.lineno
            if isinstance(called, ast.Name) and called.id == "ChainRefusal":
                assert "code" in {keyword.arg for keyword in node.exc.keywords}, node.lineno


def test_other_exceptions_map_to_their_public_errors():
    internal = public_error(ValueError("bug"))
    assert type(internal) is errors.XedaError and internal.code == "internal_error"
    already = errors.StalePlanError("changed", code="stale_plan", difference="a")
    assert public_error(already) is already


def test_a_simulation_without_an_end_record_has_a_coded_error():
    flow = _Stub()
    assert judge_evidence(flow, None, "error") is False
    assert flow.results["error"] == {
        "code": "reported_failure",
        "message": flow.results["error"]["message"],
        "node": "stub_sim",
        "exit_status": None,
        "log": None,
    }
    assert "no end record" in flow.results["error"]["message"]
    assert issubclass(SimFlow, Flow)


@pytest.fixture
def producers():
    with preserved_flow_registry():
        registered_flows.update(failing._REGISTERED)
        yield


@pytest.mark.parametrize(
    "producer, reason",
    [({"report_failure": True}, "reported failure"), ({"fail": True}, "boom")],
    ids=["reports failure", "raises"],
)
def test_a_consumer_message_names_the_producer_code_not_a_dict(
    tmp_path, producers, producer, reason
):
    design = failing._design(tmp_path)
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    with pytest.raises(FlowDependencyFailure) as refused:
        runner.run_flow(
            failing._NeedsMayFail, design, {}, all_flows_settings={"__may_fail": producer}
        )
    message = str(refused.value)
    assert "__may_fail" in message and "(reported_failure)" in message and reason in message
    assert "{" not in message and "'type'" not in message
    document = json.loads(
        (tmp_path / "run" / "d" / failing._NeedsMayFail.name / "results.json").read_text()
    )
    assert document["error"]["code"] == "producer_failed"
    assert document["error"]["message"] == message
    assert document["error"]["node"] == failing._NeedsMayFail.name


def test_a_run_that_lacks_a_report_it_asked_for_has_the_missing_report_code(tmp_path):
    class NoReport(Flow):
        results_description = {}

        def run(self) -> None:
            pass

        def parse_reports(self) -> bool:
            return self.report_file(self.run_path / "absent.rpt") is not None

    with preserved_flow_registry():
        flow = DefaultRunner(tmp_path / "run", display_results=False).run_flow(
            NoReport, failing._design(tmp_path), {}
        )
    document = json.loads((flow.run_path / "results.json").read_text())
    assert document["error"]["code"] == "missing_report"
    assert "absent.rpt" in document["error"]["message"]
    assert flow.reports_missing == [flow.run_path / "absent.rpt"]


def test_a_rejected_run_is_adopted_by_its_code_and_message():
    error = ErrorInfo("rejected", "slack", "n", None, None).as_json_value()
    document = {"success": False, "error": dict(error)}
    assert adoptable_document(document, "rejected", error)
    assert not adoptable_document(
        {"success": False, "error": {**error, "message": "other"}}, "rejected", error
    )
    assert not adoptable_document(
        {"success": False, "error": {**error, "code": "nonzero_exit"}}, "rejected", error
    )
    assert not adoptable_document(
        document, "rejected", {"type": "ReportedFailure", "message": "slack"}
    )


def test_no_reader_of_a_node_error_looks_for_a_class_name():
    """A node records a code. The files that build a top-level `--json` document (which still
    names the class of the exception) are the only places that spell these names."""
    top_level = {"cli.py", "cli_utils.py", "pdk_cli.py"}
    old_types = {"ReportedFailure", "MissingOutput", "NonZeroExitCode", "ProcessTimeout"}
    root = Path(errors.__file__).parent
    found = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if relative.parts[0] == "remote" or path.name in top_level:
            continue
        tree = ast.parse(path.read_text())
        exported = {
            id(name)
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets)
            for name in ast.walk(node.value)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and node.value in old_types
                and id(node) not in exported
            ):
                found.append(f"{relative}:{node.lineno}")
    assert not found


def test_a_refusal_of_unsupported_settings_has_the_unsupported_code(tmp_path):
    class Refuses(Flow):
        results_description = {}

        class Settings(Flow.Settings):
            knob: int = 1

        @classmethod
        def check_settings_supported(cls, settings):
            super().check_settings_supported(settings)
            raise FlowSettingsError([("knob", "no knob", None, "value_error")], cls.Settings)

        def run(self) -> None:
            pass

    with preserved_flow_registry():
        with pytest.raises(FlowSettingsError) as refused:
            check_launchable(Refuses, Refuses.Settings(), failing._design(tmp_path))
    assert settings_codes(refused.value) == ["unsupported_setting"]
    assert public_error(refused.value).code == "unsupported_setting"
    # the same finding from the validation of a value is an invalid value
    assert settings_codes(FlowSettingsError([("knob", "no", None, "value_error")], object)) == [
        "invalid_value"
    ]


def test_a_project_error_keeps_its_code_through_pickling():
    from xeda.flow_runner.default_runner import DesignNotFoundError

    error = DesignNotFoundError("no design", code="unknown_design")
    again = pickle.loads(pickle.dumps(error))
    assert (type(again), again.code, str(again)) == (
        DesignNotFoundError,
        "unknown_design",
        "no design",
    )
    assert public_error(again).code == "unknown_design"


def test_a_report_read_twice_is_named_once():
    run = Path("/run")
    info = missing_report_info("n", [run / "b.rpt", run / "a.rpt", run / "b.rpt"], run)
    assert info.code == "missing_report" and "(b.rpt, a.rpt)" in info.message


def test_a_launch_outcome_of_the_remote_is_not_a_setup_failure():
    from xeda.remote.errors import RemoteCanceled, RemoteConnectionError, RemoteProvisionError

    assert public_error(RemoteConnectionError("down")).code == "remote_unreachable"
    assert public_error(RemoteProvisionError("env-failed", "x")).code == "remote_setup_failed"
    assert public_error(RemoteCanceled("Ctrl-C")).code != "remote_setup_failed"
