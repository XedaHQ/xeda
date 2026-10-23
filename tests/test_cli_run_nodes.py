"""`xeda run --json` reports every node the launch touched, failed launches included, each run
directory once."""

import json
from pathlib import Path
from typing import ClassVar

from pydantic import Field

import pytest
from click.testing import CliRunner

from xeda.flow import MetricSpec
from xeda.cli import cli
from xeda.design import SourceType
from xeda.flow import Flow, FlowFatalError, In, Out, registered_flows

pytestmark = pytest.mark.python_compat


@pytest.fixture(scope="module")
def toys():
    class ToyNodeProducer(Flow):
        """Writes an output."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}

        class Settings(Flow.Settings):
            reject: bool = Field(False, description="Reject the results in the checks.")

        class Outputs(Flow.Outputs):
            out: Path = Out(SourceType.Data, description="The file it writes.")

        def run(self) -> None:
            (self.run_path / "out.txt").write_text("out\n")
            self.outputs.out = self.run_path / "out.txt"

        def check_results(self) -> bool:
            if self.settings.reject:
                self.reject("the file is rejected")
            return super().check_results()

    class ToyNodeFailing(Flow):
        """Reads the producer's file, then fails."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}

        class Inputs(Flow.Inputs):
            out: Path = In(
                SourceType.Data,
                producer="toy_node_producer",
                output="out",
                description="The producer's file.",
            )

        def run(self) -> None:
            raise FlowFatalError("boom")

    class ToyNodeTwice(Flow):
        """Reads the producer's file through two inputs."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}

        class Inputs(Flow.Inputs):
            first: Path = In(
                SourceType.Data,
                producer="toy_node_producer",
                output="out",
                description="The producer's file.",
            )
            second: Path = In(
                SourceType.Data,
                producer="toy_node_producer",
                output="out",
                description="The producer's file again.",
            )

        def run(self) -> None:
            pass

    classes = (ToyNodeProducer, ToyNodeFailing, ToyNodeTwice)
    yield classes
    for cls in classes:
        for name in (cls.name, cls.__name__):
            registered_flows.pop(name, None)


@pytest.fixture
def design(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.v").write_text("module a; endmodule\n")
    path = tmp_path / "toy.toml"
    path.write_text('name = "toy"\n[rtl]\nsources = ["a.v"]\ntop = "a"\n')
    return path


def _run(*args):
    result = CliRunner().invoke(cli, ["run", *args, "--json"], catch_exceptions=False)
    return result, json.loads(result.stdout)


def _states(document):
    return [(node["flow"], node["state"]) for node in document["nodes"]]


def test_a_failed_launch_reports_the_nodes_that_ran(toys, design):
    """A failure document lists the nodes that ran, the dependency included."""
    result, document = _run("toy_node_failing", str(design))
    assert result.exit_code != 0 and document["success"] is False
    assert document["error"]["type"] == "FlowFatalError"
    assert _states(document) == [("toy_node_producer", "ran"), ("toy_node_failing", "failed")]
    _, document = _run("toy_node_failing", str(design))
    assert _states(document) == [("toy_node_producer", "fresh"), ("toy_node_failing", "failed")]


def test_a_producer_reached_twice_within_a_launch_is_one_node(toys, design):
    _, document = _run("toy_node_twice", str(design))
    assert document["success"] is True
    assert _states(document) == [("toy_node_producer", "ran"), ("toy_node_twice", "ran")]
    paths = [node["run_path"] for node in document["nodes"]]
    assert len(paths) == len(set(paths))


def test_a_rejected_flow_is_failed_and_then_rejected_again(toys, design):
    """The first launch runs the flow and the checks reject it. The next one reports the same
    rejection without running the flow. Both exit with `FlowFailed`."""
    args = ("toy_node_producer", str(design), "-s", "reject=true")
    result, document = _run(*args)
    assert result.exit_code != 0 and document["success"] is False
    assert document["error"]["type"] == "FlowFailed"
    assert _states(document) == [("toy_node_producer", "failed")]
    message = json.loads(Path(document["nodes"][0]["run_path"], "results.json").read_text())[
        "error"
    ]["message"]
    result, document = _run(*args)
    assert result.exit_code != 0 and document["success"] is False
    assert document["error"]["type"] == "FlowFailed"
    assert _states(document) == [("toy_node_producer", "rejected again")]
    assert document["nodes"][0]["reason"] == message


def test_the_consumer_of_a_rejected_flow_is_blocked(toys, design):
    """The consumer does not run: it is `blocked`, and the document names a dependency failure,
    on the first launch and when the rejection is reported again."""
    args = (
        "toy_node_producer+toy_node_twice",
        str(design),
        "-s",
        "flows.toy_node_producer.reject=true",
    )
    result, document = _run(*args)
    assert result.exit_code != 0 and document["success"] is False
    assert document["error"]["type"] == "FlowDependencyFailure"
    assert _states(document) == [("toy_node_producer", "failed"), ("toy_node_twice", "blocked")]
    assert document["nodes"][1]["reason"] == "toy_node_producer did not succeed"
    result, document = _run(*args)
    assert document["error"]["type"] == "FlowDependencyFailure"
    assert _states(document) == [
        ("toy_node_producer", "rejected again"),
        ("toy_node_twice", "blocked"),
    ]
    assert document["nodes"][1]["reason"] == "toy_node_producer did not succeed"
