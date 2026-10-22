"""A dockerized consumer reads the files of its producers.

A producer's files lie in a sibling run directory under the run root. The container mounts the
run directory of the consumer, the design root and the directories of the design's sources, so
it needs one more read-only mount for each directory that holds a declared input or a file the
flow registered as an implicit input. These tests read the arguments of the `docker run`
command, so they need no Docker. `tests/test_dockerized.py` runs the same case in a container.
"""

from pathlib import Path
from typing import Callable, ClassVar, List, Optional, Tuple

import pytest

from xeda.flow import MetricSpec
from xeda import Design
from xeda.dataclass import WORKING, Field, deliverable
from xeda.design import SourceType
from xeda.flow import Flow, In, Out, registered_flows
from xeda.flow_runner import DefaultRunner
from xeda.tool import Docker, Tool

from .flow_registry import preserved_flow_registry

#: What the consumer does in `init()` and in `run()`, set by each test.
CONSUMER_INIT: List[Callable[[Flow], None]] = []
CONSUMER_RUN: List[Callable[[Flow], None]] = []


class _Producer(Flow):
    """Writes two files in its own run directory."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    class Outputs(Flow.Outputs):
        made: Path = Out(SourceType.Data, description="A file in `outputs`.")
        extra: Path = Out(SourceType.Data, description="A second file in `outputs`.")

    def run(self) -> None:
        (self.run_path / "outputs").mkdir(exist_ok=True)
        self.outputs.made = self.run_path / "outputs" / "made.txt"
        self.outputs.extra = self.run_path / "outputs" / "extra.txt"
        self.outputs.made.write_text("made\n")
        self.outputs.extra.write_text("extra\n")

    def parse_reports(self) -> bool:
        return True


class _Consumer(Flow):
    """Reads both files through a dockerized tool."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    class Settings(Flow.Settings):
        constraints: Optional[Path] = Field(None, description="A file it reads.")
        include: Optional[Path] = Field(None, description="A directory it reads.")
        libs: List[Tuple[str, Path]] = Field([], description="Library names and their files.")
        scratch: Optional[Path] = Field(
            None, description="A working location.", json_schema_extra=WORKING
        )
        report: Optional[Path] = Field(
            None, description="A file it writes.", json_schema_extra=deliverable("outputs/r.txt")
        )

    class Inputs(Flow.Inputs):
        made: Path = In(
            SourceType.Data,
            producer="__producer",
            output="made",
            description="The producer's first file.",
        )
        extra: Optional[Path] = In(
            SourceType.Data,
            producer="__producer",
            output="extra",
            description="The producer's second file.",
        )

    def init(self) -> None:
        self.tool = Tool(executable="some-tool", docker=Docker(image="img"))
        for action in CONSUMER_INIT:
            action(self)

    def run(self) -> None:
        for action in CONSUMER_RUN:
            action(self)
        self.tool.run("arg", stdout=True)
        if self.settings.report is not None:
            (self.run_path / "outputs").mkdir(exist_ok=True)
            (self.run_path / "outputs" / "r.txt").write_text("report\n")
            self.artifacts["report"] = "outputs/r.txt"

    def parse_reports(self) -> bool:
        return True


class _Alone(Flow):
    """Declares no input."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    def init(self) -> None:
        self.tool = Tool(executable="some-tool", docker=Docker(image="img"))

    def run(self) -> None:
        self.tool.run("arg", stdout=True)

    def parse_reports(self) -> bool:
        return True


# Test-only flows, launched by class and out of the registry once defined, so that no sweep over
# every registered flow finds them. The consumer's producer is found by name.
_FLOWS = (_Producer, _Consumer, _Alone)
_REGISTERED = {
    name: registered_flows[name]
    for cls in _FLOWS
    for name in (cls.name, cls.__name__)
    if name in registered_flows
}
for _cls in _FLOWS:
    for _name in (_cls.name, _cls.__name__):
        registered_flows.pop(_name, None)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A design, a run root, and the `docker run` commands that the tools start."""
    with preserved_flow_registry():
        registered_flows.update(_REGISTERED)
        (tmp_path / "design").mkdir()
        (tmp_path / "design" / "top.v").write_text("module top; endmodule\n")
        commands: List[List[str]] = []
        monkeypatch.setattr(
            "xeda.tool.run_process",
            lambda executable, args=None, **kwargs: commands.append([str(a) for a in args or []]),
        )
        CONSUMER_INIT.clear()
        CONSUMER_RUN.clear()
        design = Design(
            name="d",
            design_root=tmp_path / "design",
            rtl={"sources": ["top.v"], "top": "top"},
        )
        yield tmp_path, design, commands


def _launch(tmp_path, design, flow, **settings):
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    result = runner.launch_flow(flow, design, {"dockerized": True, **settings})
    assert result.succeeded
    return result


