"""The shapes of the plain mappings that `xeda.introspect` returns, as `TypedDict` classes.

Each function returns a new `dict` for each call, and a caller may change it. A mapping with dynamic
keys (a JSON Schema, the definitions of a settings model) is a `dict` of `Any`.

This module has no `from __future__ import annotations` on purpose: `TypedDict` reads `NotRequired`
from an annotation that is not text.
"""

from typing import Any, Literal, Mapping

from typing_extensions import NotRequired, TypedDict

from .dataclass import TierName
from .flow.io import Cardinality, ValueKind
from .flow.metrics import MetricType

__all__ = [
    "ArtifactDescription",
    "BoardInfo",
    "FlowBinding",
    "FlowInfo",
    "FlowInputInfo",
    "FlowOutputInfo",
    "FlowRelation",
    "MetricDescription",
    "PlatformInfo",
    "ResultsInfo",
    "SettingInfo",
    "SettingsInfo",
]

Category = Literal["simulation", "synthesis", "fpga_synthesis", "asic_synthesis", "other"]


class _Experimental(TypedDict):
    """The key `experimental`, which a listing has only for a flow or setting that may change in
    any release. A listing that holds it unpacks this class at the place of the key."""

    experimental: NotRequired[bool]


class FlowBinding(TypedDict):
    """One input and output pair that a link of a chain binds (`FlowRelation.binds`)."""

    #: The flow whose input the pair binds.
    flow: str
    #: The name of the input.
    input: str
    #: The name of the output that feeds it.
    output: str


class FlowRelation(TypedDict):
    """One flow that can follow or precede another in a chain (`FlowInfo.can_follow`)."""

    #: The other flow that the relation names.
    flow: str
    #: The output of the producer that a request must name, or `None` when the plain request is
    #: valid.
    output: str | None
    #: The flows that the link passes through, in chain order. It is empty for a direct link.
    through: list[str]
    #: The input and output pairs that the link binds.
    binds: list[FlowBinding]
    #: Whether the target settings decide the relation, because a type is a union that they narrow.
    target_dependent: bool


class FlowInputInfo(TypedDict):
    """One declared input of a flow (`FlowInfo.inputs`)."""

    #: The name of the input.
    name: str
    #: What the input takes: `files`, or `design` for the design input.
    value: ValueKind
    #: The names of the `SourceType` members that the input takes. It is empty for `design`.
    types: list[str]
    #: `one` (one file), `optional` (zero or one) or `many` (a list).
    cardinality: Cardinality
    #: Whether the flow cannot run without the input.
    required: bool
    #: Whether the input may be absent.
    optional: bool
    #: Whether the input compares what its type calls the same, not the bytes.
    equivalence: bool
    #: The flow that produces the input by default, or `None`.
    producer: str | None
    #: The output of that producer, or `None`.
    output: str | None
    #: The input of the flow that must come from the same producer as this input, or `None`.
    same_producer_as: str | None
    #: Whether this input and `same_producer_as` must both come from the design or both from one
    #: producer.
    paired: bool
    #: The input of the flow that makes `same_producer_as`, and whose producer this input must
    #: come from, or `None`.
    via: str | None
    #: What the input is.
    description: str


class FlowOutputInfo(TypedDict):
    """One declared output of a flow (`FlowInfo.outputs`)."""

    #: The name of the output.
    name: str
    #: What the output makes: `files`, or `design` for the design that a converter derives.
    value: ValueKind
    #: The names of the `SourceType` members that the output has. It is empty for `design`.
    types: list[str]
    #: `one` (one file), `optional` (zero or one) or `many` (a list).
    cardinality: Cardinality
    #: The setting that switches the output on, or `None` for an output that is always written.
    enabled_by: str | None
    #: What the output is.
    description: str


class FlowInfo(TypedDict):
    """What `xeda.introspect.flow_info` knows about a flow."""

    #: The canonical name of the flow.
    name: str
    #: The docstring of the flow class itself, or `None`. A flow never inherits it.
    description: str | None
    #: The kind of the flow.
    category: Category
    #: Whether the flow can run a cocotb testbench.
    supports_cocotb: bool
    #: `True` for a flow that may change in any release. The key is absent for every other flow.
    experimental: NotRequired[bool]
    #: The default producers of the declared inputs.
    dependencies: list[str]
    #: Why the flow can only end a chain, or `None`.
    action_reason: str | None
    #: The declared inputs.
    inputs: list[FlowInputInfo]
    #: The declared outputs.
    outputs: list[FlowOutputInfo]
    #: The flows that this flow can follow (`other+this`). The direct relations come first.
    can_follow: list[FlowRelation]
    #: The flows that can follow this flow (`this+other`). The direct relations come first.
    can_precede: list[FlowRelation]


