"""Shared inputs for the tests that sweep every flow's settings.

`minimal_settings` is the least a flow's `Settings` needs in order to construct; `PROBES` is a
spread of values of every structural kind a settings file or a caller can supply. Sweeps pair the
two, so a new flow, setting or validator is covered without a test of its own.
"""

from collections import namedtuple
from typing import Any, Dict, List, Tuple, Type

from xeda.flow import Flow
from xeda.flow.flow import registered_flows
from xeda.flows import __builtin_flows__

assert __builtin_flows__, "importing `xeda.flows` is what populates `registered_flows`"

#: Values that let every flow's `Settings` be constructed. A new required setting should fail the
#: sweeps until its minimal valid value is added here, never silently reduce their coverage.
MINIMAL_SETTINGS: Dict[str, Any] = {
    "fpga": {"part": "xc7a100tcsg324-1"},
    "clock_period": 5.0,
    "platform": "asap7",
    "target_libraries": ["nangate45.lib"],
}

_SYNTH = {"fpga": MINIMAL_SETTINGS["fpga"], "clock_period": MINIMAL_SETTINGS["clock_period"]}

MINIMAL_SETTINGS_BY_FLOW: Dict[str, Dict[str, Any]] = {
    "vivado_postsynth_sim": {"synth": _SYNTH},
    "vivado_power": {"postsynthsim": {"synth": _SYNTH}},
}


def flow_classes() -> List[Tuple[Type[Flow], str]]:
    """Every registered flow once, as `(class, name)`, without test-only flows."""
    classes: Dict[Type[Flow], str] = {}
    for _, (_, cls) in sorted(registered_flows.items()):
        if not cls.__name__.startswith("_"):  # test-only flows registered by other modules
            classes.setdefault(cls, cls.name)
    return sorted(classes.items(), key=lambda kv: kv[1])


def minimal_settings(cls: Type[Flow], clock_period: bool = True) -> Dict[str, Any]:
    """Minimal settings for `cls`. Legacy ``clock_period`` is accepted as input-only syntax.

    It is included for synthesis settings even though it is intentionally absent from
    ``model_fields`` and is normalized into canonical ``clocks`` by the model validator.
    """
    fields = cls.Settings.model_fields
    settings = {
        key: value
        for key, value in MINIMAL_SETTINGS.items()
        if (key in fields and key != "clock_period")
        or (key == "clock_period" and clock_period and "clocks" in fields)
    }
    settings.update(MINIMAL_SETTINGS_BY_FLOW.get(cls.name, {}))
    return settings


Pair = namedtuple("Pair", "first second")

#: One value of every structural kind: absent, empty containers, scalars of each type, text that
#: looks like a number or a list, homogeneous and mixed containers, namedtuples (which break
#: `type(value)(items)`), and mappings. Paired with each setting of each flow by the sweeps.
PROBES: List[Any] = [
    None,
    [],
    {},
    (),
    "",
    0,
    3,
    2.5,
    True,
    False,
    "x",
    "5",
    "a,b",
    ["x", "y"],
    [1, 2],
    ("a", "b"),
    Pair("a", "b"),
    Pair(1, 2),
    {"k": "v"},
    {"k": 1},
    [{"name": "A", "value": 1}],
    [[1], {"a": None}],
]
