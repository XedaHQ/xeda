"""Outputs are delivered where the user named them (D21): copied after the run, never onto an
input, never deleting, never through a link, and never over a file of the user's without their
say; where an output goes is never part of what the run is."""

import json
import logging
import os
import shutil
from pathlib import Path, PurePath
from types import SimpleNamespace
from typing import Callable, ClassVar, Dict, List, Optional, Tuple

import pytest
from click.testing import CliRunner

from xeda import Design
from xeda.cli import cli
from xeda.dataclass import Field, deliverable
from xeda.deliver import (
    Conflict,
    Delivery,
    DeliveryError,
    OutputExistsError,
    delivery_record,
)
from xeda.flow import Flow, FlowSettingsError, registered_flows
from xeda.flow_runner import DefaultRunner
from xeda.run_root import ensure_run_root

SQRT = Path(__file__).parent.parent / "examples" / "vhdl" / "sqrt"
FAKE_TOOLS = Path(__file__).parent / "fake_tools"

RUNS: List[str] = []
#: what a test does while the deliverer runs (a stand-in for a user editing a file meanwhile)
DURING_RUN: List[Callable[[], object]] = []
#: what a test does while the wrapper runs, its dependency completed
DURING_WRAPPER: List[Callable[["_Wrapper"], object]] = []


class _Deliverer(Flow):
    """Writes the netlist its setting names, and reports it."""

    results_description: ClassVar[dict[str, str]] = {}

    class Settings(Flow.Settings):
        netlist: Optional[Path] = Field(
            None,
            description="The netlist it writes.",
            json_schema_extra=deliverable("outputs/{design}.v"),
        )
        text: str = Field("net\n", description="What it writes.")
        fail: bool = Field(False, description="Whether its reports say it failed.")
        reads: Optional[Path] = Field(None, description="A file it reads: an input.")

    def run(self) -> None:
        RUNS.append(self.name)
        assert self.settings.netlist is not None
        netlist = Path(self.settings.netlist)
        netlist.parent.mkdir(parents=True, exist_ok=True)
        netlist.write_text(self.settings.text)
        self.artifacts["netlist"] = str(netlist)
        for action in DURING_RUN:
            action()

    def parse_reports(self) -> bool:
        return not self.settings.fail


class _Wrapper(Flow):
    """Launches the deliverer with the settings it holds for it."""

    results_description: ClassVar[dict[str, str]] = {}

    class Settings(Flow.Settings):
        dependency_settings: ClassVar[Dict[str, Tuple[str, ...]]] = {"inner": ()}
        inner: _Deliverer.Settings = Field(
            default_factory=_Deliverer.Settings, description="The deliverer's settings."
        )
        reads: Optional[Path] = Field(None, description="A file it reads: an input.")
        fail: bool = Field(False, description="Whether its reports say it failed.")

    def init(self) -> None:
        self.add_dependency(_Deliverer, self.settings.resolve_dependency("inner"))

    def run(self) -> None:
        for action in DURING_WRAPPER:
            action(self)
        (self.run_path / "summary.txt").write_text("ok\n")
        self.artifacts["summary"] = "summary.txt"

    def parse_reports(self) -> bool:
        return not self.settings.fail


class _Twice(Flow):
    """Launches the deliverer twice, with the settings it holds for each: one configuration,
    whatever the locations, so one run directory entered twice in a launch."""

    results_description: ClassVar[dict[str, str]] = {}

    class Settings(Flow.Settings):
        dependency_settings: ClassVar[Dict[str, Tuple[str, ...]]] = {"first": (), "second": ()}
        first: _Deliverer.Settings = Field(
            default_factory=_Deliverer.Settings, description="The first deliverer's settings."
        )
        second: _Deliverer.Settings = Field(
            default_factory=_Deliverer.Settings, description="The second deliverer's settings."
        )

    def init(self) -> None:
        self.add_dependency(_Deliverer, self.settings.resolve_dependency("first"))
        self.add_dependency(_Deliverer, self.settings.resolve_dependency("second"))

    def run(self) -> None:
        pass