def _volumes(commands: List[List[str]]) -> List[str]:
    """The `--volume` arguments of the one command that ran `arg`."""
    (command,) = [c for c in commands if "arg" in c]
    return [a for a in command if a.startswith("--volume=")]


def _read_only(volumes: List[str]) -> List[str]:
    return [v.removeprefix("--volume=").removesuffix(":ro") for v in volumes if v.endswith(":ro")]


def test_a_consumer_mounts_the_directory_of_its_input_read_only(world):
    tmp_path, design, commands = world
    consumer = _launch(tmp_path, design, _Consumer)
    producer_outputs = consumer.inputs.made.parent
    assert producer_outputs != consumer.run_path
    volumes = _volumes(commands)
    assert f"--volume={producer_outputs}:{producer_outputs}:ro" in volumes
    assert f"--volume={consumer.run_path}:{consumer.run_path}" in volumes


def test_two_inputs_in_one_directory_make_one_mount(world):
    tmp_path, design, commands = world
    consumer = _launch(tmp_path, design, _Consumer)
    assert consumer.inputs.extra is not None
    assert consumer.inputs.made.parent == consumer.inputs.extra.parent
    read_only = _read_only(_volumes(commands))
    assert read_only.count(f"{consumer.inputs.made.parent}:{consumer.inputs.made.parent}") == 1


def test_a_flow_with_no_input_mounts_only_the_design_and_its_run_directory(world):
    tmp_path, design, commands = world
    alone = _launch(tmp_path, design, _Alone)
    root = design.design_root
    assert _volumes(commands) == [
        f"--volume={alone.run_path}:{alone.run_path}",
        f"--volume={root}:{root}:ro",
    ]


def test_nothing_but_the_run_directory_is_mounted_writable(world):
    tmp_path, design, commands = world
    consumer = _launch(tmp_path, design, _Consumer)
    writable = [v for v in _volumes(commands) if not v.endswith(":ro")]
    assert writable == [f"--volume={consumer.run_path}:{consumer.run_path}"]
    (command,) = [c for c in commands if "arg" in c]
    assert ["--security-opt", "label=disable"] == command[
        command.index("--security-opt") : command.index("--security-opt") + 2
    ]
    assert not any(a.endswith(":z") or a.endswith(":Z") for a in command)


def test_a_file_the_flow_registered_is_mounted_by_its_directory(world):
    tmp_path, design, commands = world
    registered = tmp_path / "cache" / "chipdb.bin"
    registered.parent.mkdir()
    registered.write_text("db\n")
    CONSUMER_RUN.append(lambda flow: flow.implicit_inputs.append(registered))
    _launch(tmp_path, design, _Consumer)
    assert f"{registered.parent}:{registered.parent}" in _read_only(_volumes(commands))


def test_a_directory_that_is_already_mounted_is_not_mounted_again(world):
    tmp_path, design, commands = world
    in_design = design.design_root / "extra.bin"
    in_design.write_text("x\n")
    consumer_dir: List[Path] = []

    def register(flow: Flow) -> None:
        inside = flow.run_path / "scratch" / "inside.bin"
        inside.parent.mkdir(exist_ok=True)
        inside.write_text("x\n")
        consumer_dir.append(inside.parent)
        flow.implicit_inputs.extend([in_design, inside])

    CONSUMER_RUN.append(register)
    _launch(tmp_path, design, _Consumer)
    mounted = [v.removeprefix("--volume=").split(":")[0] for v in _volumes(commands)]
    assert str(consumer_dir[0]) not in mounted, "a subdirectory of the run directory"
    assert mounted.count(str(design.design_root)) == 1


def test_a_missing_directory_is_not_mounted(world):
    tmp_path, design, commands = world
    gone = tmp_path / "gone" / "file.bin"
    CONSUMER_RUN.append(lambda flow: flow.implicit_inputs.append(gone))
    _launch(tmp_path, design, _Consumer)
    assert str(gone.parent) not in " ".join(_volumes(commands))


def test_a_derived_tool_mounts_the_inputs_too(world):
    tmp_path, design, commands = world
    CONSUMER_RUN.append(lambda flow: flow.tool.derive("other-tool").run("arg", stdout=True))
    consumer = _launch(tmp_path, design, _Consumer)
    # the tool of the flow and its sibling each started one command
    for command in [c for c in commands if "arg" in c]:
        assert f"--volume={consumer.inputs.made.parent}:{consumer.inputs.made.parent}:ro" in command


@pytest.mark.parametrize("target, mounted_again", [("{host}", False), ("/cache", True)])
def test_a_configured_mount_holds_an_input_only_at_the_same_path(world, target, mounted_again):
    tmp_path, design, commands = world
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "db.bin").write_text("db\n")
    CONSUMER_INIT.append(
        lambda flow: flow.tool.docker.mounts.update({str(cache): target.format(host=cache)})
    )
    CONSUMER_RUN.append(lambda flow: flow.implicit_inputs.append(cache / "db.bin"))
    _launch(tmp_path, design, _Consumer)
    assert (f"{cache}:{cache}" in _read_only(_volumes(commands))) is mounted_again


