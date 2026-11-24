"""The annotations of `Design` say what the class returns and accepts."""

from __future__ import annotations

import collections.abc
import inspect
import typing
from pathlib import Path

import pytest

from xeda import Design


@pytest.mark.parametrize("name", ["load", "with_updates", "with_target"])
def test_a_method_that_builds_a_design_returns_self_not_a_private_type_variable(name):
    hints = typing.get_type_hints(getattr(Design, name))
    assert hints["return"] is typing.Self


def test_no_public_annotation_of_design_names_a_type_variable():
    for name, member in inspect.getmembers(Design):
        if name.startswith("_") or not callable(member):
            continue
        try:
            hints = typing.get_type_hints(member)
        except Exception:  # a property or a pydantic helper without hints
            continue
        for hint in hints.values():
            assert not isinstance(hint, typing.TypeVar), name


def test_the_license_of_the_constructor_takes_what_the_field_holds(tmp_path: Path):
    (tmp_path / "top.v").write_text("module top; endmodule\n")
    hints = typing.get_type_hints(Design.__init__)
    assert hints["license"] == str | collections.abc.Sequence[str] | None
    for given, held in (("MIT", "MIT"), (["MIT", "BSD-3-Clause"], ("MIT", "BSD-3-Clause"))):
        design = Design(
            name="d", root=tmp_path, rtl={"sources": ["top.v"], "top": "top"}, license=given
        )
        assert design.license == held