# Test-only flows, launched by class: out of the registry at once, so that no sweep over every
# registered flow (`test_documentation`, `test_flow_registry`) collected after this module finds
# them.
for _cls in (_Deliverer, _Wrapper, _Twice):
    for _name in (_cls.name, _cls.__name__):
        registered_flows.pop(_name, None)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A design, the user's directory xeda is started in, and where runs go."""
    (tmp_path / "design").mkdir()
    (tmp_path / "design" / "top.v").write_text("module top; endmodule\n")
    user = tmp_path / "user"
    user.mkdir()
    monkeypatch.chdir(user)
    RUNS.clear()
    DURING_RUN.clear()
    DURING_WRAPPER.clear()
    design = Design(
        name="d", design_root=tmp_path / "design", rtl={"sources": ["top.v"], "top": "top"}
    )
    return SimpleNamespace(user=user, design=design, root=tmp_path / "xeda_run")


def _launch(world, launcher=None, flow=_Deliverer, **settings):
    runner = launcher or DefaultRunner(world.root, display_results=False)
    return runner.launch_flow(flow, world.design, settings)


def test_a_location_is_delivered_and_the_run_writes_the_conventional_name(world):
    flow = _launch(world, netlist="$PWD/out/net.v")
    destination = world.user / "out" / "net.v"
    assert flow.succeeded and (flow.run_path / "outputs" / "d.v").read_text() == "net\n"
    assert destination.read_text() == "net\n"
    recorded = json.loads((flow.run_path / "settings.json").read_text())
    assert recorded["flow_settings"]["netlist"] == "outputs/d.v"
    assert recorded["deliveries"] == [
        {"setting": "netlist", "name": "outputs/d.v", "to": str(destination)}
    ]
    assert [(d.destination, d.state) for d in flow.deliveries] == [(destination, "delivered")]


def test_renaming_or_moving_a_destination_never_re_runs(world):
    """Delivery targets are not identity, and never what the tool is told to write."""
    first = _launch(world, netlist="$PWD/a/net.v")
    second = _launch(world, netlist="$PWD/b/renamed.txt")
    assert second.reused and second.flow_hash == first.flow_hash and len(RUNS) == 1
    assert (world.user / "b" / "renamed.txt").read_text() == "net\n"
    assert _launch(world, netlist="outputs/d.v").reused, "the conventional name, delivered nowhere"


def test_a_dependency_delivers_its_output_and_where_is_no_one_s_identity(world):
    _launch(world, flow=_Wrapper, inner={"netlist": "$PWD/a/net.v"})
    second = _launch(world, flow=_Wrapper, inner={"netlist": "$PWD/b/other.v"})
    assert second.reused and second.completed_dependencies[0].reused
    assert (world.user / "a" / "net.v").is_file() and (world.user / "b" / "other.v").is_file()


def test_another_extension_is_copied_as_it_is_and_says_so(world, caplog):
    with caplog.at_level(logging.WARNING, logger="xeda.deliver"):
        _launch(world, netlist="$PWD/net.v.gz")
    assert (world.user / "net.v.gz").read_text() == "net\n"
    assert "outputs/d.v" in caplog.text


def test_an_up_to_date_run_delivers_again(world):
    _launch(world, netlist="$PWD/net.v")
    (world.user / "net.v").unlink()
    flow = _launch(world, netlist="$PWD/net.v")
    assert flow.reused and (world.user / "net.v").read_text() == "net\n"
    assert [d.state for d in _launch(world, netlist="$PWD/net.v").deliveries] == ["unchanged"]


def test_xeda_s_own_unchanged_copy_is_replaced(world):
    _launch(world, netlist="$PWD/net.v", text="a\n")
    flow = _launch(world, netlist="$PWD/net.v", text="b\n")
    assert not flow.reused and (world.user / "net.v").read_text() == "b\n"


def test_the_delivery_record_lies_beside_the_run_directory_and_survives_clean(world):
    flow = _launch(world, netlist="$PWD/net.v", text="a\n")
    record = delivery_record(flow.run_path)
    assert record.parent == flow.run_path.parent and record.is_file()
    cleaning = DefaultRunner(world.root, display_results=False, clean=True)
    _launch(world, cleaning, netlist="$PWD/net.v", text="b\n")
    assert (world.user / "net.v").read_text() == "b\n", "still xeda's own copy after --clean"


