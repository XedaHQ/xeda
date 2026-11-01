"""The public functions of `xeda.introspect` take a flow name, and `design_schema` a keyword."""

import pytest

from xeda.errors import FlowNotFoundError
from xeda.flows import Yosys
from xeda.introspect import design_schema, flow_info, results_info, settings_info

NAMED = [flow_info, settings_info, results_info]


@pytest.mark.parametrize("function", NAMED)
def test_a_flow_name_gives_the_flow(function) -> None:
    info = function("yosys")
    assert info.get("flow", info.get("name")) == "yosys"


@pytest.mark.parametrize("function", NAMED)
def test_an_unknown_flow_name_is_a_flow_not_found_error(function) -> None:
    with pytest.raises(FlowNotFoundError):
        function("no_such_flow")


@pytest.mark.parametrize("function", NAMED)
def test_a_flow_class_is_refused(function) -> None:
    with pytest.raises(TypeError, match="flow name"):
        function(Yosys)


def test_design_schema_takes_its_flag_by_keyword() -> None:
    with pytest.raises(TypeError):
        design_schema(False)  # type: ignore[misc]
    assert design_schema(input_syntax=False) != design_schema()