def test_nested_input_directories_make_one_mount(world):
    tmp_path, design, commands = world
    outer = tmp_path / "libs"
    (outer / "inner").mkdir(parents=True)
    (outer / "a.bin").write_text("a\n")
    (outer / "inner" / "b.bin").write_text("b\n")
    CONSUMER_RUN.append(
        lambda flow: flow.implicit_inputs.extend([outer / "inner" / "b.bin", outer / "a.bin"])
    )
    _launch(tmp_path, design, _Consumer)
    read_only = _read_only(_volumes(commands))
    assert f"{outer}:{outer}" in read_only
    assert f"{outer / 'inner'}:{outer / 'inner'}" not in read_only


def test_a_linked_input_mounts_the_directory_of_its_target_too(world):
    tmp_path, design, commands = world
    target = tmp_path / "elsewhere" / "real.bin"
    target.parent.mkdir()
    target.write_text("x\n")
    link = tmp_path / "links" / "link.bin"
    link.parent.mkdir()
    link.symlink_to(target)
    CONSUMER_RUN.append(lambda flow: flow.implicit_inputs.append(link))
    _launch(tmp_path, design, _Consumer)
    read_only = _read_only(_volumes(commands))
    assert f"{link.parent}:{link.parent}" in read_only
    assert f"{target.parent.resolve()}:{target.parent.resolve()}" in read_only


def _outside(tmp_path: Path) -> Path:
    """A directory with a file, a subdirectory with a file, and nothing else of the design's."""
    outside = tmp_path / "outside"
    (outside / "include").mkdir(parents=True)
    (outside / "include" / "defs.vh").write_text("defs\n")
    (outside / "pins.xdc").write_text("pins\n")
    (outside / "lib").mkdir()
    (outside / "lib" / "cells.lib").write_text("cells\n")
    return outside


def test_the_directory_of_a_file_a_setting_names_is_mounted_read_only(world):
    tmp_path, design, commands = world
    outside = _outside(tmp_path)
    _launch(tmp_path, design, _Consumer, constraints=str(outside / "pins.xdc"))
    assert f"{outside}:{outside}" in _read_only(_volumes(commands))


def test_a_directory_a_setting_names_is_mounted_read_only(world):
    tmp_path, design, commands = world
    outside = _outside(tmp_path)
    _launch(tmp_path, design, _Consumer, include=str(outside / "include"))
    read_only = _read_only(_volumes(commands))
    assert f"{outside / 'include'}:{outside / 'include'}" in read_only
    assert f"{outside}:{outside}" not in read_only


def test_the_path_half_of_a_library_entry_is_mounted_and_its_name_is_not(world):
    tmp_path, design, commands = world
    outside = _outside(tmp_path)
    _launch(tmp_path, design, _Consumer, libs=[["cells", str(outside / "lib" / "cells.lib")]])
    read_only = _read_only(_volumes(commands))
    assert f"{outside / 'lib'}:{outside / 'lib'}" in read_only
    assert not any(v.startswith("cells") for v in read_only)


def test_a_relative_setting_is_mounted_where_it_is_found(world):
    tmp_path, design, commands = world
    (design.design_root / "rtl").mkdir()
    (design.design_root / "rtl" / "extra.v").write_text("x\n")
    _launch(tmp_path, design, _Consumer, constraints="rtl/extra.v")
    mounted = [v.removeprefix("--volume=").split(":")[0] for v in _volumes(commands)]
    # the design root holds it already
    assert mounted.count(str(design.design_root)) == 1
    assert str(design.design_root / "rtl") not in mounted


def test_a_working_or_deliverable_setting_is_not_mounted(world):
    tmp_path, design, commands = world
    destination = tmp_path / "destination"
    destination.mkdir()
    _launch(tmp_path, design, _Consumer, scratch="scratch", report=str(destination / "r.txt"))
    volumes = " ".join(_volumes(commands))
    assert str(destination) not in volumes
    assert "scratch" not in volumes


def test_every_file_and_directory_the_trace_records_from_a_setting_is_held(world):
    """The mounts and the trace's setting inputs come from one reading of the settings."""
    from xeda.flow_runner.trace_inputs import setting_directories, setting_files

    tmp_path, design, commands = world
    outside = _outside(tmp_path)
    consumer = _launch(
        tmp_path,
        design,
        _Consumer,
        constraints=str(outside / "pins.xdc"),
        include=str(outside / "include"),
        libs=[["cells", str(outside / "lib" / "cells.lib")]],
    )
    held = consumer.input_directories()
    files = setting_files(consumer.settings)
    directories = [d for _key, d in setting_directories(consumer.settings, consumer.run_path)]
    assert len(files) == 2 and len(directories) == 1
    assert all(f.parent.resolve() in {h.resolve() for h in held} for f in files)
    assert all(d.resolve() in {h.resolve() for h in held} for d in directories)