@pytest.mark.parametrize("theirs", ["edited", "foreign", "a link"])
def test_a_file_that_is_not_xeda_s_unchanged_copy_is_refused_before_the_tool_runs(
    world, tmp_path, theirs
):
    destination = world.user / "net.v"
    if theirs == "edited":
        _launch(world, netlist="$PWD/net.v", text="a\n")
        destination.write_text("my edit\n")
    elif theirs == "foreign":
        destination.write_text("mine\n")
    else:
        (tmp_path / "target.txt").write_text("target\n")
        destination.symlink_to(tmp_path / "target.txt")
    before = destination.read_text()
    RUNS.clear()
    with pytest.raises(OutputExistsError, match="--overwrite-outputs"):
        _launch(world, netlist="$PWD/net.v", text="b\n")
    assert RUNS == [] and destination.read_text() == before


@pytest.mark.parametrize("how", ["flag", "yes"])
def test_a_confirmed_replacement_replaces_a_link_as_itself(world, tmp_path, how):
    (tmp_path / "target.txt").write_text("target\n")
    destination = world.user / "net.v"
    destination.symlink_to(tmp_path / "target.txt")
    launcher = DefaultRunner(world.root, display_results=False, overwrite_outputs=how == "flag")
    asked: List[Conflict] = []
    if how == "yes":
        launcher.confirm_overwrite = lambda conflicts: asked.extend(conflicts) or True
    _launch(world, launcher, netlist="$PWD/net.v")
    assert not destination.is_symlink() and destination.read_text() == "net\n"
    assert (tmp_path / "target.txt").read_text() == "target\n"
    assert how == "flag" or [c.why for c in asked] == ["a symbolic link xeda did not make"]


def test_a_no_at_the_prompt_keeps_the_file(world):
    (world.user / "net.v").write_text("mine\n")
    launcher = DefaultRunner(world.root, display_results=False)
    launcher.confirm_overwrite = lambda conflicts: False
    with pytest.raises(OutputExistsError):
        _launch(world, launcher, netlist="$PWD/net.v")
    assert (world.user / "net.v").read_text() == "mine\n"


@pytest.mark.parametrize("spelling", ["the source", "a hard link", "a symbolic link", "a case"])
def test_an_input_is_never_a_destination_even_with_overwrite_outputs(world, spelling):
    source = world.design.root_path / "top.v"
    destination = world.user / "net.v"
    if spelling == "the source":
        destination = source
    elif spelling == "a hard link":
        os.link(source, destination)
    elif spelling == "a symbolic link":
        destination.symlink_to(source)
    else:
        destination = source.with_name("TOP.v")
        if not destination.exists():
            pytest.skip("a case-sensitive file system: TOP.v would be another file")
    launcher = DefaultRunner(world.root, display_results=False, overwrite_outputs=True)
    with pytest.raises(DeliveryError, match="an input of the run"):
        _launch(world, launcher, netlist=str(destination))
    assert source.read_text() == "module top; endmodule\n" and RUNS == []


def test_a_directory_is_not_a_file_s_destination(world):
    (world.user / "net.v").mkdir()
    with pytest.raises(DeliveryError, match="a directory"):
        _launch(world, netlist="$PWD/net.v")
    assert RUNS == []


def test_a_destination_in_the_run_root_is_refused(world):
    with pytest.raises(DeliveryError, match="run root"):
        _launch(world, netlist=str(world.root / "elsewhere" / "net.v"))


def test_a_destination_in_any_run_root_is_refused(world, tmp_path):
    """Another project's `xeda_run`, a DSE root: every marked run root is xeda's."""
    other = ensure_run_root(tmp_path / "another" / "xeda_run")
    assert other is not None
    with pytest.raises(DeliveryError, match="run root"):
        _launch(world, netlist=str(other / "d" / "net.v"))
    assert RUNS == []


def test_a_name_xeda_keeps_is_refused(world):
    with pytest.raises(FlowSettingsError, match="a name xeda keeps"):
        _launch(world, netlist="results.json")  # a bare name: a location never is one


def test_a_failed_run_delivers_nothing(world):
    _launch(world, netlist="$PWD/net.v", text="a\n")
    flow = _launch(world, netlist="$PWD/net.v", text="b\n", fail=True)
    assert not flow.succeeded and (world.user / "net.v").read_text() == "a\n"


