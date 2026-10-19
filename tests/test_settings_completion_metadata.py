"""The generated settings index follows the settings models that accept each input."""

from __future__ import annotations

from enum import Enum, IntEnum
from typing import Any, ClassVar, Literal

import pytest
from pydantic import AliasChoices

from xeda.dataclass import Field, XedaBaseModel, field_validator
from xeda.flow import Flow
from xeda.flow.settings_group import SettingsGroup
from xeda.introspect import settings_completion_info
from .flow_registry import preserved_flow_registry
from .tool_utils import HANG_GUARD

pytestmark = pytest.mark.python_compat


class _Primary(XedaBaseModel):
    frequency: int | None = Field(None, description="Clock frequency.")


class _Secondary(XedaBaseModel):
    phase: int | None = None


class _Choice(Enum):
    FIRST = "first"
    SECOND = "second"


class _IntegerChoice(IntEnum):
    ONE = 1
    TWO = 2


class _UnsupportedJsonType:
    pass


class _Nested(SettingsGroup):
    mode: Literal["fast", "safe"] = Field("fast", description="Operating mode.")
    hidden: str | None = Field(None, json_schema_extra={"hidden_from_schema": True})


@pytest.fixture
def completion_flow():
    """A registered flow with aliases, unions, maps, and hidden settings."""
    with preserved_flow_registry():

        class CompletionFlow(Flow):
            """A flow whose declared setting shapes exercise metadata discovery."""

            class Settings(Flow.Settings):
                opaque_key_fields: ClassVar[tuple[str, ...]] = ("opaque",)

                nested: _Nested = Field(default_factory=_Nested, description="Nested settings.")
                legacy_name: str | None = Field(None, alias="legacy-name")
                validated_name: str | None = Field(
                    None, validation_alias=AliasChoices("validation-choice")
                )
                selected_text: _Primary = Field(
                    default_factory=_Primary,
                    validation_alias=AliasChoices("selected-text"),
                )
                selected: _Primary | _Secondary | None = None
                hybrid: _Primary | dict[str, _Secondary] = Field(default_factory=_Primary)
                clocks: dict[str, _Primary] = Field(default_factory=dict)
                instruments: dict[str, _Primary] | dict[str, _Secondary] = Field(
                    default_factory=dict
                )
                labels: dict[str, str] = Field(default_factory=dict)
                opaque: dict[str, str] = Field(default_factory=dict)
                enabled: bool | None = None
                literal_flag: Literal[True, False] | None = None
                numeric_literal: Literal[1, 2] | None = None
                choice: _Choice | None = None
                integer_choice: _IntegerChoice | None = None
                flags: dict[str, bool] = Field(default_factory=dict)
                modes: dict[str, Literal["on", "off"]] = Field(default_factory=dict)
                nested_maps: dict[str, dict[str, _Primary]] = Field(default_factory=dict)
                input_hook: _Primary = Field(default_factory=_Primary)
                unrestricted: Any = None
                hook_calls: ClassVar[int] = 0

                @field_validator("input_hook", mode="before", json_schema_input_type=str)
                @classmethod
                def input_hook_from_text(cls, value):
                    if isinstance(value, str):
                        cls.hook_calls += 1
                        return {"frequency": int(value)}
                    return value

                @field_validator("selected_text", mode="before", json_schema_input_type=str)
                @classmethod
                def selected_text_from_text(cls, value):
                    if isinstance(value, str):
                        return {"frequency": int(value)}
                    return value

            def run(self) -> None:
                pass

        yield CompletionFlow


def test_fpga_completion_exposes_nested_fields_and_string_shorthand() -> None:
    from xeda.flows.fpga_pack import FpgaPack

    info = settings_completion_info(FpgaPack)

    assert info["fpga"]["children"] is True
    assert info["fpga"]["assignable"] is True
    assert info["fpga.part"]["assignable"] is True
    assert info["fpga.family"]["assignable"] is True


