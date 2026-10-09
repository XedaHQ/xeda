"""A flow built directly works on its own copies of the design and the settings it was given.

The launcher has always given a flow copies (`tests/test_flow_design_isolation.py`); a plugin, a
notebook or a test that builds a flow itself must get the same: a flow completes its settings
(paths, clocks) and some edit the design, and none of that is the caller's to find changed.
"""

import copy
from pathlib import Path

from xeda import Design
from xeda.flows import VivadoPostsynthSim, VivadoSynth

RESOURCES = Path(__file__).parent / "resources"
SETTINGS = {"fpga": "xc7a12tcsg325-1", "clock_period": 5.5}


def _design() -> Design:
    return Design.from_file(RESOURCES / "design0" / "design0.toml")


def test_a_flow_built_from_objects_keeps_its_own_copies(tmp_path) -> None:
    design = _design()
    settings = VivadoSynth.Settings(**SETTINGS)
    asked_design, asked_settings = design.model_dump_json(), settings.model_dump_json()
    flow = VivadoSynth(settings, design, tmp_path)
    assert flow.design is not design and flow.settings is not settings
    assert design.model_dump_json() == asked_design
    assert settings.model_dump_json() == asked_settings


def test_what_a_flow_does_to_its_design_and_settings_is_not_seen_by_the_caller(tmp_path) -> None:
    design = _design()
    settings = VivadoPostsynthSim.Settings(**SETTINGS)
    asked = (copy.deepcopy(design.model_dump()), copy.deepcopy(settings.model_dump()))
    flow = VivadoPostsynthSim(settings, design, tmp_path)
    flow.design.rtl.top = "something_else"
    flow.design.tb.top = ("a", "b")
    flow.settings.clocks["extra"] = flow.settings.main_clock.model_copy()
    flow.settings.suppress_msgs.append("Synth 8-0000")
    assert (design.model_dump(), settings.model_dump()) == asked


def test_a_flow_built_from_mappings_is_unchanged(tmp_path) -> None:
    flow = VivadoSynth(dict(SETTINGS), _design(), tmp_path)  # type: ignore[arg-type]
    assert flow.settings.main_clock.period == 5.5