def test_a_failed_requested_flow_delivers_nothing_not_even_its_dependency_s(world):
    """Delivery is gated on the requested flow's success: its dependency succeeded and noted
    what it delivers, but the flow the launch was asked for reports failure (it returns, it does
    not raise) -- nothing is delivered, and the user's files are as they were."""
    (world.user / "net.v").write_text("mine\n")
    launcher = DefaultRunner(
        world.root, display_results=False, overwrite_outputs=True, outputs_to=world.user / "got"
    )
    flow = _launch(world, launcher, flow=_Wrapper, fail=True, inner={"netlist": "$PWD/net.v"})
    assert not flow.succeeded and flow.completed_dependencies[0].succeeded
    assert RUNS == [_Deliverer.name], "the dependency ran, and wrote its output in its run dir"
    assert (world.user / "net.v").read_text() == "mine\n"
    assert not (world.user / "got").exists()
    assert flow.deliveries == [] and flow.completed_dependencies[0].deliveries == []


def test_a_destination_changed_while_the_run_went_on_is_not_replaced(world):
    (world.user / "net.v").write_text("mine\n")
    DURING_RUN.append(lambda: (world.user / "net.v").write_text("edited while it ran\n"))
    launcher = DefaultRunner(world.root, display_results=False, overwrite_outputs=True)
    with pytest.raises(DeliveryError, match="changed while the run went on"):
        _launch(world, launcher, netlist="$PWD/net.v")
    assert (world.user / "net.v").read_text() == "edited while it ran\n"


@pytest.mark.parametrize("swap", ["destination", "parent"])
def test_a_destination_swapped_during_the_copy_is_not_replaced(world, monkeypatch, swap):
    """Checked again right before the rename: a file put there, or its directory swapped for a
    link to the design's, while the copy was made, is left as it is -- the input untouched."""
    import xeda.deliver

    out = world.user / "out"
    out.mkdir()
    copy = shutil.copyfileobj

    def copy_then_swap(source, target, *args):
        copy(source, target, *args)
        if swap == "destination":
            (out / "net.v").write_text("put there meanwhile\n")
        else:
            out.rename(world.user / "moved")
            out.symlink_to(world.design.root_path, target_is_directory=True)

    monkeypatch.setattr(xeda.deliver.shutil, "copyfileobj", copy_then_swap)
    with pytest.raises(DeliveryError, match="changed while the run went on"):
        _launch(world, netlist="$PWD/out/net.v")
    if swap == "destination":
        assert (out / "net.v").read_text() == "put there meanwhile\n"
    else:
        assert not (world.design.root_path / "net.v").exists()
        assert (world.design.root_path / "top.v").read_text() == "module top; endmodule\n"


def test_the_same_bytes_in_another_file_are_not_xeda_s_copy(world):
    """A record is a `FileRecord`: a user's file of the same content put in place of xeda's copy
    (another inode) is not xeda's, and is not replaced without a yes."""
    _launch(world, netlist="$PWD/net.v", text="a\n")
    theirs = world.user / "theirs.v"
    theirs.write_text("a\n")
    os.replace(theirs, world.user / "net.v")  # made while xeda's copy existed: another inode
    RUNS.clear()
    with pytest.raises(OutputExistsError, match="changed since xeda wrote it"):
        _launch(world, netlist="$PWD/net.v", text="b\n")
    assert RUNS == [] and (world.user / "net.v").read_text() == "a\n"


def test_outputs_to_copies_the_requested_flow_s_artifacts_only(world):
    launcher = DefaultRunner(world.root, display_results=False, outputs_to=world.user / "got")
    flow = _launch(world, launcher, flow=_Wrapper, inner={"netlist": "build/net.v"})
    assert (world.user / "got" / "summary.txt").read_text() == "ok\n"
    assert not (world.user / "got" / "build").exists(), "a dependency's artifacts stay put"
    assert [d.key for d in flow.deliveries] == ["--outputs-to"]


def test_a_dependency_s_read_input_is_never_a_destination(world):
    """gpt-6-sol's final (c): the guard covers every read input of the launched graph -- here a
    file only the dependency's settings, nested in the wrapper's, name -- flag or not."""
    kept = world.user / "summary.txt"
    kept.write_text("the dependency reads this\n")
    launcher = DefaultRunner(
        world.root, display_results=False, outputs_to=world.user, overwrite_outputs=True
    )
    with pytest.raises(DeliveryError, match="an input of the run"):
        _launch(
            world, launcher, flow=_Wrapper, inner={"netlist": "build/net.v", "reads": str(kept)}
        )
    assert kept.read_text() == "the dependency reads this\n"


