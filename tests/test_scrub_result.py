"""`ScrubResult` is a frozen, keyword-only dataclass like the other result classes."""

from __future__ import annotations

import copy
import dataclasses
import pickle
from pathlib import Path

import pytest

import xeda
from xeda import ScrubResult


def _result(reasons: dict[Path, str] | None = None) -> ScrubResult:
    return ScrubResult(
        scanned=[Path("a")],  # type: ignore[arg-type]
        removed=(Path("a/b"),),
        kept=(),
        gone=(),
        skipped=(Path("a/link"),),
        **({} if reasons is None else {"reasons": reasons}),
    )


def test_it_is_a_frozen_keyword_only_dataclass():
    assert dataclasses.is_dataclass(ScrubResult)
    assert not issubclass(ScrubResult, tuple)
    params = ScrubResult.__dataclass_params__  # type: ignore[attr-defined]
    assert params.frozen
    with pytest.raises(TypeError):
        ScrubResult((), (), (), (), ())  # type: ignore[misc]
    result = _result()
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.removed = ()  # type: ignore[misc]


def test_the_lists_are_tuples_and_reasons_is_a_copy_that_nobody_can_change():
    table = {Path("a/link"): "it leads elsewhere"}
    result = _result(table)
    assert result.scanned == (Path("a"),) and isinstance(result.scanned, tuple)
    table[Path("a/other")] = "later"
    assert dict(result.reasons) == {Path("a/link"): "it leads elsewhere"}
    with pytest.raises(TypeError):
        result.reasons[Path("x")] = "no"  # type: ignore[index]
    assert dict(_result().reasons) == {}


def test_it_pickles_copies_and_compares():
    result = _result({Path("a/link"): "it leads elsewhere"})
    assert pickle.loads(pickle.dumps(result)) == result
    assert copy.deepcopy(result) == result
    assert copy.copy(result) == result
    assert hash(result) == hash(copy.deepcopy(result))


def test_the_docstring_names_no_internal_function():
    assert "run_directory_problem" not in (ScrubResult.__doc__ or "")
    assert xeda.ScrubResult is ScrubResult
