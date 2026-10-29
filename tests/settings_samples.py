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
from xeda.units import Time

from .settings_fields import FieldPath, dotted, merged, nest, quantity_paths

assert __builtin_flows__, "importing `xeda.flows` is what populates `registered_flows`"

#: Values that let every flow's `Settings` be constructed. A new required setting should fail the
#: sweeps until its minimal valid value is added here, never silently reduce their coverage.
MINIMAL_SETTINGS: Dict[str, Any] = {
    "fpga": {"part": "xc7a100tcsg324-1"},
    "clocks": {"main_clock": {"period": "5 ns"}},
    "platform": "nangate45",
    "target_libraries": ["nangate45.lib"],
}

MINIMAL_SETTINGS_BY_FLOW: Dict[str, Dict[str, Any]] = {
    # The open-source place-and-route chain targets ECP5, iCE40 and Nexus, not Xilinx.
    "nextpnr": {"fpga": {"part": "LFE5U-25F-6BG381C"}},
    "fpga_pack": {"fpga": {"part": "LFE5U-25F-6BG381C"}},
    "openfpgaloader": {"fpga": {"part": "LFE5U-25F-6BG381C"}},
}


def flow_classes() -> List[Tuple[Type[Flow], str]]:
    """Every registered flow once, as `(class, name)`, without test-only flows."""
    classes: Dict[Type[Flow], str] = {}
    for _, (_, cls) in sorted(registered_flows.items()):
        if cls.__module__.startswith("xeda."):  # not the test-only flows of other modules
            classes.setdefault(cls, cls.name)
    return sorted(classes.items(), key=lambda kv: kv[1])


def minimal_settings(cls: Type[Flow], clock: bool = True) -> Dict[str, Any]:
    """Minimal settings for `cls`. `clock` says whether a synthesis flow gets its `clocks`."""
    fields = cls.Settings.model_fields
    settings = {
        key: value
        for key, value in MINIMAL_SETTINGS.items()
        if key in fields and (key != "clocks" or clock)
    }
    settings.update(MINIMAL_SETTINGS_BY_FLOW.get(cls.name, {}))
    return settings


Pair = namedtuple("Pair", "first second")

#: One value of every structural kind: absent, empty containers, scalars of each type, text that
#: looks like a number or a list, homogeneous and mixed containers, namedtuples (which break
#: `type(value)(items)`), mappings, and a quantity of each kind. Paired with each setting of each
#: flow by the sweeps.
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
    "[]",
    "[x, y]",
    ["x", "y"],
    [1, 2],
    ("a", "b"),
    Pair("a", "b"),
    Pair(1, 2),
    {"k": "v"},
    {"k": 1},
    [{"name": "A", "value": 1}],
    [[1], {"a": None}],
    # a quantity of each kind, as text with a unit, and one as an object
    "10 ns",
    "100 MHz",
    "2 fF",
    "3 um",
    "1.2 V",
    "5 ohm",
    "2 mW",
    Time("10 ns"),
]


#: Every field that holds a quantity, as `(flow class, path)`. A quantity sits in a group
#: (`abc.delay_target`), in a clock (`clocks.<key>.period`) or at the top of a flow's settings
#: (`timeout`).
QUANTITY_FIELDS: List[Tuple[Type[Flow], FieldPath]] = [
    (cls, path) for cls, _ in flow_classes() for path in quantity_paths(cls.Settings)
]
QUANTITY_FIELD_IDS: List[str] = [f"{cls.name}:{dotted(path)}" for cls, path in QUANTITY_FIELDS]


def with_quantity(cls: Type[Flow], path: FieldPath, value: Any) -> Flow.Settings:
    """The minimal settings of `cls` with `value` at the quantity field `path`."""
    return cls.Settings(**merged(minimal_settings(cls, clock=False), nest(path, value)))