def test_pure_maps_and_model_or_map_fields_are_hierarchical_only(completion_flow) -> None:
    from xeda.flow import FlowSettingsError
    from xeda.flows.nextpnr import Nextpnr

    info = settings_completion_info(completion_flow)
    assert info["labels"] == {"children": True, "assignable": False, "dynamic": True}
    assert info["opaque"] == {
        "children": True,
        "assignable": False,
        "dynamic": True,
        "opaque_keys": True,
    }
    assert info["hybrid"]["assignable"] is False
    assert info["hybrid"]["dynamic"] is True
    assert info["clocks"]["assignable"] is False
    with pytest.raises(FlowSettingsError, match="clocks"):
        Nextpnr.Settings.from_input({"clocks": "foo"})


def test_validation_schema_synthetic_paths_are_indexed_recursively() -> None:
    from xeda.flows.fpga_pack import FpgaPack
    from xeda.flows.yosys.yosys_fpga import YosysFpga

    fpga = settings_completion_info(FpgaPack)
    assert fpga["clock"]["children"] is True
    assert fpga["clock"]["assignable"] is False
    assert fpga["clock.freq"]["assignable"] is True
    assert fpga["clocks.*.freq"]["assignable"] is True

    yosys = settings_completion_info(YosysFpga)
    assert yosys["ghdl.clock.freq"]["assignable"] is True
    assert yosys["ghdl.clocks.*.freq"]["assignable"] is True


def test_list_settings_remain_assignable_from_cli_text() -> None:
    from xeda.flows.yosys.yosys_fpga import YosysFpga

    settings = YosysFpga.Settings.from_input(
        {"read_verilog_flags": "-sv", "synth_flags": "-flatten,-noflatten"}
    )
    assert settings.read_verilog_flags == ["-sv"]
    assert settings.synth_flags == ["-flatten", "-noflatten"]

    info = settings_completion_info(YosysFpga)
    assert info["read_verilog_flags"]["assignable"] is True
    assert info["synth_flags"]["assignable"] is True


def test_any_settings_remain_assignable_from_cli_text(completion_flow) -> None:
    settings = completion_flow.Settings.from_input({"unrestricted": "text"})
    assert settings.unrestricted == "text"
    assert settings_completion_info(completion_flow)["unrestricted"]["assignable"] is True


def test_map_value_templates_walk_scalar_and_nested_map_types(completion_flow) -> None:
    info = settings_completion_info(completion_flow)

    assert info["flags"]["assignable"] is False
    assert info["flags.*"]["assignable"] is True
    assert info["flags.*"]["values"] == ["false", "true"]
    assert info["modes.*"]["values"] == ["off", "on"]
    assert info["nested_maps.*"]["dynamic"] is True
    assert info["nested_maps.*"]["assignable"] is False
    assert info["nested_maps.*.*"]["children"] is True
    assert info["nested_maps.*.*"]["assignable"] is False
    assert info["nested_maps.*.*.frequency"]["assignable"] is True
    assert "nested_maps.user_key.*" not in info


def test_finite_values_only_offer_cli_spellings_accepted_by_the_model(completion_flow) -> None:
    from xeda.flow import FlowSettingsError
    from xeda.flows.nvc import Nvc

    info = settings_completion_info(completion_flow)
    assert "values" not in info["literal_flag"]
    assert "values" not in info["numeric_literal"]
    assert info["choice"]["values"] == ["first", "second"]
    assert info["integer_choice"]["values"] == ["1", "2"]
    assert "values" not in settings_completion_info(Nvc)["optimization_level"]
    with pytest.raises(FlowSettingsError):
        Nvc.Settings.from_input({"optimization_level": "0"})
    with pytest.raises(FlowSettingsError):
        completion_flow.Settings.from_input({"literal_flag": "true"})
    assert info["input_hook"]["assignable"] is True
    assert completion_flow.Settings.hook_calls == 0


