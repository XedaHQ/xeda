"""A launch from Python writes nothing to the standard streams of the program.

The output of a tool goes to `logs/<program>.log` in the run directory. It reaches the terminal
only when the caller asks (`proc_utils.TOOL_OUTPUT`). The library adds no handler that writes to
the `xeda` logger and calls no `warnings.warn`. The log records of a launch say which launch and
which node they belong to, also when launches run at once in several threads.
"""

import logging
import subprocess
import sys
import threading
import warnings
from pathlib import Path

import pytest

from xeda import DefaultRunner, Design
from xeda.flow import Flow
from xeda.flow_runner.launch_result import launch_result
from xeda.outcomes import Result
from xeda.spec import Spec
from xeda.log_context import current_launch_id, launch_context, running_node
from xeda.proc_utils import TOOL_OUTPUT, child_stdout, run_child, run_process
from xeda.tool import Tool, log_stem
from xeda.utils import NonZeroExitCode

from .flow_registry import preserved_flow_registry

pytestmark = pytest.mark.python_compat

PROGRAM = Path(sys.executable).name


@pytest.fixture(autouse=True)
def application_logging_off(monkeypatch):
    """An earlier test of the process may have left a handler on the root logger that writes log
    records to a stream. That output belongs to the application and not to the library."""
    kept = [h for h in logging.root.handlers if type(h).__module__.startswith("_pytest")]
    monkeypatch.setattr(logging.root, "handlers", kept)


CHATTER = "import sys; print('tool says hi'); print('tool says oops', file=sys.stderr)"


def _design(tmp_path: Path) -> Design:
    return Design(name="quiet", design_root=tmp_path, rtl={"sources": [], "top": "t"})


def _chatty_flow(times: int = 1, seen: list | None = None) -> type[Flow]:
    class ChattyFlow(Flow):
        """A flow that starts a program that writes to both of its streams."""

        def run(self):
            if seen is not None:
                seen.append(self.run_directory.path)
            for _ in range(times):
                Tool(sys.executable, version_flag=None).run("-c", CHATTER)

        def parse_reports(self):
            return True

    return ChattyFlow


class _Launched:
    """What a launch left: its `Result`, and the run directory of its one node."""

    def __init__(self, result: Result, flow_class: type[Flow]) -> None:
        self.result = result
        self.run_path = result.nodes[flow_class.name].run_dir

    @property
    def succeeded(self) -> bool:
        return self.result.passed


def _launch(tmp_path: Path, flow_class: type[Flow], name: str = "runs", **launcher) -> _Launched:
    runner = DefaultRunner(tmp_path / name, display_results=False, **launcher)
    design = _design(tmp_path)
    result = launch_result(
        runner, Spec(flow_class.name, design), lambda: runner.run_flow(flow_class, design, {})
    )
    return _Launched(result, flow_class)


def test_a_library_launch_writes_nothing_to_the_standard_streams(tmp_path, capfd):
    with preserved_flow_registry():
        flow = _launch(tmp_path, _chatty_flow())
    out, err = capfd.readouterr()
    assert (out, err) == ("", "")
    text = (flow.run_path / "logs" / f"{PROGRAM}.log").read_text()
    assert "tool says hi" in text and "tool says oops" in text


def test_a_program_that_runs_again_adds_to_its_log(tmp_path):
    with preserved_flow_registry():
        flow = _launch(tmp_path, _chatty_flow(times=2))
    text = (flow.run_path / "logs" / f"{PROGRAM}.log").read_text()
    assert text.count("tool says hi") == 2 and text.count("tool says oops") == 2


def test_a_later_launch_replaces_the_log_of_an_earlier_one(tmp_path):
    """A second launch of the flow in the same run directory makes the log anew."""
    with preserved_flow_registry():
        flow_class = _chatty_flow()
        first = _launch(tmp_path, flow_class)
        log_file = first.run_path / "logs" / f"{PROGRAM}.log"
        log_file.write_text("left by an earlier run\n")
        second = _launch(tmp_path, flow_class, rebuild_all=True)
    assert second.run_path == first.run_path
    text = log_file.read_text()
    assert "left by an earlier run" not in text and text.count("tool says hi") == 1