def test_a_dependency_never_delivers_onto_what_its_depender_reads(world):
    """Refused before the dependency's tool runs: the depender's inputs are registered first."""
    (world.user / "in.v").write_text("the wrapper reads this\n")
    launcher = DefaultRunner(world.root, display_results=False, overwrite_outputs=True)
    with pytest.raises(DeliveryError, match="an input of the run"):
        _launch(world, launcher, flow=_Wrapper, reads="$PWD/in.v", inner={"netlist": "$PWD/in.v"})
    assert (world.user / "in.v").read_text() == "the wrapper reads this\n" and RUNS == []


def test_a_dependency_s_output_is_delivered_once_the_launch_has_finished(world):
    seen: List[bool] = []
    DURING_WRAPPER.append(lambda wrapper: seen.append((world.user / "net.v").exists()))
    _launch(world, flow=_Wrapper, inner={"netlist": "$PWD/net.v"})
    assert seen == [False] and (world.user / "net.v").read_text() == "net\n"


@pytest.mark.parametrize("second", ["$PWD/b.v", "$PWD/a.v"])
def test_a_run_directory_entered_twice_delivers_once_to_every_destination(world, second):
    """One configuration asked for twice in a launch (locations are no part of it) runs once, and
    delivers to each destination either asked for -- one record of both, so neither is a
    stranger's file at the next launch."""
    _launch(world, flow=_Twice, first={"netlist": "$PWD/a.v"}, second={"netlist": second})
    assert RUNS == [_Deliverer.name]
    for name in {"a.v", Path(second).name}:
        assert (world.user / name).read_text() == "net\n"
    again = _launch(
        world,
        flow=_Twice,
        first={"netlist": "$PWD/a.v", "text": "b\n"},
        second={"netlist": second, "text": "b\n"},
    )
    assert again.succeeded and (world.user / "a.v").read_text() == "b\n"


def test_an_output_changed_after_its_run_is_not_delivered(world):
    """A deferred delivery copies what its node's run left (the digest `collect` noted), not a
    file written into that run directory since -- as another launch there would."""

    def rewrite(wrapper):
        (wrapper.completed_dependencies[0].run_path / "outputs" / "d.v").write_text("other\n")

    DURING_WRAPPER.append(rewrite)
    with pytest.raises(DeliveryError, match="changed after its run"):
        _launch(world, flow=_Wrapper, inner={"netlist": "$PWD/net.v"})
    assert not (world.user / "net.v").exists()


def test_an_exploration_delivers_nothing(world, tmp_path):
    from xeda.flow_runner.dse.dse_runner import Dse, Optimizer

    class _NoBatch(Optimizer):
        def next_batch(self):
            return None

    dse = Dse(_NoBatch, {}, tmp_path / "dse", variations={})
    with pytest.raises(FlowSettingsError, match="nothing is delivered"):
        dse.run_flow(_Deliverer, world.design, {"netlist": str(world.user / "net.v")})


def test_a_remote_run_refuses_a_located_output_before_connecting(world, monkeypatch):
    from xeda.flow_runner import remote

    monkeypatch.setattr(remote, "Connection", lambda *a, **k: pytest.fail("it connected"))
    with pytest.raises(DeliveryError, match="--outputs-to"):
        remote.RemoteRunner(world.root).run_remote(
            world.design,
            "vivado_synth",
            "host",
            flow_settings=["fpga.part=xc7a12tcsg325-1", "bitstream=$PWD/top.bit"],
        )


def test_a_remote_run_checks_its_output_names_before_connecting(world, monkeypatch):
    """`results.json` would overwrite the remote's own record (Opus minor)."""
    from xeda.flow_runner import remote

    monkeypatch.setattr(remote, "Connection", lambda *a, **k: pytest.fail("it connected"))
    with pytest.raises(FlowSettingsError, match="a name xeda keeps"):
        remote.RemoteRunner(world.root).run_remote(
            world.design,
            "yosys_fpga",
            "host",
            flow_settings=["fpga.part=LFE5U-25F-6BG381C", "netlist_json=results.json"],
        )