def test_unsupported_validation_schema_keeps_model_field_metadata() -> None:
    from pydantic import ConfigDict

    with preserved_flow_registry():

        class UnsupportedSchemaFlow(Flow):
            class Settings(Flow.Settings):
                model_config = ConfigDict(arbitrary_types_allowed=True)
                odd: _UnsupportedJsonType | None = Field(None)

            def run(self) -> None:
                pass

        info = settings_completion_info(UnsupportedSchemaFlow)
        assert info["odd"]["assignable"] is True


def test_metadata_includes_field_and_validation_aliases(completion_flow) -> None:
    info = settings_completion_info(completion_flow)

    assert info["legacy-name"]["assignable"] is True
    assert info["legacy_name"]["assignable"] is True
    assert info["validation-choice"]["assignable"] is True


def test_model_field_scalar_schema_is_used_for_validation_aliases(completion_flow) -> None:
    info = settings_completion_info(completion_flow)

    assert info["selected-text"]["assignable"] is True
    settings = completion_flow.Settings.model_validate({"selected-text": "19"})
    assert settings.selected_text.frequency == 19
    from xeda.cli_settings_completion import _settings_candidates

    candidates = _settings_candidates(
        "completion", [], "selected-tex", {"completion": {"settings": info}}
    )
    assert candidates is not None
    assert "selected-text=" in [item.value for item in candidates]


def test_opaque_settings_are_marked_for_lightweight_cli_parsing(completion_flow) -> None:
    info = settings_completion_info(completion_flow)

    assert info["opaque"]["opaque_keys"] is True


def test_metadata_includes_children_from_each_model_union_branch(completion_flow) -> None:
    info = settings_completion_info(completion_flow)

    assert info["selected"]["children"] is True
    assert info["selected"]["assignable"] is False
    assert info["selected.frequency"]["assignable"] is True
    assert info["selected.phase"]["assignable"] is True
    assert info["hybrid.frequency"]["assignable"] is True
    assert info["hybrid"]["dynamic"] is True
    assert info["hybrid.*.phase"]["assignable"] is True


def test_metadata_marks_dynamic_maps_and_templates_typed_model_keys(completion_flow) -> None:
    info = settings_completion_info(completion_flow)

    assert info["clocks"]["dynamic"] is True
    assert info["clocks.*"]["children"] is True
    assert info["clocks.*"]["assignable"] is False
    assert info["clocks.*.frequency"]["assignable"] is True
    assert info["instruments.*.frequency"]["assignable"] is True
    assert info["instruments.*.phase"]["assignable"] is True
    assert info["labels"]["dynamic"] is True
    assert "labels.*" not in info
    assert info["opaque"]["dynamic"] is True
    assert "opaque.*" not in info


def test_metadata_skips_hidden_fields_and_offers_finite_group_values(completion_flow) -> None:
    info = settings_completion_info(completion_flow)

    assert info["nested"]["assignable"] is False
    assert info["nested.mode"]["values"] == ["fast", "safe"]
    assert "nested.hidden" not in info
    assert info["enabled"]["values"] == ["false", "true"]
    assert "values" not in info["literal_flag"]
    assert info["choice"]["values"] == ["first", "second"]


def test_catalog_generator_records_settings_and_default_producers() -> None:
    import json
    import runpy

    generator = runpy.run_path("tools/generate_flow_catalog.py")
    catalog = json.loads(generator["render"]())

    fpga_pack = catalog["fpga_pack"]
    assert fpga_pack["settings"]["fpga"]["children"] is True
    assert fpga_pack["settings"]["fpga"]["assignable"] is True
    assert "default_producers" in fpga_pack
    assert "nextpnr" in fpga_pack["default_producers"]


def test_generated_catalog_has_fresh_settings_metadata() -> None:
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "tools/generate_flow_catalog.py", "--check"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=HANG_GUARD,
    )
    assert result.returncode == 0, result.stdout + result.stderr