def test_terminal_output_reaches_the_streams(tmp_path, capfd):
    token = TOOL_OUTPUT.set("terminal")
    try:
        with preserved_flow_registry():
            flow = _launch(tmp_path, _chatty_flow())
    finally:
        TOOL_OUTPUT.reset(token)
    out, err = capfd.readouterr()
    assert "tool says hi" in out and "tool says oops" in err
    assert "tool says hi" in (flow.run_path / "logs" / f"{PROGRAM}.log").read_text()


def test_the_log_name_drops_a_program_suffix():
    assert log_stem("/opt/tools/bin/vivado") == "vivado"
    assert log_stem("xtclsh.exe") == "xtclsh"
    assert log_stem("python3.12") == "python3.12"


def test_a_process_with_no_log_sends_its_output_to_the_logger(tmp_path, capfd, caplog):
    with caplog.at_level(logging.DEBUG, logger="xeda"):
        run_process(sys.executable, ["-c", CHATTER])
    assert capfd.readouterr() == ("", "")
    assert "tool says hi" in caplog.text and "tool says oops" in caplog.text


def test_a_captured_process_keeps_its_error_output_off_the_streams(capfd):
    out = run_process(sys.executable, ["-c", CHATTER], stdout=True)
    assert out == "tool says hi"
    assert capfd.readouterr() == ("", "")


def test_a_child_with_no_log_writes_nowhere_unless_asked(capfd, caplog):
    assert child_stdout() is not None  # the null device
    with caplog.at_level(logging.INFO, logger="xeda"):
        done = run_child([sys.executable, "-c", CHATTER], source="generator")
    assert done.returncode == 0
    assert capfd.readouterr() == ("", "")
    assert "generator: tool says hi" in caplog.text and "generator: tool says oops" in caplog.text
    token = TOOL_OUTPUT.set("terminal")
    try:
        assert child_stdout() is None
        run_child([sys.executable, "-c", CHATTER], source="generator")
    finally:
        TOOL_OUTPUT.reset(token)
    out, err = capfd.readouterr()
    assert "tool says hi" in out and "tool says oops" in err


def test_the_xeda_logger_has_a_null_handler_and_no_other_handler():
    handlers = logging.getLogger("xeda").handlers
    assert any(isinstance(handler, logging.NullHandler) for handler in handlers)
    assert all(isinstance(handler, logging.NullHandler) for handler in handlers)


def test_a_library_launch_emits_no_warning(tmp_path):
    with preserved_flow_registry():
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            _launch(tmp_path, _chatty_flow())


def test_a_library_launch_adds_no_handler_and_changes_no_level(tmp_path):
    package = logging.getLogger("xeda")
    handlers, level = list(package.handlers), package.level
    root_handlers, root_level = list(logging.root.handlers), logging.root.level
    with preserved_flow_registry():
        _launch(tmp_path, _chatty_flow())
        DefaultRunner(tmp_path / "debug", display_results=False, debug=True)
    assert (package.handlers, package.level) == (handlers, level)
    assert (logging.root.handlers, logging.root.level) == (root_handlers, root_level)


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _collected(run):
    """Run `run()` with a handler on the `xeda` logger that keeps every record; return them."""
    collect, package = _Collect(), logging.getLogger("xeda")
    level = package.level
    package.addHandler(collect)
    package.setLevel(logging.DEBUG)
    try:
        run()
    finally:
        package.removeHandler(collect)
        package.setLevel(level)
    return collect.records


