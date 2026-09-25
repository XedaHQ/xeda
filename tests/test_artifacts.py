"""The artifact contract is the same for transfer, path rewriting and cleanup."""

from copy import deepcopy
from pathlib import Path

import pytest
from box import Box

from xeda.artifacts import iter_artifact_paths, map_artifact_paths
from xeda.flow_runner.default_runner import _artifact_rows


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
