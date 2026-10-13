"""A dockerized consumer reads the files of its producers.

A producer's files lie in a sibling run directory under the run root. The container mounts the
run directory of the consumer, the design root and the directories of the design's sources, so
it needs one more read-only mount for each directory that holds a declared input or a file the
flow registered as an implicit input. These tests read the arguments of the `docker run`
command, so they need no Docker. `tests/test_dockerized.py` runs the same case in a container.
"""

from pathlib import Path
from typing import Callable, ClassVar, List, Optional

import pytest

from xeda import Design
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

    results_description: ClassVar[dict[str, str]] = {}

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

    results_description: ClassVar[dict[str, str]] = {}

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

    def parse_reports(self) -> bool:
        return True


class _Alone(Flow):
    """Declares no input."""

    results_description: ClassVar[dict[str, str]] = {}

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


def _launch(tmp_path, design, flow):
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    result = runner.launch_flow(flow, design, {"dockerized": True})
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
