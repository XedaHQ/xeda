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
    return [(node["flow"], node["outcome"], node["disposition"]) for node in document["nodes"]]


def test_a_failed_launch_reports_the_nodes_that_ran(toys, design):
    """A failure document lists the nodes that ran, the dependency included."""
    result, document = _run("toy_node_failing", str(design))
    assert result.exit_code == 1 and document["outcome"] == "failed"
    assert document["error"]["code"] == "reported_failure"
    assert document["error"]["node"] == "toy_node_failing"
    assert _states(document) == [
        ("toy_node_producer", "passed", "ran"),
        ("toy_node_failing", "failed", "ran"),
    ]
    _, document = _run("toy_node_failing", str(design))
    assert _states(document) == [
        ("toy_node_producer", "passed", "reused"),
        ("toy_node_failing", "failed", "ran"),
    ]


def test_a_producer_reached_twice_within_a_launch_is_one_node(toys, design):
    result, document = _run("toy_node_twice", str(design))
    assert result.exit_code == 0 and document["outcome"] == "passed"
    assert _states(document) == [
        ("toy_node_producer", "passed", "ran"),
        ("toy_node_twice", "passed", "ran"),
    ]
    paths = [node["run_dir"] for node in document["nodes"]]
    assert len(paths) == len(set(paths))


def test_a_rejected_flow_is_rejected_and_then_rejected_again(toys, design):
    """The first launch runs the flow and the checks reject it. The next one reports the same
    rejection without running the flow. Both exit with status 1."""
    args = ("toy_node_producer", str(design), "-s", "reject=true")
    result, document = _run(*args)
    assert result.exit_code == 1 and document["outcome"] == "rejected"
    assert document["error"]["code"] == "rejected"
    assert _states(document) == [("toy_node_producer", "rejected", "ran")]
    message = document["error"]["message"]
    stored = json.loads(Path(document["nodes"][0]["run_dir"], "results.json").read_text())
    assert stored["error"]["message"] == message
    result, document = _run(*args)
    assert result.exit_code == 1 and document["outcome"] == "rejected"
    assert document["error"]["code"] == "rejected" and document["error"]["message"] == message
    assert _states(document) == [("toy_node_producer", "rejected", "reused")]
    assert document["nodes"][0]["reason"] == message


def test_the_consumer_of_a_rejected_flow_is_blocked(toys, design):
    """The consumer does not run: it is `blocked` and has no record, and the document names the
    rejection of the producer as the cause, on the first launch and when the rejection is
    reported again."""
    args = (
        "toy_node_producer+toy_node_twice",
        str(design),
        "-s",
        "flows.toy_node_producer.reject=true",
    )
    result, document = _run(*args)
    assert result.exit_code == 1 and document["outcome"] == "rejected"
    assert document["error"]["code"] == "rejected"
    assert document["error"]["node"] == "toy_node_producer"
    assert _states(document) == [
        ("toy_node_producer", "rejected", "ran"),
        ("toy_node_twice", "blocked", "not_run"),
    ]
    blocked = document["nodes"][1]
    assert blocked["blocked_by"] == "toy_node_producer" and blocked["record"] is None
    assert not Path(blocked["run_dir"], "results.json").exists()
    result, document = _run(*args)
    assert document["outcome"] == "rejected"
    assert _states(document) == [
        ("toy_node_producer", "rejected", "reused"),
        ("toy_node_twice", "blocked", "not_run"),
    ]
    assert not Path(document["nodes"][1]["run_dir"], "results.json").exists()


def test_the_cli_prints_the_document_of_the_result_it_got(toys, design, monkeypatch):
    """The command prints `Result.to_dict()` of the result of the launch, and nothing else."""
    import xeda.cli as cli_module

    seen = []
    real = cli_module.launch_result

    def capture(*args, **kwargs):
        seen.append(real(*args, **kwargs))
        return seen[-1]

    monkeypatch.setattr(cli_module, "launch_result", capture)
    for flow, status in (("toy_node_failing", 1), ("toy_node_twice", 0)):
        result, document = _run(flow, str(design))
        assert result.exit_code == status
        assert document == json.loads(json.dumps(seen[-1].to_dict()))
    assert len(seen) == 2