def test_a_remote_run_protects_every_read_input_it_can_name(world):
    """gpt-6-sol's final (c), remote: the files the nested dependency's settings and every flow
    section sent with the design name are inputs, as the design's own are."""
    from xeda.flow_runner.remote import remote_read_inputs
    from xeda.flows import VivadoPostsynthSim

    for name in ("nested.xdc", "section.xdc"):
        (world.user / name).write_text("")
    settings = VivadoPostsynthSim.Settings.from_input(
        {"synth": {"fpga": {"part": "xc7a12tcsg325-1"}, "xdc_files": ["$PWD/nested.xdc"]}},
        design_root=world.design.root_path,
        runner_cwd=world.user,
    )
    sections = {"vivado_synth": {"xdc_files": [str(world.user / "section.xdc")]}}
    inputs = remote_read_inputs(world.design, settings, sections, [])
    named = [world.user / "nested.xdc", world.user / "section.xdc"]
    for path in [*named, world.design.root_path / "top.v"]:
        assert inputs.find(path) is not None, path


def test_a_failed_remote_run_delivers_nothing(world):
    """Opus I2: `--outputs-to` delivers a remote run's fetched artifacts only when it succeeded:
    a failed run's files never replace xeda's earlier copies."""
    from xeda.deliver import Deliveries, ReadInputs
    from xeda.flow_runner.remote import RemoteRunner

    root = ensure_run_root(world.root)
    assert root is not None
    mirror = root / "d" / "flow_0123456789abcdef"
    fetched = mirror / "artifacts"
    fetched.mkdir(parents=True)
    (fetched / "a.txt").write_text("fetched\n")
    runner = RemoteRunner(root, outputs_to=world.user / "got")
    for success in (False, True):
        results = {"success": success}
        delivery = Deliveries(mirror, root, inputs=ReadInputs())
        artifacts = {"a": "/remote/run/a.txt"}  # as the remote reported them
        runner._deliver_fetched(delivery, results, artifacts, "/remote/run", fetched)
        assert (world.user / "got" / "a.txt").exists() is success
        assert ("deliveries" in results) is success


@pytest.fixture
def sqrt_copy(tmp_path, monkeypatch):
    work = tmp_path / "work"
    shutil.copytree(SQRT, work, ignore=shutil.ignore_patterns("xeda_run*"))
    monkeypatch.setenv("PATH", str(FAKE_TOOLS) + os.pathsep + os.environ["PATH"])
    monkeypatch.chdir(work)
    return work


def test_outputs_to_and_overwrite_outputs_on_the_command_line(sqrt_copy):
    args = [
        "run",
        "vivado_synth",
        "sqrt.toml",
        "-s",
        "fpga.part=xc7a12tcsg325-1",
        "--outputs-to",
        "got",
        "--json",
    ]
    first = json.loads(CliRunner().invoke(cli, args).stdout)
    delivered = first["nodes"][0]["deliveries"]
    assert first["success"] and delivered
    assert all(Path(d["to"]).is_relative_to(sqrt_copy / "got") for d in delivered)
    edited = Path(delivered[0]["to"])
    edited.write_text("my edit\n")
    refused = CliRunner().invoke(cli, args)
    document = json.loads(refused.stdout)
    assert refused.exit_code != 0 and document["error"]["type"] == "OutputExistsError"
    assert edited.read_text() == "my edit\n", "no terminal, no --overwrite-outputs: kept"
    replaced = json.loads(CliRunner().invoke(cli, [*args, "--overwrite-outputs"]).stdout)
    assert replaced["success"] and edited.read_text() != "my edit\n"


def test_the_prompt_lists_what_it_would_replace_and_defaults_to_no(monkeypatch, capsys):
    from xeda.cli import _prompt_overwrite

    destination = Path("/u/sqrt.bit")
    conflict = Conflict(
        Delivery("bitstream", PurePath("sqrt.bit"), destination),
        destination,
        "not recorded as delivered from sqrt: yours, or another run's",
    )
    monkeypatch.setattr("click.confirm", lambda text, default, err: default)
    assert _prompt_overwrite([conflict]) is False
    assert "/u/sqrt.bit" in capsys.readouterr().err
