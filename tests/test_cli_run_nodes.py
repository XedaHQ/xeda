"""`xeda run --json` reports every node the launch touched, failed launches included, each run
directory once."""

import json
from typing import ClassVar

import pytest
from click.testing import CliRunner

from xeda.cli import cli
from xeda.flow import Flow, FlowFatalError, registered_flows


@pytest.fixture(scope="module")
def toys():
    class ToyNodeProducer(Flow):
        """Writes an output."""

        results_description: ClassVar[dict[str, str]] = {}

        def run(self) -> None:
            (self.run_path / "out.txt").write_text("out\n")
            self.artifacts.out = self.run_path / "out.txt"

    class ToyNodeFailing(Flow):
        """Depends on the producer, then fails."""

        results_description: ClassVar[dict[str, str]] = {}

        def init(self) -> None:
            self.add_dependency(ToyNodeProducer, ToyNodeProducer.Settings())

        def run(self) -> None:
            raise FlowFatalError("boom")

    class ToyNodeTwice(Flow):
        """Depends on the producer twice, with the same settings."""

        results_description: ClassVar[dict[str, str]] = {}

        def init(self) -> None:
            self.add_dependency(ToyNodeProducer, ToyNodeProducer.Settings())
            self.add_dependency(ToyNodeProducer, ToyNodeProducer.Settings())

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
    """A failure document used to carry `nodes: []`, dropping the dependency that ran."""
    result, document = _run("toy_node_failing", str(design))
    assert result.exit_code != 0 and document["success"] is False
    assert document["error"]["type"] == "FlowFatalError"
    assert _states(document) == [("toy_node_producer", "ran"), ("toy_node_failing", "failed")]
    _, document = _run("toy_node_failing", str(design))
    assert _states(document) == [("toy_node_producer", "fresh"), ("toy_node_failing", "failed")]


def test_a_directory_revisited_within_a_launch_is_one_node(toys, design):
    _, document = _run("toy_node_twice", str(design))
    assert document["success"] is True
    assert _states(document) == [("toy_node_producer", "ran"), ("toy_node_twice", "ran")]
    paths = [node["run_path"] for node in document["nodes"]]
    assert len(paths) == len(set(paths))
