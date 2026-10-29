"""An OpenROAD process writes only inside its run directory, whatever a tool left there.

A tool may leave symbolic links anywhere in a run directory. OpenROAD follows a link at the name
of a file that it writes, so before the process starts, Xeda removes a link at each name that
the script writes (`OpenroadStage.run_openroad`). The tests run a small flow over the shared base
with the fake OpenROAD, and plant links that lead outside the run directory.
"""

import shutil
from pathlib import Path

import pytest

from xeda.flow import FlowSettingsException
from xeda.flow_runner import DefaultRunner

from .openroad_utils import openroad_design, openroad_settings, probe_flows
from .tiny_platform import tiny_store
from .tool_utils import require_tclsh, use_fake_openroad
from .scoping import node_settings

RECORDED = Path(__file__).parent / "resources" / "openroad" / "metrics" / "openroad_place.json"


class Launch:
    """One launch of the netlist probe, with what the launch left."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probes):
        require_tclsh()
        use_fake_openroad(monkeypatch)
        metrics = tmp_path / "recorded"
        metrics.mkdir()
        shutil.copy(RECORDED, metrics / "openroad_probe.json")
        monkeypatch.setenv("XEDA_FAKE_OPENROAD_METRICS", str(metrics))
        self.tmp_path = tmp_path
        self.probes = probes
        self.root = tmp_path / "xeda_run"
        self.outside = tmp_path / "outside"
        self.outside.mkdir()
        self.design = openroad_design(tmp_path / "design")

    def run_directory(self) -> Path:
        (found,) = self.root.glob("*/t_openroad_probe")
        return found

    def runner(self) -> DefaultRunner:
        return DefaultRunner(self.root, display_results=False)

    def launch(self, design: Path | None = None):
        store = tiny_store(self.tmp_path)
        return self.runner().run(
            self.probes.netlist,
            design or self.design,
            flow_settings=node_settings(self.probes.netlist, openroad_settings(store)),
        )

    def outside_file(self, name: str) -> Path:
        path = self.outside / name
        path.write_text("outside\n")
        return path


@pytest.fixture
def launched(tmp_path, monkeypatch):
    with probe_flows() as probes:
        yield Launch(tmp_path, monkeypatch, probes)


def test_a_link_at_a_written_name_is_replaced_by_a_regular_file(launched):
    outside = {name: launched.outside_file(name) for name in ("metrics", "database", "report")}
    outside_dir = launched.outside / "reports"
    outside_dir.mkdir()

    def leave_links(flow) -> None:
        for name, target in (
            ("metrics.json", outside["metrics"]),
            ("probe.odb", outside["database"]),
            ("reports", outside_dir),
        ):
            link = flow.run_path / name
            if link.is_dir() and not link.is_symlink():
                shutil.rmtree(link)  # the launcher made `reports/`
            link.unlink(missing_ok=True)
            link.symlink_to(target)

    launched.probes.netlist.before_run = leave_links
    flow = launched.launch()
    assert flow is not None and flow.succeeded
    run_dir = launched.run_directory()
    for name in ("metrics.json", "probe.odb"):
        assert (run_dir / name).is_file() and not (run_dir / name).is_symlink(), name
    assert (run_dir / "reports").is_dir() and not (run_dir / "reports").is_symlink()
    assert "write_db" in (run_dir / "probe.odb").read_text()
    assert "detailedplace__timing__setup__ws" in (run_dir / "metrics.json").read_text()
    assert [path.read_text() for path in outside.values()] == ["outside\n"] * 3
    assert list(outside_dir.iterdir()) == []


def test_a_link_at_the_name_of_a_numbered_report_is_removed(launched):
    """Every few iterations, the global router writes `probe-<n>.rpt` beside `probe.rpt`, and
    the detailed router `probe.rpt-<n>.rpt`. A link that an earlier tool left at such a name is
    removed before the process starts, and an entry of another name stays."""
    numbered = ("probe-5.rpt", "probe.rpt-10.rpt")
    targets = {name: launched.outside_file(name) for name in numbered}

    def leave_links(flow) -> None:
        reports = flow.run_path / "reports"
        reports.mkdir(exist_ok=True)
        for name, target in targets.items():
            (reports / name).symlink_to(target)
        (reports / "probe-old.rpt").write_text("kept\n")

    launched.probes.netlist.before_run = leave_links
    flow = launched.launch()
    assert flow is not None and flow.succeeded
    reports = launched.run_directory() / "reports"
    for name in numbered:
        assert not (reports / name).is_symlink() and not (reports / name).exists(), name
    assert [target.read_text() for target in targets.values()] == ["outside\n"] * 2
    assert (reports / "probe-old.rpt").read_text() == "kept\n"


def test_a_link_at_the_name_of_the_failed_database_is_replaced_by_a_regular_file(
    launched, monkeypatch
):
    target = launched.outside_file("failed")

    def leave_link(flow) -> None:
        (flow.run_path / flow.failed_odb).symlink_to(target)

    launched.probes.netlist.before_run = leave_link
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "report_tns_metric")
    flow = launched.launch()  # the process fails, as the script re-raises the error
    assert flow is None or not flow.succeeded
    failed = launched.run_directory() / "t_openroad_probe-failed.odb"
    assert failed.is_file() and not failed.is_symlink()
    assert "write_db" in failed.read_text()
    assert target.read_text() == "outside\n"


def test_a_failed_database_of_an_earlier_run_is_removed_before_the_process_starts(launched):
    stale = []

    def leave_stale(flow) -> None:
        stale.append(flow.run_path / flow.failed_odb)
        stale[0].write_text("from an earlier run\n")

    launched.probes.netlist.before_run = leave_stale
    flow = launched.launch()
    assert flow is not None and flow.succeeded
    assert stale and not stale[0].exists()


def test_the_log_goes_through_live_log_and_the_command_has_no_log_argument(launched, monkeypatch):
    from xeda import proc_utils, tool

    logs: list[Path] = []
    commands: list[list[str]] = []
    real_live_log, real_run_process = proc_utils.live_log, tool.run_process

    def live_log(path, *args, **kwargs):
        logs.append(Path(path))
        return real_live_log(path, *args, **kwargs)

    def run_process(executable, arguments, *args, **kwargs):
        commands.append([str(executable), *map(str, arguments)])
        return real_run_process(executable, arguments, *args, **kwargs)

    monkeypatch.setattr(proc_utils, "live_log", live_log)
    monkeypatch.setattr(tool, "run_process", run_process)
    flow = launched.launch()
    assert flow is not None and flow.succeeded
    run_dir = launched.run_directory()
    assert (run_dir / "openroad_probe.log").resolve() in [path.resolve() for path in logs]
    (command,) = [c for c in commands if Path(c[0]).name == "openroad" and "-no_init" in c]
    assert "-log" not in command
    assert command[1:] == [
        "-no_init",
        "-no_splash",
        "-threads",
        "max",
        "-exit",
        "-metrics",
        "metrics.json",
        "openroad_probe.tcl",
    ]
    assert (run_dir / "openroad_probe.tcl").is_file()


def test_the_threads_setting_gives_the_thread_count(launched, monkeypatch):
    from xeda import tool

    commands: list[list[str]] = []
    real_run_process = tool.run_process

    def run_process(executable, arguments, *args, **kwargs):
        commands.append([str(executable), *map(str, arguments)])
        return real_run_process(executable, arguments, *args, **kwargs)

    monkeypatch.setattr(tool, "run_process", run_process)
    store = tiny_store(launched.tmp_path)
    flow = launched.runner().run(
        launched.probes.netlist,
        launched.design,
        flow_settings=node_settings(
            launched.probes.netlist, [*openroad_settings(store), "nthreads=3"]
        ),
    )
    assert flow is not None and flow.succeeded
    (command,) = [c for c in commands if "-no_init" in c]
    assert command[command.index("-threads") + 1] == "3"


@pytest.mark.parametrize("top", ["a/b", "../escape", "\\top/a"])
def test_a_top_module_that_would_put_totcap_outside_is_refused_before_openroad_starts(
    launched, top
):
    design = openroad_design(launched.tmp_path / "other", top=top)
    with pytest.raises(FlowSettingsException) as error:
        launched.launch(design)
    message = str(error.value)
    assert top in message and "totCap" in message and "outside the run directory" in message
    assert not list(launched.root.rglob("fake_openroad.calls"))
    assert not list(launched.root.rglob("*.tcl"))


def test_a_file_that_would_leave_the_run_directory_is_refused_when_it_is_guarded(launched):
    from xeda.run_dir import RunDirectoryError

    flow = launched.probes.flow(launched.tmp_path / "flow")
    with pytest.raises(RunDirectoryError, match="not inside the run directory"):
        flow._guard_write("../x.totCap")
    with pytest.raises(RunDirectoryError):
        flow._guard_write(launched.tmp_path / "elsewhere" / "x.odb")
