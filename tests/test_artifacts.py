"""The artifact contract is the same for transfer, path rewriting and cleanup."""

import json
import logging
from copy import deepcopy
from pathlib import Path
from typing import ClassVar

import pytest
from box import Box

from xeda import Design
from xeda.artifacts import filter_artifact_paths, iter_artifact_paths, map_artifact_paths
from xeda.flow import Flow, registered_flows
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.default_runner import _artifact_rows

EXAMPLE = Path(__file__).parent.parent / "examples" / "vhdl" / "sqrt" / "sqrt.toml"


@pytest.mark.parametrize("as_box", [False, True])
def test_nested_artifacts_preserve_labels_order_shape_and_optional_values(as_box):
    artifacts = {
        "label_not_a_path": "netlist.json",
        "generated": [Path("a.v"), {"more": ("b.v", "netlist.json")}],
        "optional": [None, "", False, 7, [], {}],
    }
    if as_box:
        artifacts = Box(artifacts)
    before = deepcopy(artifacts)

    assert list(iter_artifact_paths(artifacts)) == [
        "netlist.json",
        Path("a.v"),
        "b.v",
        "netlist.json",
    ]
    rewritten = map_artifact_paths(artifacts, lambda path: str(Path("local") / path))
    assert rewritten == {
        "label_not_a_path": "local/netlist.json",
        "generated": ["local/a.v", {"more": ("local/b.v", "local/netlist.json")}],
        "optional": [None, "", False, 7, [], {}],
    }
    assert dict(artifacts) == before


@pytest.mark.parametrize("artifact", ["netlist.json", Path("netlist.json")])
def test_an_artifact_can_be_a_single_path(artifact):
    assert list(iter_artifact_paths(artifact)) == [artifact]
    assert (
        map_artifact_paths(artifact, lambda path: str(Path("local") / path)) == "local/netlist.json"
    )


def test_artifact_rows_show_every_path_once_under_its_label():
    """Each path gets a row in the path column, its label only on the first row of its group."""
    artifacts = {
        "bitstream": Path("top.bit"),
        "netlists": [Path("a.v"), Path("b.v")],
        "reports": ("timing.rpt", "utilization.rpt"),
        "logs": Box({"synth": {"stdout": "synth.log", "stderr": "synth.err"}}),
        "unused": [None, "", {}, []],
    }

    assert _artifact_rows(artifacts) == [
        ("bitstream", "top.bit", True),
        ("netlists", "a.v", False),
        ("", "b.v", True),
        ("reports", "timing.rpt", False),
        ("", "utilization.rpt", True),
        ("logs", "synth.log", False),
        ("", "synth.err", True),
    ]


def test_filtering_drops_rejected_paths_and_the_groups_they_emptied():
    """A rejected path leaves its group; a group whose every path was rejected leaves its
    parent, while one that never held a path stays as it was."""
    artifacts = Box(
        {
            "netlist": "gone.v",
            "generated": [Path("a.v"), Path("gone_a.v"), {"more": ("gone_b.v",)}],
            "optional": [None, "", {}],
            "kept": {"sdf": "impl.sdf", "sdc": "gone.sdc"},
        }
    )
    before = deepcopy(artifacts)

    filtered = filter_artifact_paths(artifacts, lambda path: "gone" not in str(path))

    assert filtered == {
        "generated": [Path("a.v")],
        "optional": [None, "", {}],
        "kept": {"sdf": "impl.sdf"},
    }
    assert artifacts == before


@pytest.mark.parametrize("passes", [True, False], ids=["succeeded", "failed"])
def test_a_failed_run_reports_only_the_artifacts_that_exist(passes, tmp_path, caplog):
    """A flow records its outputs before the tool runs. A failed run may not have written them,
    and its results listed them anyway: the remote runner then fetched a file that was not
    there, and a failed remote run crashed instead of reporting its failure. A successful run
    reports what it recorded."""

    class HalfWritten(Flow):
        """Record four outputs, write two of them, and succeed or fail as the test says."""

        results_description: ClassVar[dict[str, str]] = {}

        def run(self) -> None:
            (self.run_path / "written.v").write_text("module m; endmodule\n")
            (self.run_path / "outputs").mkdir()
            (self.run_path / "outputs" / "written.dcp").write_text("")
            self.artifacts.netlist = "written.v"
            self.artifacts.sdf = "never_written.sdf"
            self.artifacts.checkpoints = [
                self.run_path / "outputs" / "written.dcp",
                self.run_path / "outputs" / "never_written.dcp",
            ]
            self.results.artifacts["sdc"] = "never_written.sdc"

        def parse_reports(self) -> bool:
            return passes

    design = Design.from_file(EXAMPLE)
    try:
        with caplog.at_level(logging.WARNING):
            flow = DefaultRunner(tmp_path / "run", display_results=False).launch_flow(
                HalfWritten, design, {}
            )
    finally:
        for name in (HalfWritten.name, HalfWritten.__name__):
            registered_flows.pop(name, None)

    assert flow.succeeded == passes
    written_dcp = flow.run_path / "outputs" / "written.dcp"
    missing_dcp = flow.run_path / "outputs" / "never_written.dcp"
    expected = {"netlist": "written.v", "checkpoints": [str(written_dcp)]}
    if passes:
        expected["checkpoints"].append(str(missing_dcp))
        expected |= {"sdf": "never_written.sdf", "sdc": "never_written.sdc"}
    saved = json.loads((flow.run_path / "results.json").read_text())
    assert saved["success"] == passes
    assert saved["artifacts"] == expected
    assert json.loads(json.dumps(flow.results.artifacts, default=str)) == expected

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    dropped = [w for w in warnings if "never_written" in w]
    if passes:
        assert not dropped
    else:
        (warning,) = dropped
        assert warning.startswith("half_written failed")
        for dropped in (
            "sdf: never_written.sdf",
            f"checkpoints: {missing_dcp}",
            "sdc: never_written.sdc",
        ):
            assert dropped in warning
