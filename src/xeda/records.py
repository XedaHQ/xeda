"""The values that a launch records and a caller reads.

A run leaves one record, `results.json`: a `RunRecord`. This module owns its schema. `record_of`
builds a record from a flow that ran, `encode_record` makes its document, and `decode_record` and
`read_record` read it back. Every reader of a stored run goes through them.

The module imports no flow module when it loads, so a reader needs nothing of the flows.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Final, Literal, cast

from .errors import RecordError, RunReplacedError
from .frozen import freeze, thaw
from .generation import generation_storage
from .outcomes import Outcome

if TYPE_CHECKING:
    from .flow.flow import Flow
    from .flow.metrics import MetricSpec

__all__ = [
    "RECORD_SCHEMA_VERSION",
    "ErrorInfo",
    "InputRecord",
    "InputSource",
    "InvalidMetric",
    "MetricInfo",
    "OpenRun",
    "OutputRecord",
    "RecordProblem",
    "RunListing",
    "RunRecord",
    "collect_metrics",
    "decode_record",
    "encode_record",
    "name_in",
    "nest_metrics",
    "read_record",
    "read_stored_record",
    "record_of",
    "stored_outputs",
]

#: The schema version that `encode_record` writes.
RECORD_SCHEMA_VERSION: Final = 1

RESULTS_FILE: Final = "results.json"

# The codes of the errors that a node records (`flow_runner.error_codes` re-exports them). They
# are defined here so that the flow classes can write an error without importing the launcher.
NONZERO_EXIT = "nonzero_exit"
TIMEOUT = "timeout"
EXECUTABLE_NOT_FOUND = "executable_not_found"
REPORTED_FAILURE = "reported_failure"
MISSING_REPORT = "missing_report"
MISSING_OUTPUT = "missing_output"
MISSING_GENERATED = "missing_generated"
INVALID_METRIC = "invalid_metric"
REJECTED = "rejected"
PRODUCER_FAILED = "producer_failed"
CANCELED = "canceled"
STORAGE_ERROR = "storage_error"
INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class ErrorInfo:
    """Why a node failed, was rejected or was canceled, or why a launch failed.

    `code` is a stable lower snake case text. Its set is open: a reader keeps a code it does not
    know as text. `node` is the node that the error belongs to, `None` for an error of the whole
    launch. `exit_status` is the exit status of the tool, for `nonzero_exit`. `log` is the key of
    the artifact to read for the cause, if there is one."""

    code: str
    message: str
    node: str | None
    exit_status: int | None
    log: str | None

    def as_json_value(self) -> dict[str, Any]:
        """Return the JSON form: the document `results.json` holds under `error`."""
        return {
            "code": self.code,
            "message": self.message,
            "node": self.node,
            "exit_status": self.exit_status,
            "log": self.log,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> ErrorInfo:
        """Return the error that `as_json_value` wrote. A value of another shape is a `ValueError`."""
        if not isinstance(value, Mapping):
            raise ValueError(f"an error is a mapping, not {type(value).__name__}")
        for key in ("code", "message"):
            if not isinstance(value.get(key), str):
                raise ValueError(f"the `{key}` of an error is text")
        for key in ("node", "log"):
            if value.get(key) is not None and not isinstance(value[key], str):
                raise ValueError(f"the `{key}` of an error is text or null")
        status = value.get("exit_status")
        if status is not None and (isinstance(status, bool) or not isinstance(status, int)):
            raise ValueError("the `exit_status` of an error is an integer or null")
        return cls(
            code=value["code"],
            message=value["message"],
            node=value.get("node"),
            exit_status=status,
            log=value.get("log"),
        )


#: The outcomes that a record holds. A node that did not run (blocked, skipped) has no record.
OUTCOMES: Final = ("passed", "rejected", "failed", "canceled")

#: The types of a metric value (`xeda.flow.metrics.MetricType`).
_METRIC_TYPES: Final = (
    "number",
    "integer",
    "boolean",
    "text",
    "text_list",
    "counts",
    "numbers",
    "object",
)

_CARDINALITIES: Final = ("one", "optional", "many")
_ORIGINS: Final = ("source", "producer", "none")

#: The kind of an output that holds the design a converter derived.
DESIGN_KIND: Final = "design"


class InvalidMetric(ValueError):
    """A flow wrote a value that is not a metric: it is not finite, or not of its declared type.

    `path` is the metric and `problem` says what is wrong with its value."""

    def __init__(self, path: str, problem: str) -> None:
        super().__init__(f"the metric `{path}` {problem}")
        self.path = path
        self.problem = problem


@dataclass(frozen=True)
class MetricInfo:
    """What a metric of a record is: the path, the description, the type of its value and the unit.

    `unit` is a unit text of the units table of `xeda.units`, or `None`. For the type `numbers`,
    it is the unit of every value."""

    path: str
    description: str
    type: Literal[
        "number", "integer", "boolean", "text", "text_list", "counts", "numbers", "object"
    ]
    unit: str | None


@dataclass(frozen=True)
class OutputRecord:
    """One declared output of a run.

    `names` are the files the run wrote for it, relative to the run directory, and `digests` are
    their content digests, in the same order. Both are empty when the run wrote none. `design` is
    the derived design, for the kind `design` when the run wrote it, else `None`: its top, the
    design it came from, and its sources, in order. A source is a file that the converter
    generated (`generated`, a name in `names`) or a source of the input design that passes
    through (`source`, its index, and `file`).
    """

    name: str
    kind: str
    cardinality: Literal["one", "optional", "many"]
    names: tuple[str, ...]
    digests: tuple[str, ...]
    design: Mapping[str, Any] | None

    def __post_init__(self) -> None:
        for name in ("names", "digests", "design"):
            object.__setattr__(self, name, freeze(getattr(self, name)))


@dataclass(frozen=True)
class InputSource:
    """One source file that a run read through a declared input.

    `path` is relative to the design root. For a generated source, it is the name in the output of
    its producer. `dependency` is the index of the dependency in `RunRecord.dependencies`, or
    `None`. `producer` tells which producer wrote a generated source: `None` for the generator of
    the design itself, the name of the design for the generator of a dependency, and the name of
    the node for a converter. It is `None` for a written source."""

    path: str
    generated: bool
    digest: str
    dependency: int | None = None
    producer: str | None = None


@dataclass(frozen=True)
class InputRecord:
    """Where one declared input of a run came from, through one binding.

    An input of cardinality `many` that several producer outputs feed has one `InputRecord` for
    each of them, in binding order, so `name` repeats in `RunRecord.inputs`. `producer_run_dir` is
    absolute: the run root of the record joined with the relative text that the document holds."""

    name: str
    origin: Literal["source", "producer", "none"]
    producer: str | None
    output: str | None
    producer_run_id: str | None
    producer_run_dir: Path | None
    generation: str | None
    sources: tuple[InputSource, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "sources", tuple(self.sources))


@dataclass(frozen=True)
class RunRecord:
    """Everything that one execution of a node left behind: the record in `results.json`.

    Every table and list in it is immutable (`xeda.frozen`). `run_root` and `run_dir` say where
    the record is stored now; the document does not hold them. `outcome` is `passed`, `rejected`,
    `failed` or `canceled`. A passed record has no `error`, and every other has one. `metrics` is
    nested by the dotted path of each metric, and `metric_info` is keyed by the same paths: its
    keys are exactly the leaf paths that hold a value. `outputs` has an entry for every declared
    output. `artifacts` maps a key to the name of a file written by the run, relative to the run
    directory."""

    schema_version: int
    run_id: str
    xeda_version: str
    flow: str
    design: str
    target: str | None
    identity: str
    configuration: Mapping[str, Any]
    run_root: Path
    run_dir: Path
    outcome: Outcome
    error: ErrorInfo | None
    metrics: Mapping[str, Any]
    metric_info: Mapping[str, MetricInfo]
    outputs: Mapping[str, OutputRecord]
    artifacts: Mapping[str, str]
    settings: Mapping[str, Any]
    inputs: tuple[InputRecord, ...]
    started: datetime
    finished: datetime
    runtime: float
    inherited_variables: tuple[str, ...]
    dependencies: tuple[Mapping[str, str | None], ...] = field(default=())

    def __post_init__(self) -> None:
        outcome = Outcome(self.outcome)
        if outcome not in OUTCOMES:
            raise ValueError(f"a record cannot hold the outcome `{outcome}`")
        object.__setattr__(self, "outcome", outcome)
        for name in (
            "configuration",
            "metrics",
            "metric_info",
            "outputs",
            "artifacts",
            "settings",
            "inputs",
            "inherited_variables",
            "dependencies",
        ):
            object.__setattr__(self, name, freeze(getattr(self, name)))

    @property
    def passed(self) -> bool:
        """Whether the run passed."""
        return self.outcome is Outcome.PASSED

    def metric(self, path: str) -> Any:
        """Return the metric at the dotted `path` (`sim.ended_by`).

        A path that no metric has raises `KeyError`. The path of a group of metrics (`sim`) gives
        the table of what is below it. A path below a metric is not a metric: it raises `KeyError`
        too."""
        node: Any = self.metrics
        walked = ""
        for segment in path.split("."):
            walked = f"{walked}.{segment}" if walked else segment
            if not isinstance(node, Mapping) or segment not in node:
                raise KeyError(path)
            node = node[segment]
            if walked in self.metric_info and walked != path:
                raise KeyError(path)
        return node

    def to_dict(self) -> dict[str, Any]:
        """Return the `results.json` document of the record (`encode_record`)."""
        return encode_record(self)

    @contextlib.contextmanager
    def open(self) -> Iterator[OpenRun]:
        """Hold the files of the run for the length of a `with` block, and give their paths.

        The block holds a shared lease on the run directory, and on each directory that holds a
        file that the run read and a producer wrote (a generation storage, the run directory of
        a converter). No launch, clean-up or scrub changes them until the block ends: each waits.
        Under the leases, `results.json` must still hold this record's `run_id`.

        Raise `RunReplacedError` when another run replaced this one, and `RecordError`
        (`missing_record`, `corrupt_record`) when its record is gone or unreadable. A generated
        source that is gone or that holds other bytes than the run read is no error: `OpenRun`
        names it in `missing_generated`. A thread that holds a lock of Xeda only in a callback of
        Xeda can open a record whose locks that call holds, and no other (`LeaseHeldError`)."""
        from .flow_runner.run_lock import LockRequest, acquire_all

        where = self.run_dir / RESULTS_FILE
        if not os.path.lexists(where):
            raise RecordError(f"{where} does not exist", code="missing_record", path=where)
        places = _generated_places(self)
        leases = [
            LockRequest("storage" if kind == "storage" else "run_dir", directory, False)
            for kind, directory, _source in places.values()
            if directory is not None and directory.is_dir()
        ]
        with acquire_all(
            [LockRequest("run_dir", self.run_dir, False), *leases], run_root=self.run_root
        ):
            current = read_record(self.run_dir, run_root=self.run_root)
            if current.run_id != self.run_id:
                raise RunReplacedError(
                    f"{where} holds the run {current.run_id}: the run {self.run_id} that this "
                    "record describes was replaced",
                    code="run_replaced",
                    path=where,
                    expected_run_id=self.run_id,
                    found_run_id=current.run_id,
                )
            yield _open_run(self, places)


@dataclass(frozen=True)
class OpenRun:
    """The files of a run, while `RunRecord.open()` holds them. It is valid only in its block.

    `outputs` has an entry for each declared output: its absolute files, or `()` for an output
    that the run did not write. `artifacts` maps each key to an absolute file. `generated_sources`
    maps the generated sources that the run read to their files, which hold the bytes that the
    run read. The key is the name of the source in the output of its producer for the generator of
    the design itself, and `<producer>:<name>` for a converter (its node) or for the generator
    of a dependency (its design). `missing_generated` holds the keys of the sources that are gone
    or hold other bytes now, or whose place the record does not name."""

    record: RunRecord
    run_dir: Path
    outputs: Mapping[str, tuple[Path, ...]]
    artifacts: Mapping[str, Path]
    generated_sources: Mapping[str, Path]
    missing_generated: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("outputs", "artifacts", "generated_sources"):
            object.__setattr__(self, name, freeze(getattr(self, name)))
        object.__setattr__(self, "missing_generated", tuple(self.missing_generated))

    def output(self, name: str) -> Path:
        """Return the one file of the output `name`.

        Raise `KeyError` for a name that is no output, and `RecordError` for an output that the run
        did not write (`not_written`) and for an output of many files (`several_files`: read
        `outputs[name]`)."""
        output = self.record.outputs[name]
        where = self.run_dir / RESULTS_FILE
        if output.cardinality == "many":
            raise RecordError(
                f"the output `{name}` holds many files: read `outputs[{name!r}]`",
                code="several_files",
                path=where,
            )
        files = self.outputs[name]
        if not files:
            raise RecordError(
                f"the run did not write the output `{name}`", code="not_written", path=where
            )
        return files[0]


@dataclass(frozen=True)
class RecordProblem:
    """A run directory whose record could not be read: its `results.json` and why.

    `code` is `missing_record`, `corrupt_record` or `unsupported_schema`."""

    path: Path
    code: str
    message: str


@dataclass(frozen=True)
class RunListing:
    """The records of the run directories that a listing found, sorted by run directory, and the
    run directories whose record could not be read."""

    records: tuple[RunRecord, ...]
    problems: tuple[RecordProblem, ...]

    def __iter__(self) -> Iterator[RunRecord]:
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)


# --- Metrics -----------------------------------------------------------------------------------


def collect_metrics(
    flat: Mapping[str, Any], specs: Mapping[str, MetricSpec]
) -> tuple[dict[str, Any], dict[str, MetricInfo], list[InvalidMetric]]:
    """Return the metrics among the results `flat`, nested, with their descriptions and problems.

    Only a key that `specs` describes is a metric. A key that starts with `_` is internal, and a
    value `None` is unknown: neither is a metric. A dotted key nests (`sim.ended_by` is the key
    `ended_by` of the table `sim`). `metric_info` is keyed by the dotted path of each metric that
    holds a value.

    A value that is not finite (a number inside a list or a table too), a value of another type
    than the one declared, and a key that clashes with another are not metrics. The third result
    lists them, one `InvalidMetric` each, and the others stay."""
    from .flow.metrics import non_finite_in, value_fits

    metrics: dict[str, Any] = {}
    info: dict[str, MetricInfo] = {}
    problems: list[InvalidMetric] = []
    for key, value in flat.items():
        if not isinstance(key, str) or key.startswith("_") or value is None:
            continue
        spec = specs.get(key)
        if spec is None:
            continue
        if not value_fits(spec.type, value):
            problems.append(
                InvalidMetric(key, f"is not of its declared type {spec.type}: {value!r:.80}")
            )
            continue
        place = non_finite_in(value)
        if place is not None:
            problems.append(InvalidMetric(key, f"is not finite: it holds a number at `{place}`"))
            continue
        segments = key.split(".")
        node = metrics
        clash: str | None = None
        for index, segment in enumerate(segments[:-1]):
            above = ".".join(segments[: index + 1])
            if above in info:
                clash = above
                break
            node = node.setdefault(segment, {})
        if clash is None and segments[-1] in node:
            clash = key
        if clash is not None:
            problems.append(InvalidMetric(key, f"clashes with the metric `{clash}`"))
            continue
        node[segments[-1]] = value
        info[key] = MetricInfo(key, spec.description, spec.type, spec.unit)
    return metrics, info, problems


def nest_metrics(
    flat: Mapping[str, Any], specs: Mapping[str, MetricSpec]
) -> tuple[dict[str, Any], dict[str, MetricInfo]]:
    """Return the metrics among the results `flat`, nested, and the description of each.

    The rules are those of `collect_metrics`. The first value that is not a metric raises
    `InvalidMetric`."""
    metrics, info, problems = collect_metrics(flat, specs)
    if problems:
        raise problems[0]
    return metrics, info


# --- The document ------------------------------------------------------------------------------


def _utc_text(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _run_dir_text(path: Path, run_root: Path) -> str:
    """Return how a record names the run directory `path`: relative to the run root in POSIX form,
    or absolute if it lies outside the run root."""
    where, root = Path(os.path.abspath(path)), Path(os.path.abspath(run_root))
    return (where.relative_to(root) if where.is_relative_to(root) else where).as_posix()


def _encode_output(output: OutputRecord) -> dict[str, Any]:
    document: dict[str, Any] = {
        "kind": output.kind,
        "cardinality": output.cardinality,
        "names": list(output.names),
        "digests": list(output.digests),
        "design": None if output.design is None else thaw(output.design),
    }
    return document


def _encode_input(item: InputRecord, run_root: Path) -> dict[str, Any]:
    return {
        "name": item.name,
        "origin": item.origin,
        "producer": item.producer,
        "output": item.output,
        "producer_run_id": item.producer_run_id,
        "producer_run_dir": (
            None
            if item.producer_run_dir is None
            else _run_dir_text(item.producer_run_dir, run_root)
        ),
        "generation": item.generation,
        "sources": [
            {
                "path": source.path,
                "generated": source.generated,
                "digest": source.digest,
                "dependency": source.dependency,
                "producer": source.producer,
            }
            for source in item.sources
        ],
    }


def encode_record(record: RunRecord) -> dict[str, Any]:
    """Return the `results.json` document of `record`: plain JSON values, finite numbers only.

    The document holds neither the run root nor the run directory: they follow from where the file
    is."""
    return {
        "schema_version": record.schema_version,
        "run_id": record.run_id,
        "xeda_version": record.xeda_version,
        "flow": record.flow,
        "design": record.design,
        "target": record.target,
        "identity": record.identity,
        "configuration": thaw(record.configuration),
        "outcome": record.outcome.value,
        "error": None if record.error is None else record.error.as_json_value(),
        "metrics": thaw(record.metrics),
        "metric_info": {
            path: {"description": info.description, "type": info.type, "unit": info.unit}
            for path, info in record.metric_info.items()
        },
        "outputs": {name: _encode_output(output) for name, output in record.outputs.items()},
        "artifacts": dict(record.artifacts),
        "settings": thaw(record.settings),
        "inputs": [_encode_input(item, record.run_root) for item in record.inputs],
        "started": _utc_text(record.started),
        "finished": _utc_text(record.finished),
        "runtime": record.runtime,
        "dependencies": thaw(record.dependencies),
        "inherited_variables": list(record.inherited_variables),
    }


class _Malformed(ValueError):
    """A document is not a record. The message says where."""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _Malformed(f"`{label}` is not an object")
    return value


def _sequence(value: Any, label: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise _Malformed(f"`{label}` is not a list")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise _Malformed(f"`{label}` is not text")
    return value


def _optional_text(value: Any, label: str) -> str | None:
    return None if value is None else _text(value, label)


def _texts(value: Any, label: str) -> tuple[str, ...]:
    return tuple(_text(item, f"{label}[]") for item in _sequence(value, label))


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _Malformed(f"`{label}` is not an integer")
    return value


def _file_name(value: Any, label: str) -> str:
    """Return a name relative to a run directory: a name that leads out of it is not one."""
    name = _text(value, label)
    path = PurePosixPath(name)
    if not path.parts or path.is_absolute() or ".." in path.parts or "\\" in name or "\x00" in name:
        raise _Malformed(f"`{label}` is not a name inside the run directory: {name!r}")
    return name


def _moment(value: Any, label: str) -> datetime:
    try:
        moment = datetime.fromisoformat(_text(value, label))
    except ValueError:
        raise _Malformed(f"`{label}` is not a time") from None
    if moment.tzinfo is None:
        raise _Malformed(f"`{label}` has no time zone")
    return moment.astimezone(timezone.utc)


def _decode_design(value: Any, label: str, names: tuple[str, ...]) -> Mapping[str, Any]:
    """Check the derived design of an output, and return it."""
    design = _mapping(value, label)
    _optional_text(design.get("top"), f"{label}.top")
    _text(design.get("input_design"), f"{label}.input_design")
    generated: list[str] = []
    for item in _sequence(design.get("sources"), f"{label}.sources"):
        source = _mapping(item, f"{label}.sources[]")
        _text(source.get("type"), f"{label}.sources[].type")
        if "generated" in source:
            generated.append(_file_name(source["generated"], f"{label}.sources[].generated"))
        else:
            _integer(source.get("source"), f"{label}.sources[].source")
            _text(source.get("file"), f"{label}.sources[].file")
    if tuple(generated) != names:
        raise _Malformed(f"`{label}.sources` generate other files than `names` lists")
    return design


def _decode_output(name: str, value: Any) -> OutputRecord:
    label = f"outputs.{name}"
    item = _mapping(value, label)
    kind = _text(item.get("kind"), f"{label}.kind")
    cardinality = item.get("cardinality")
    if cardinality not in _CARDINALITIES:
        raise _Malformed(f"`{label}.cardinality` is not one of {', '.join(_CARDINALITIES)}")
    names = tuple(
        _file_name(entry, f"{label}.names[]")
        for entry in _sequence(item.get("names"), f"{label}.names")
    )
    digests = _texts(item.get("digests"), f"{label}.digests")
    if len(digests) != len(names):
        raise _Malformed(f"`{label}` has {len(names)} names and {len(digests)} digests")
    if kind != DESIGN_KIND and cardinality != "many" and len(names) > 1:
        raise _Malformed(f"`{label}` of cardinality {cardinality} has {len(names)} names")
    design = item.get("design")
    if design is not None and kind != DESIGN_KIND:
        raise _Malformed(f"`{label}.design` is there only if the kind is `{DESIGN_KIND}`")
    if design is None and kind == DESIGN_KIND and names:
        raise _Malformed(f"`{label}` names generated files, and it has no `design`")
    return OutputRecord(
        name=name,
        kind=kind,
        cardinality=cardinality,
        names=names,
        digests=digests,
        design=None if design is None else _decode_design(design, f"{label}.design", names),
    )


def _decode_input(value: Any, run_root: Path) -> InputRecord:
    item = _mapping(value, "inputs[]")
    origin = item.get("origin")
    if origin not in _ORIGINS:
        raise _Malformed(f"`inputs[].origin` is not one of {', '.join(_ORIGINS)}")
    where = _optional_text(item.get("producer_run_dir"), "inputs[].producer_run_dir")
    sources = []
    for entry in _sequence(item.get("sources"), "inputs[].sources"):
        source = _mapping(entry, "inputs[].sources[]")
        generated = source.get("generated")
        if not isinstance(generated, bool):
            raise _Malformed("`inputs[].sources[].generated` is not true or false")
        dependency = source.get("dependency")
        sources.append(
            InputSource(
                path=_text(source.get("path"), "inputs[].sources[].path"),
                generated=generated,
                digest=_text(source.get("digest"), "inputs[].sources[].digest"),
                dependency=(
                    None
                    if dependency is None
                    else _integer(dependency, "inputs[].sources[].dependency")
                ),
                producer=_optional_text(source.get("producer"), "inputs[].sources[].producer"),
            )
        )
    return InputRecord(
        name=_text(item.get("name"), "inputs[].name"),
        origin=origin,
        producer=_optional_text(item.get("producer"), "inputs[].producer"),
        output=_optional_text(item.get("output"), "inputs[].output"),
        producer_run_id=_optional_text(item.get("producer_run_id"), "inputs[].producer_run_id"),
        producer_run_dir=None if where is None else run_root / where,
        generation=_optional_text(item.get("generation"), "inputs[].generation"),
        sources=tuple(sources),
    )


def _decode_metric_info(value: Any, metrics: Mapping[str, Any]) -> dict[str, MetricInfo]:
    from .flow.metrics import non_finite_in, value_fits

    info: dict[str, MetricInfo] = {}
    for path, entry in _mapping(value, "metric_info").items():
        item = _mapping(entry, f"metric_info.{path}")
        metric_type = item.get("type")
        if metric_type not in _METRIC_TYPES:
            raise _Malformed(f"`metric_info.{path}.type` is not a metric type")
        unit = _optional_text(item.get("unit"), f"metric_info.{path}.unit")
        node: Any = metrics
        for segment in path.split("."):
            if not isinstance(node, Mapping) or segment not in node:
                raise _Malformed(f"`metric_info` lists `{path}`, and `metrics` has no such value")
            node = node[segment]
        if not value_fits(metric_type, node) or non_finite_in(node) is not None:
            raise _Malformed(f"`metrics.{path}` is not a finite value of the type {metric_type}")
        info[path] = MetricInfo(
            path,
            _text(item.get("description"), f"metric_info.{path}.description"),
            metric_type,
            unit,
        )
    return info


def _decode(document: Mapping[str, Any], run_dir: Path, run_root: Path) -> RunRecord:
    outcome = document.get("outcome")
    if outcome not in OUTCOMES:
        raise _Malformed(f"`outcome` is not one of {', '.join(OUTCOMES)}")
    raw_error = document.get("error")
    error: ErrorInfo | None = None
    if raw_error is not None:
        try:
            error = ErrorInfo.from_json(raw_error)
        except ValueError as problem:
            raise _Malformed(f"`error`: {problem}") from None
    if (outcome == "passed") != (error is None):
        raise _Malformed("`error` is there if and only if the outcome is not `passed`")
    metrics = _mapping(document.get("metrics"), "metrics")
    runtime = document.get("runtime")
    if (
        isinstance(runtime, bool)
        or not isinstance(runtime, (int, float))
        or (isinstance(runtime, float) and not math.isfinite(runtime))
    ):
        raise _Malformed("`runtime` is not a finite number")
    dependencies = []
    for entry in _sequence(document.get("dependencies"), "dependencies"):
        item = _mapping(entry, "dependencies[]")
        dependencies.append(
            {
                key: _optional_text(item.get(key), f"dependencies[].{key}")
                for key in ("repository", "reference", "commit", "design_file")
            }
        )
    from .flow.metrics import ARTIFACT_KEY

    artifacts = {}
    for key, name in _mapping(document.get("artifacts"), "artifacts").items():
        if not ARTIFACT_KEY.fullmatch(key):
            raise _Malformed(f"`artifacts.{key}` is not a valid artifact key")
        artifacts[key] = _file_name(name, f"artifacts.{key}")
    return RunRecord(
        schema_version=RECORD_SCHEMA_VERSION,
        run_id=_text(document.get("run_id"), "run_id"),
        xeda_version=_text(document.get("xeda_version"), "xeda_version"),
        flow=_text(document.get("flow"), "flow"),
        design=_text(document.get("design"), "design"),
        target=_optional_text(document.get("target"), "target"),
        identity=_text(document.get("identity"), "identity"),
        configuration=_mapping(document.get("configuration"), "configuration"),
        run_root=run_root,
        run_dir=run_dir,
        outcome=outcome,
        error=error,
        metrics=metrics,
        metric_info=_decode_metric_info(document.get("metric_info"), metrics),
        outputs={
            name: _decode_output(name, value)
            for name, value in _mapping(document.get("outputs"), "outputs").items()
        },
        artifacts=artifacts,
        settings=_mapping(document.get("settings"), "settings"),
        inputs=tuple(
            _decode_input(item, run_root) for item in _sequence(document.get("inputs"), "inputs")
        ),
        started=_moment(document.get("started"), "started"),
        finished=_moment(document.get("finished"), "finished"),
        runtime=runtime,
        inherited_variables=_texts(document.get("inherited_variables"), "inherited_variables"),
        dependencies=tuple(dependencies),
    )


def decode_record(document: Any, *, run_dir: Path, run_root: Path) -> RunRecord:
    """Return the record that the `results.json` document `document` holds.

    `run_dir` is the directory of the file, and `run_root` the run root it lies in. Neither is in
    the document. A field that this version does not know is ignored. A document without
    `schema_version` was written by an older Xeda, and a newer schema is unknown: both raise
    `RecordError` with the code `unsupported_schema`. A document of any other wrong form raises it
    with the code `corrupt_record`."""
    where = Path(run_dir) / RESULTS_FILE
    if not isinstance(document, Mapping):
        raise RecordError(
            f"{where} does not hold a record: its content is not an object",
            code="corrupt_record",
            path=where,
        )
    if "schema_version" not in document:
        raise RecordError(
            f"{where} has no `schema_version`: an older Xeda wrote it. Run the flow again to "
            "replace it",
            code="unsupported_schema",
            path=where,
        )
    version = document["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise RecordError(
            f"{where} does not hold a record: its `schema_version` is {version!r}",
            code="corrupt_record",
            path=where,
        )
    if version > RECORD_SCHEMA_VERSION:
        raise RecordError(
            f"{where} has the schema version {version}, and this Xeda reads up to "
            f"{RECORD_SCHEMA_VERSION}: a newer Xeda wrote it",
            code="unsupported_schema",
            path=where,
        )
    try:
        return _decode(document, Path(run_dir), Path(run_root))
    except _Malformed as problem:
        raise RecordError(
            f"{where} does not hold a record: {problem}", code="corrupt_record", path=where
        ) from None


def _read_document(run_dir: Path) -> Any:
    """Return the JSON value in the `results.json` of `run_dir`. Raise `RecordError` with the code
    `missing_record` if there is no file, and `corrupt_record` if it is no JSON."""
    where = Path(run_dir) / RESULTS_FILE
    try:
        text = where.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise RecordError(f"{where} does not exist", code="missing_record", path=where) from None
    except (OSError, UnicodeDecodeError) as problem:
        raise RecordError(
            f"{where} cannot be read: {problem}", code="corrupt_record", path=where
        ) from None
    try:
        return json.loads(text, parse_constant=_refuse_constant)
    except ValueError as problem:
        raise RecordError(
            f"{where} is not JSON: {problem}", code="corrupt_record", path=where
        ) from None


def read_record(run_dir: Path, *, run_root: Path) -> RunRecord:
    """Return the record in the `results.json` of the run directory `run_dir`.

    `run_root` is the run root that the directory lies in. Raise `RecordError` with the code
    `missing_record` if there is no file, `corrupt_record` if it cannot be read as JSON or does not
    hold a record, and `unsupported_schema` as `decode_record` does."""
    return decode_record(_read_document(run_dir), run_dir=Path(run_dir), run_root=Path(run_root))


def read_stored_record(run_dir: Path | str) -> RunRecord:
    """Return the record in the `results.json` of the run directory `run_dir`, whose run root is
    not known.

    A run directory is `<run root>/<design>[/<target>]/<flow>`, and the record names its design and
    target, so the run root follows from where the file is. The errors are those of `read_record`.
    A directory that does not lie in that layout holds no record that Xeda can place: it raises
    `RecordError` with the code `corrupt_record`."""
    where = Path(os.path.abspath(run_dir))
    document = _read_document(where)
    names = []
    if isinstance(document, Mapping):
        names = [document.get("design"), *([document["target"]] if document.get("target") else [])]
    above: Path | None = where.parent
    for name in reversed(names):
        above = above.parent if above is not None and above.name == name else None
    # A document that is no record raises its own error first.
    record = decode_record(document, run_dir=where, run_root=above or where.parent)
    if above is None:
        raise RecordError(
            f"{where / RESULTS_FILE} is not in a run directory of its design and target "
            "(`<run root>/<design>[/<target>]/<flow>`): its run root is unknown",
            code="corrupt_record",
            path=where / RESULTS_FILE,
        )
    return record


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"{name} is no JSON number")


def stored_outputs(record: RunRecord) -> dict[str, list[tuple[Path, str]]]:
    """Return the files that `record` holds for each output, as `(path, digest)` pairs in order.

    Each path is the name joined to the run directory of the record. An output that the run did not
    write is not in the result. It is the one way to read the files of a stored run: the hand-over
    of a producer, the delivery and the dry run all use it."""
    return {
        name: [
            (record.run_dir / file, digest) for file, digest in zip(output.names, output.digests)
        ]
        for name, output in record.outputs.items()
        if output.names
    }


# --- The files of a stored run ----------------------------------------------------------------


def _generated_places(record: RunRecord) -> dict[str, tuple[str, Path | None, InputSource]]:
    """Return where each generated source that `record` read is, by its key in `OpenRun`.

    The value is the kind of the directory (`storage` or `run_dir`), the directory, and the source.
    The directory is None when the record does not name it. A converter's source is a file of the
    run directory of its node, which an input row that the node produced names. The source of a
    generator is a file of a generation storage, `<run root>/.generated/<design>/<key>`. A row
    holds one key, the one of the generator that wrote its first generated source, so the sources
    of another generator in that row have no place."""
    from .run_dir import RunDirectoryError

    nodes = {
        row.producer: row.producer_run_dir
        for row in record.inputs
        if row.origin == "producer" and row.producer_run_dir is not None
    }
    places: dict[str, tuple[str, Path | None, InputSource]] = {}
    for row in record.inputs:
        first = next((source for source in row.sources if source.generated), None)
        for source in row.sources:
            if not source.generated:
                continue
            key = source.path if source.producer is None else f"{source.producer}:{source.path}"
            if key in places:
                continue
            if source.producer in nodes:
                places[key] = ("run_dir", nodes[source.producer], source)
                continue
            directory = None
            if (
                row.generation is not None
                and first is not None
                and first.producer == source.producer
            ):
                design = record.design if source.producer is None else source.producer
                try:
                    directory = generation_storage(record.run_root, design, row.generation)
                except RunDirectoryError:
                    pass
            places[key] = ("storage", directory, source)
    return places


def _open_run(
    record: RunRecord, places: Mapping[str, tuple[str, Path | None, InputSource]]
) -> OpenRun:
    """Return the `OpenRun` of `record`. The caller holds the leases of every directory in
    `places`. A generated source is handed out when its file holds the bytes that the run read."""
    from .digest import content_digest

    generated: dict[str, Path] = {}
    missing: list[str] = []
    for key, (_kind, directory, source) in places.items():
        if directory is None:
            missing.append(key)
            continue
        try:
            if (
                name_in(directory, source.path) is not None
                and source.digest
                and content_digest(directory / source.path) == source.digest
            ):
                generated[key] = directory / source.path
                continue
        except (OSError, ValueError):
            pass
        missing.append(key)
    return OpenRun(
        record=record,
        run_dir=record.run_dir,
        outputs={
            name: tuple(record.run_dir / file for file in output.names)
            for name, output in record.outputs.items()
        },
        artifacts={key: record.run_dir / file for key, file in record.artifacts.items()},
        generated_sources=generated,
        missing_generated=tuple(missing),
    )


# --- From a flow that ran ----------------------------------------------------------------------


def _plain(value: Any) -> Any:
    """Return `value` as plain JSON data, the form a record holds and a document writes.

    A number that is not finite raises `ValueError`: the document never holds `NaN`."""
    from .utils import plain_json

    return plain_json(value)


def name_in(run_dir: Path, path: str | os.PathLike[str]) -> str | None:
    """Return the name of `path` relative to `run_dir`, in POSIX form, or None if it lies outside.

    The name is the place in the run directory as `path` spells it, whatever a link there leads
    to. A run directory that is reached through a link of its own can be spelled by its target as
    well: a path in that spelling is named too."""
    for base, target in (
        (os.path.abspath(run_dir), os.path.abspath(os.path.join(run_dir, os.fspath(path)))),
        (os.path.realpath(run_dir), os.path.abspath(os.path.join(run_dir, os.fspath(path)))),
    ):
        try:
            relative = os.path.relpath(target, base)
        except ValueError:
            continue
        if (
            relative == os.curdir
            or relative == os.pardir
            or relative.startswith(os.pardir + os.sep)
        ):
            continue
        return Path(relative).as_posix()
    return None


def artifact_problem(run_path: Path, key: object, value: Any) -> str | None:
    """Return why a flow cannot register `value` as the artifact `key`, or None if it can.

    An artifact is one file, under a key of `ARTIFACT_KEY`. A list, a table, a key off the grammar
    and a directory are not one. An empty value (`None`, `""`, `[]`) is no artifact. A path outside the run directory `run_path` or a file that is not
    there is no problem here: the record leaves it out."""
    from .artifacts import iter_artifact_paths
    from .flow.metrics import ARTIFACT_KEY

    if not value:  # `None`, `""`, `[]`: no artifact, in every mapping that holds it
        return None
    if not ARTIFACT_KEY.fullmatch(str(key)):
        return "the key is not lower snake case"
    leaves = list(iter_artifact_paths(value))
    if len(leaves) > 1 or isinstance(value, (list, tuple, Mapping)):
        return "it holds several files, and an artifact is one file"
    for leaf in leaves:
        name = name_in(run_path, leaf)
        located = run_path / (name or "")
        if name is not None and os.path.isdir(located) and not os.path.islink(located):
            return "it is a directory, and an artifact is one file"
    return None


def _artifacts_of(flow: Flow) -> dict[str, str]:
    """Return the artifacts that `flow` wrote inside its run directory: key to relative name.

    A path outside the run directory is no artifact of a record. Neither is a value that is no
    path, or a file that is not there. An artifact that `artifact_problem` finds a problem with is
    a bug of the flow, which the launcher has dropped before it asks for the record
    (`launch_run.drop_bad_artifacts`): `ValueError`."""
    from .artifacts import iter_artifact_paths

    artifacts: dict[str, str] = {}
    # The launcher copies `Flow.artifacts` into the results and drops what a failed run did not
    # write, so the results are the list to record.
    for key, value in (flow.results.get("artifacts") or {}).items():
        problem = artifact_problem(flow.run_path, key, value)
        if problem is not None:
            raise ValueError(f"`{flow.name}` registers the artifact {key!r}: {problem}")
        for leaf in iter_artifact_paths(value):
            name = name_in(flow.run_path, leaf)
            # A key is present only when its file was written. A link counts as itself.
            if name is not None and os.path.lexists(flow.run_path / name):
                artifacts[str(key)] = name
    return artifacts


def _outputs_of(flow: Flow) -> dict[str, OutputRecord]:
    """Return the record of every declared output of `flow`.

    The files come from `flow.results["outputs"]`, which `flow_runner.outputs.record_outputs`
    wrote with the checks of the run."""
    from .flow.io import declared_outputs, selected_types

    held = flow.results.get("outputs") or {}
    outputs: dict[str, OutputRecord] = {}
    for name, declaration in declared_outputs(type(flow)).items():
        recorded = held.get(name)
        names: list[str] = []
        digests: list[str] = []
        design: dict[str, Any] | None = None
        if declaration.is_design:
            kind = DESIGN_KIND
            if recorded is not None:
                sources = []
                for source in recorded["sources"]:
                    if "generated" in source:
                        file = name_in(flow.run_path, source["generated"])
                        assert file is not None, f"{source['generated']} lies outside the run"
                        names.append(file)
                        digests.append(source["sha"])
                        sources.append({"generated": file, "type": source["type"]})
                    else:
                        sources.append(dict(source))
                design = _plain(
                    {
                        "top": recorded["top"],
                        "input_design": recorded["input_design"],
                        "sources": sources,
                    }
                )
        else:
            types = selected_types(type(flow), flow.settings, name, output=True)
            kind = (types or declaration.types)[0].name
            for entry in (
                [] if recorded is None else (recorded if isinstance(recorded, list) else [recorded])
            ):
                file = name_in(flow.run_path, entry["path"])
                assert file is not None, f"{entry['path']} lies outside the run"
                names.append(file)
                digests.append(entry["sha"])
        outputs[name] = OutputRecord(
            name=name,
            kind=kind,
            cardinality=cast(Any, declaration.cardinality),
            names=tuple(names),
            digests=tuple(digests),
            design=design,
        )
    return outputs


_NO_OWNER: Any = object()


def _design_source(flow: Flow, path: Path) -> Any:
    """Return the source of the design of `flow` that is the file at `path`, or None."""
    for part in ("rtl", "tb"):
        for source in getattr(flow.design, part).sources:
            if source.file == path:
                return source
    return None


def _source_digest(flow: Flow, path: Path) -> str:
    """Return the content digest of the design source at `path`, or "" if it cannot be read."""
    from .digest import content_digest

    source = _design_source(flow, path)
    try:
        return cast(str, source.content_hash) if source is not None else content_digest(path)
    except (OSError, ValueError):
        return ""


def _generated_producer(flow: Flow, source: Any) -> str | None:
    """Return who wrote the generated `source` of the design: None for the design's own generator,
    else the name of the design whose generator wrote it (its owner)."""
    if source.owner is None:
        return None
    names = {unit.generation_label: unit.name for unit in flow.design.generation_units}
    return names[source.owner]


def _input_source(flow: Flow, path: str) -> InputSource:
    """Return the record of the design source at `path`, one that an input reads.

    A written source is named by its path from the design root. A generated source is named by
    its name in the output of its producer, so the record does not hold the place of a storage."""
    from .design import root_relative_text

    source = _design_source(flow, Path(path))
    generated = source is not None and source.generated
    dependency = None if source is None else source.dependency
    return InputSource(
        path=(
            cast(str, source.output_name)
            if generated
            else (
                source.dependency_place
                if dependency is not None
                else root_relative_text(flow.design.root, Path(path))
            )
        ),
        generated=generated,
        digest=_source_digest(flow, Path(path)),
        dependency=dependency,
        producer=_generated_producer(flow, source) if generated else None,
    )


def _inputs_of(flow: Flow, run_root: Path) -> list[InputRecord]:
    """Return where each declared input of `flow` came from, as one row for each binding.

    An input that producers provide has one row for each producer reference, in binding order,
    so an input of cardinality `many` that is bound to several producers repeats its name. Any
    other input has one row. The resolver gives an input either design sources or producer
    references, never both. A row without a producer holds the design sources that it reads. The
    row of a `design` input that a converter fills holds the sources that the converter
    generated."""
    from .flow.derived import generated_name
    from .flow.io import declared_inputs

    declarations = declared_inputs(type(flow))
    inputs: list[InputRecord] = []
    for declared in flow.declared_input_records:
        if declared.references:
            for reference in declared.references:
                producer_path = reference.producer_path
                # A converter hands over the sources it generated: the run read them.
                made = declarations[declared.name].is_design and producer_path is not None
                sources = tuple(
                    InputSource(
                        path=generated_name(Path(entry.path), Path(str(producer_path))),
                        generated=True,
                        digest=entry.sha,
                        producer=reference.producer,
                    )
                    for entry in (reference.entries if made else ())
                )
                inputs.append(
                    InputRecord(
                        name=declared.name,
                        origin=declared.origin,
                        producer=reference.producer,
                        output=reference.output,
                        producer_run_id=(
                            None
                            if producer_path is None
                            else flow.producer_run_ids.get(producer_path)
                        ),
                        producer_run_dir=(
                            None
                            if producer_path is None
                            else run_root / _run_dir_text(Path(producer_path), run_root)
                        ),
                        generation=None,
                        sources=sources,
                    )
                )
            continue
        sources = tuple(_input_source(flow, path) for path in declared.paths)
        # the key of the storage that wrote the first generated source that the input reads
        owner = next(
            (
                found.owner
                for path in declared.paths
                if (found := _design_source(flow, Path(path))) is not None and found.generated
            ),
            _NO_OWNER,
        )
        generation = None if owner is _NO_OWNER else flow.generation_keys.get(owner)
        inputs.append(
            InputRecord(
                name=declared.name,
                origin=declared.origin,
                producer=declared.producer,
                output=declared.output,
                producer_run_id=None,
                producer_run_dir=None,
                generation=generation,
                sources=sources,
            )
        )
    return inputs


def record_of(flow: Flow, *, run_root: Path, run_id: str, finished: datetime) -> RunRecord:
    """Return the record of the execution that `flow` just made.

    `run_root` is the run root of the launch, `run_id` the id of the execution, and `finished`
    the moment it ended. The record holds the metrics that the flow describes, and none other.
    A metric that is no metric (`collect_metrics`) is left out. Such a value is a failure of the
    node, and the launcher has recorded that before it asks for the record: if it has not, the
    record still tells the failure."""
    import time

    from . import __version__

    results = flow.results
    metrics, info, problems = collect_metrics(results, type(flow).results_description)
    outcome = flow.outcome or ("passed" if results.get("success") else "failed")
    error: ErrorInfo | None = None
    if problems and outcome in ("passed", "rejected"):
        outcome = "failed"
        error = ErrorInfo(
            code=INVALID_METRIC,
            message="; ".join(str(problem) for problem in problems),
            node=flow.name,
            exit_status=None,
            log=None,
        )
    elif outcome != "passed":
        try:
            error = ErrorInfo.from_json(results.get("error"))  # type: ignore[arg-type]
        except ValueError:
            error = ErrorInfo(
                code=INTERNAL_ERROR,
                message=f"`{flow.name}` did not pass and recorded no valid error",
                node=flow.name,
                exit_status=None,
                log=None,
            )
    artifacts = _artifacts_of(flow)
    if error is not None:
        # The log to read is the one of the child run that failed, if the flow knows one, else the
        # one of the tool that failed, else the one of the main tool.
        named = flow.failure_log_key()
        if named in artifacts:
            error = replace(error, log=named)
        elif error.log not in artifacts:
            error = replace(error, log="log" if "log" in artifacts else None)
    runtime = results.get("runtime")
    if runtime is None:
        runtime = 0.0 if flow.init_time is None else time.monotonic() - flow.init_time
    started = flow.started or finished
    return RunRecord(
        schema_version=RECORD_SCHEMA_VERSION,
        run_id=run_id,
        xeda_version=__version__,
        flow=flow.name,
        design=flow.design.name,
        target=flow.design.target,
        identity=flow.flow_hash or "",
        configuration=_plain(flow.configuration),
        run_root=run_root,
        run_dir=flow.run_path,
        outcome=Outcome(outcome),
        error=error,
        metrics=_plain(metrics),
        metric_info=info,
        outputs=_outputs_of(flow),
        artifacts=artifacts,
        settings=_plain(flow.settings),
        inputs=tuple(_inputs_of(flow, run_root)),
        started=started.astimezone(timezone.utc),
        finished=finished.astimezone(timezone.utc),
        runtime=float(runtime),
        inherited_variables=flow.inherited_variables,
        dependencies=tuple(d.as_mapping() for d in flow.design.resolved_dependencies),
    )
