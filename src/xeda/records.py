"""The values that a launch records and a caller reads.

A run leaves one record, `results.json`: a `RunRecord`. This module owns its schema. `record_of`
builds a record from a flow that ran, `encode_record` makes its document, and `decode_record` and
`read_record` read it back. Every reader of a stored run goes through them.

The module imports no flow module when it loads, so a reader needs nothing of the flows.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Final, Literal, cast

from .errors import RecordError
from .frozen import freeze, thaw

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
    "OutputRecord",
    "RunRecord",
    "collect_metrics",
    "decode_record",
    "encode_record",
    "name_in",
    "nest_metrics",
    "read_record",
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
    generated (`generated`, a name in `names`) or a source of the input design that passes through (`source`, its index, and `file`).
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
    the generator. `dependency` is the index of the dependency in `RunRecord.dependencies`, or
    `None`."""

    path: str
    generated: bool
    digest: str
    dependency: int | None = None


@dataclass(frozen=True)
class InputRecord:
    """Where one declared input of a run came from.

    `producer_run_dir` is absolute: the run root of the record joined with the relative text that
    the document holds."""

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
    outcome: Literal["passed", "rejected", "failed", "canceled"]
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
        return self.outcome == "passed"

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
        "outcome": record.outcome,
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
    if not name or path.is_absolute() or ".." in path.parts or "\\" in name:
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
    design = item.get("design")
    if design is not None and kind != DESIGN_KIND:
        raise _Malformed(f"`{label}.design` is there only if the kind is `{DESIGN_KIND}`")
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
    artifacts = {
        key: _file_name(name, f"artifacts.{key}")
        for key, name in _mapping(document.get("artifacts"), "artifacts").items()
    }
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


def read_record(run_dir: Path, *, run_root: Path) -> RunRecord:
    """Return the record in the `results.json` of the run directory `run_dir`.

    `run_root` is the run root that the directory lies in. Raise `RecordError` with the code
    `missing_record` if there is no file, `corrupt_record` if it cannot be read as JSON or does not
    hold a record, and `unsupported_schema` as `decode_record` does."""
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
        document = json.loads(text, parse_constant=_refuse_constant)
    except ValueError as problem:
        raise RecordError(
            f"{where} is not JSON: {problem}", code="corrupt_record", path=where
        ) from None
    return decode_record(document, run_dir=Path(run_dir), run_root=Path(run_root))


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


# --- From a flow that ran ----------------------------------------------------------------------


def _plain(value: Any) -> Any:
    """Return `value` as plain JSON data, the form a record holds and a document writes.

    A number that is not finite raises `ValueError`: the document never holds `NaN`."""
    from .utils import json_encodable, with_json_keys

    return json.loads(json.dumps(with_json_keys(value), default=json_encodable, allow_nan=False))


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


def _artifacts_of(flow: Flow) -> dict[str, str]:
    """Return the artifacts that `flow` wrote inside its run directory: key to relative name.

    A key that holds one path keeps its name. A key that holds several paths (a list, or a group)
    gets one key per path: `key[0]`, `key[1]`. A path outside the run directory is no artifact
    of a record, and neither is a value that is no path."""
    from .artifacts import iter_artifact_paths

    artifacts: dict[str, str] = {}
    # The launcher copies `Flow.artifacts` into the results and drops what a failed run did not
    # write, so the results are the list to record.
    for key, value in (flow.results.get("artifacts") or {}).items():
        leaves = list(iter_artifact_paths(value))
        grouped = isinstance(value, (list, tuple, Mapping)) or len(leaves) > 1
        for index, leaf in enumerate(leaves):
            name = name_in(flow.run_path, leaf)
            if name is not None:
                artifacts[f"{key}[{index}]" if grouped else str(key)] = name
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


def _source_digest(flow: Flow, path: Path) -> str:
    """Return the content digest of the design source at `path`, or "" if it cannot be read."""
    from .digest import content_digest

    for part in ("rtl", "tb"):
        for source in getattr(flow.design, part).sources:
            if source.file == path:
                try:
                    return cast(str, source.content_hash)
                except (OSError, ValueError):
                    return ""
    try:
        return content_digest(path)
    except (OSError, ValueError):
        return ""


def _inputs_of(flow: Flow, run_root: Path) -> list[InputRecord]:
    """Return where each declared input of `flow` came from."""
    from .design import root_relative_text

    inputs: list[InputRecord] = []
    for declared in flow.declared_input_records:
        reference = declared.references[0] if declared.references else None
        handed = {entry.path for ref in declared.references for entry in ref.entries}
        sources = tuple(
            InputSource(
                path=root_relative_text(flow.design.root_path, Path(path)),
                generated=False,
                digest=_source_digest(flow, Path(path)),
                dependency=None,
            )
            for path in declared.paths
            if path not in handed
        )
        producer_path = None if reference is None else reference.producer_path
        inputs.append(
            InputRecord(
                name=declared.name,
                origin=declared.origin,
                producer=declared.producer if reference is None else reference.producer,
                output=declared.output if reference is None else reference.output,
                producer_run_id=(
                    None if producer_path is None else flow.producer_run_ids.get(producer_path)
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
    return inputs


def _configuration_of(flow: Flow) -> dict[str, Any]:
    """Return what `flow` read of its design: the parts of `Flow.design_parts` (see `RunRecord`).

    Each part has its metadata and its sources as `{path, type}`, without content. A path is
    relative to the design root, and a source that a converter generated is named by its place in
    the output of the converter."""
    from .design import root_relative_text

    design = flow.design
    scope = type(flow).design_scope(flow.settings)
    configuration: dict[str, Any] = {}
    for part in ("rtl", "tb"):
        if part not in flow.design_parts:
            continue
        settings = getattr(design, part)
        entry: dict[str, Any] = {
            "top": settings.top,
            "parameters": design._parameters_fingerprint(settings),  # pylint: disable=W0212
            "defines": dict(settings.defines),
        }
        if part == "rtl":
            entry["clocks"] = [clock.model_dump() for clock in settings.clocks]
            entry["attributes"] = settings.attributes
        else:
            entry["uut"] = settings.uut
            entry["cocotb"] = None if settings.cocotb is None else settings.cocotb.model_dump()
        entry["sources"] = [
            {
                "path": (
                    source.generated_name
                    if source.generated_name is not None
                    else root_relative_text(design.root_path, source.file)
                ),
                "type": source.type.name,
            }
            for source in settings.sources
            if scope is None or source.type in scope
        ]
        configuration[part] = entry
    if "rtl" in flow.design_parts:
        configuration["language"] = design.language.model_dump()
    return cast(dict[str, Any], _plain(configuration))


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
        configuration=_configuration_of(flow),
        run_root=run_root,
        run_dir=flow.run_path,
        outcome=cast(Any, outcome),
        error=error,
        metrics=_plain(metrics),
        metric_info=info,
        outputs=_outputs_of(flow),
        artifacts=_artifacts_of(flow),
        settings=_plain(flow.settings),
        inputs=tuple(_inputs_of(flow, run_root)),
        started=started.astimezone(timezone.utc),
        finished=finished.astimezone(timezone.utc),
        runtime=float(runtime),
        inherited_variables=(),
        dependencies=(),
    )
