"""A public class that holds a design is not hashable, and says so; the others are hashable."""

from __future__ import annotations

import dataclasses
import inspect
from pathlib import Path

import pytest

import xeda
from xeda import Design, Spec, SweepPoint, SweepResult
from xeda._internal.outcomes import NodeResult, Result
from xeda._internal.records import ErrorInfo

DESIGN_HOLDERS = ("Spec", "Plan", "Result", "SweepPoint", "SweepResult")


def _design(tmp_path: Path) -> Design:
    (tmp_path / "top.v").write_text("module top; endmodule\n")
    return Design(name="d", root=tmp_path, rtl={"sources": ["top.v"], "top": "top"})


def _result(tmp_path: Path) -> Result:
    node = NodeResult(
        name="a",
        outcome="skipped",  # type: ignore[arg-type]
        disposition="not_run",  # type: ignore[arg-type]
        reason="",
        blocked_by=None,
        elapsed="0 s",
        record=None,
        error=None,
        deliveries=(),
        run_dir=Path("a"),
        flow="a",
    )
    return Result(
        launch_id="id",
        spec=Spec("a", _design(tmp_path)),
        roots=("a",),
        outcome="skipped",  # type: ignore[arg-type]
        error=None,
        nodes={"a": node},
    )


def test_hash_of_a_class_that_holds_a_design_raises_type_error(tmp_path):
    spec = Spec("a", _design(tmp_path))
    result = _result(tmp_path)
    point = SweepPoint(index=0, spec=spec, result=result)
    sweep = SweepResult(points=(point,), canceled=False)
    for value in (spec, result, point, sweep):
        with pytest.raises(TypeError, match="unhashable"):
            hash(value)
    assert xeda.Plan.__hash__ is None


@pytest.mark.parametrize("name", DESIGN_HOLDERS)
def test_a_class_that_holds_a_design_defines_no_hash(name):
    assert getattr(xeda, name).__hash__ is None


def test_every_other_public_dataclass_keeps_its_hash():
    """A class not in the list holds only frozen values: it stays hashable."""
    for name in xeda.__all__:
        cls = getattr(xeda, name)
        if (
            inspect.isclass(cls)
            and dataclasses.is_dataclass(cls)
            and name not in DESIGN_HOLDERS
            and cls.__dataclass_params__.eq  # type: ignore[attr-defined]
        ):
            assert cls.__hash__ is not None, name


def test_error_info_is_hashable():
    info = ErrorInfo(code="c", message="m", node=None, exit_status=None, log=None)
    assert hash(info) == hash(info)