def test_each_launch_tags_its_records_with_its_own_id_and_node(tmp_path):
    ids: list[str | None] = []

    class Tagging(Flow):
        """A flow that logs from its `run`."""

        def run(self):
            ids.append(current_launch_id())
            logging.getLogger("xeda.test_quiet_library").info("running")

        def parse_reports(self):
            return True

    launched: list[_Launched] = []

    def launches():
        with preserved_flow_registry():
            for name in ("one", "two"):  # one thread each, one after the other
                (tmp_path / name).mkdir()
                thread = threading.Thread(
                    target=lambda: launched.append(_launch(tmp_path / name, Tagging))
                )
                thread.start()
                thread.join()

    records = _collected(launches)
    assert len(ids) == 2 and ids[0] != ids[1] and None not in ids
    # the id of a launch is the id of its `Result`
    assert [item.result.launch_id for item in launched] == ids
    mine = [r for r in records if r.getMessage() == "running"]
    assert [r.xeda_launch_id for r in mine] == ids
    assert {r.xeda_node for r in mine} == {"tagging"}
    # the records that the launcher writes during a launch carry the id too
    tagged = {r.xeda_launch_id for r in records if r.name.startswith("xeda.flow_runner")}
    assert set(ids) <= tagged <= {None, *ids}


def test_launches_that_run_at_once_in_two_threads_tag_their_own_records():
    """Each thread runs in a context of its own, so the id and the node of one launch never reach
    the records of another."""
    both_running = threading.Barrier(2, timeout=60)
    ids: dict[str, str] = {}

    def launch(name: str) -> None:
        with launch_context() as launch_id, running_node(name):
            ids[name] = launch_id
            logging.getLogger("xeda.test_quiet_library").info("begin %s", name)
            both_running.wait()
            logging.getLogger("xeda.test_quiet_library").info("end %s", name)

    def both() -> None:
        threads = [threading.Thread(target=launch, args=(n,)) for n in ("one", "two")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    records = _collected(both)
    assert len(set(ids.values())) == 2
    for name, launch_id in ids.items():
        mine = [r for r in records if r.getMessage() in (f"begin {name}", f"end {name}")]
        assert len(mine) == 2
        assert {(r.xeda_launch_id, r.xeda_node) for r in mine} == {(launch_id, name)}


def test_a_record_outside_a_launch_has_no_launch_id():
    record = logging.LogRecord("xeda.x", logging.INFO, __file__, 1, "outside", None, None)
    logging.getLogger("xeda").handle(record)
    assert (record.xeda_launch_id, record.xeda_node) == (None, None)


def test_a_launch_inside_a_launch_keeps_the_outer_id():
    with launch_context() as outer, launch_context() as inner:
        assert inner == outer == current_launch_id()
    assert current_launch_id() is None


@pytest.mark.parametrize("kind", ["file", "dangling link", "link out"])
def test_a_logs_entry_that_is_no_directory_costs_a_log_and_not_the_run(tmp_path, caplog, kind):
    """A tool may leave `logs` as a file or a link. The run goes on without that log, and nothing
    is written through the link."""
    outside = tmp_path / "outside"
    outside.mkdir()

    class Blocked(Flow):
        """A flow whose `logs` is no directory when its tool starts."""

        def run(self):
            logs = self.run_path / "logs"
            if kind == "file":
                logs.write_text("not a directory\n")
            elif kind == "dangling link":
                logs.symlink_to(self.run_path / "nowhere")
            else:
                logs.symlink_to(outside)
            Tool(sys.executable, version_flag=None).run("-c", CHATTER)

        def parse_reports(self):
            return True

    with preserved_flow_registry(), caplog.at_level(logging.WARNING, logger="xeda"):
        flow = _launch(tmp_path, Blocked)
    assert flow.succeeded
    assert "No log of" in caplog.text
    assert list(outside.iterdir()) == []
    assert (flow.run_path / "logs").is_symlink() or (flow.run_path / "logs").is_file()


def test_a_failing_generator_leaves_its_output_in_the_log(caplog):
    with caplog.at_level(logging.INFO, logger="xeda"):
        with pytest.raises(subprocess.CalledProcessError):
            run_child(
                [sys.executable, "-c", "print('why it failed'); raise SystemExit(3)"],
                source="generator",
                check=True,
            )
    assert "generator: why it failed" in caplog.text


def test_a_helper_with_no_log_says_the_end_of_its_output_when_it_fails(capfd):
    code = "\n".join(["import sys"] + [f"print('line {n}')" for n in range(30)] + ["sys.exit(4)"])
    with pytest.raises(NonZeroExitCode) as raised:
        run_process(sys.executable, ["-c", code])
    message = str(raised.value)
    assert "line 29" in message and "line 10" in message and "line 9\n" not in message
    assert capfd.readouterr() == ("", "")


def _write_design(tmp_path: Path) -> Path:
    design = tmp_path / "quiet.yaml"
    design.write_text("name: quiet\nrtl:\n  sources: []\n  top: t\n")
    return design


def test_the_command_line_shows_the_results_table_and_json_does_not(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from xeda.cli import cli

    monkeypatch.chdir(tmp_path)
    design = _write_design(tmp_path)
    with preserved_flow_registry():
        flow_class = _chatty_flow()
        shown = CliRunner().invoke(
            cli, ["run", flow_class.name, str(design), "--run-root", str(tmp_path / "shown")]
        )
        assert shown.exit_code == 0, shown.output
        # the narrow console of the runner wraps the title
        assert f"Results of flow:{flow_class.name} design:quiet" in " ".join(shown.output.split())
        assert "tool says hi" in shown.output  # the tool output, as a person sees it
        document = CliRunner().invoke(
            cli,
            ["run", flow_class.name, str(design), "--run-root", str(tmp_path / "doc"), "--json"],
        )
        assert document.exit_code == 0, document.output
    assert "Results of flow" not in document.stdout


def test_the_library_prints_no_table_and_calls_no_print_outside_the_command_line():
    """No module of the package but the command line writes to the console, to the standard
    streams or to the warnings machinery. (`flow_runner/scrub.py` still asks on the console.)"""
    import ast

    import xeda

    package = Path(xeda.__file__).parent
    command_line = {
        "cli.py",
        "cli_utils.py",
        "pdk_cli.py",
        "cli_completion.py",
        "flows/openroad/data/def2stream.py",  # a script that a flow gives to a tool
        "remote/commands.py",
    }
    allowed = {"flow_runner/scrub.py"}
    found: list[str] = []
    for path in sorted(package.rglob("*.py")):
        relative = path.relative_to(package).as_posix()
        if relative in command_line or relative in allowed:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            function = ast.unparse(node.func)
            if function in ("print", "console.print", "console.input", "warnings.warn"):
                found.append(f"{relative}:{node.lineno}: {function}")
    assert not found, found


def test_a_failed_result_names_the_launch_of_the_records_of_the_launch_that_failed(tmp_path):
    from xeda.flow_runner.launch_result import failed_result
    from xeda.log_context import launch_id_of
    from xeda.records import ErrorInfo

    runner = DefaultRunner(tmp_path / "runs", display_results=False)
    design = _design(tmp_path)
    spec = Spec("chatty_flow", design)
    raised: list[Exception] = []

    def launch():
        logging.getLogger("xeda.test_quiet_library").info("inside")
        raise RuntimeError("a bug in the launcher")

    def run():
        try:
            launch_result(runner, spec, launch)
        except RuntimeError as error:
            raised.append(error)

    records = _collected(run)
    (inside,) = [r for r in records if r.getMessage() == "inside"]
    assert launch_id_of(raised[0]) == inside.xeda_launch_id is not None
    error = ErrorInfo("internal_error", "a bug", None, None, None)
    result = failed_result(runner, spec, None, error, launch_id=launch_id_of(raised[0]))
    assert result.launch_id == inside.xeda_launch_id


def test_a_failed_capture_says_the_last_lines_of_its_output() -> None:
    """A captured command has no log; when it fails, its error says what it printed."""
    script = "import sys; print('the reason'); print('more', file=sys.stderr); sys.exit(3)"
    with pytest.raises(NonZeroExitCode) as failed:
        run_process(sys.executable, ["-c", script], stdout=True)
    assert "the reason" in str(failed.value) and "more" in str(failed.value)