class SettingInfo(TypedDict):
    """One setting of a flow (`SettingsInfo.fields`)."""

    #: The dotted name that `-s` and a design file accept.
    name: str
    #: The type of the value, as text.
    type: str
    #: Whether the flow cannot run without the setting.
    required: bool
    #: The unit of the value, or `None`. The key is absent for a setting that only the input form
    #: of a design file accepts.
    unit: NotRequired[str | None]
    #: The default value, as JSON data.
    default: Any
    #: What the setting does, or `None`.
    description: str | None
    #: The accepted values of a closed set, or `None`.
    enum: list[Any] | None
    #: Whether every flow accepts the setting.
    common: bool
    #: `working` or `deliverable` for a path that the flow writes. `None`: the flow reads it.
    writes: str | None
    #: Whether the keys of the setting are a tool's names, so a dot in a key is part of the name.
    opaque_keys: bool
    #: The JSON Schema of the setting.
    json_schema: Any
    #: How often a user needs the setting.
    tier: TierName
    #: The top-level setting that holds the group of this setting, or `None` outside a group.
    group: str | None
    #: `True` for a setting that may change in any release. The key is absent for every other.
    experimental: NotRequired[bool]


class SettingsInfo(TypedDict):
    """What `xeda.introspect.settings_info` knows about the settings of a flow."""

    #: The canonical name of the flow.
    flow: str
    #: The settings in order: each top-level setting, then the fields of its group, depth first.
    fields: list[SettingInfo]
    #: The JSON Schema of the nested models, by name.
    definitions: dict[str, Any]
    #: The JSON Schema of the settings model.
    json_schema: dict[str, Any]


class MetricDescription(TypedDict):
    """A metric that a flow can write (`ResultsInfo.metrics`): the fields of `MetricInfo`."""

    #: The dotted path of the metric.
    path: str
    #: What the metric is.
    description: str
    #: The type of the value.
    type: MetricType
    #: The unit of the value, or `None`.
    unit: str | None


class ArtifactDescription(TypedDict):
    """An artifact that a flow can write (`ResultsInfo.artifacts`): a file of the run directory."""

    #: The key of the artifact in `RunRecord.artifacts`.
    key: str
    #: What the file is.
    description: str
    #: When the run writes the file.
    when: str


class ResultsInfo(TypedDict):
    """What `xeda.introspect.results_info` knows about the results of a flow."""

    #: The canonical name of the flow.
    flow: str
    #: The metrics that the flow can write.
    metrics: list[MetricDescription]
    #: The artifacts that the flow can write.
    artifacts: list[ArtifactDescription]
    #: A reminder that some metrics are present only under some settings.
    note: str


class BoardInfo(TypedDict):
    """An FPGA board that ships with Xeda (`xeda.introspect.boards_info`)."""

    #: The name of the board, in lower case.
    board: str
    #: The FPGA of the board, with the keys of the `fpga` setting.
    fpga: Mapping[str, Any]
    #: The name of the board in openFPGALoader. The key is absent for a board that it does not know.
    openfpgaloader_board: NotRequired[str]
    #: The local pin file of the board in the format `lpf`. A board has the one pin file whose key
    #: is the pin format of its family.
    lpf: NotRequired[str]
    #: A pin file of the format `pcf`, as above.
    pcf: NotRequired[str]
    #: A pin file of the format `pdc`, as above.
    pdc: NotRequired[str]
    #: A pin file of the format `xdc`, as above.
    xdc: NotRequired[str]


class PlatformInfo(TypedDict):
    """An ASIC platform that ships with Xeda (`xeda.introspect.platforms_info`)."""

    #: The name that the `platform` setting takes.
    platform: str
    #: What the platform is, or `None`.
    description: str | None
    #: The default provider of the PDK files.
    provider: str
    #: The version that Xeda pins for the platform and the provider.
    version: str
    #: Whether the default store holds the PDK files of that version.
    installed: bool
    #: The directory that holds the files. The key is present only when `installed` is true.
    directory: NotRequired[str]
    #: Why the files in the store do not match the pinned version. The key is absent when they do.
    problem: NotRequired[str]
