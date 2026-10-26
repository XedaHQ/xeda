"""Declared outputs, recorded with their integrity and handed to the flows that read them.

Recording checks containment and writes by the current run before it hashes each enabled output.
Hand-over checks the recorded shape, containment and content under the held read lease of the
producer. It never uses `wrote_output`: the trace of a reused producer vouches for its recorded
run.

The design output of a converter flow (`xeda.flow.derived`) is recorded as one table that lists
the sources of the derived design: the files the flow generated, with their digests, and the
sources of the input design that pass through, by index. Hand-over checks the generated files as
it checks any output, and each pass-through entry against the design it is handed
(`handed_design`).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..design import DESIGN_PARTS, Design, SourceType, root_relative_text
from ..digest import content_digest
from ..equivalence import digests
from ..flow import Flow, FlowDependencyFailure
from ..flow.derived import (
    DESIGN_RECORD_KIND,
    DerivedSources,
    Passed,
    derive_design,
    recorded_files,
)
from ..flow.io import OutputDeclaration, declared_outputs, output_enabled, output_required
from ..frozen import thaw
from ..records import RunRecord
from ..run_dir import RunDirectory, RunDirectoryError
from .trace import OutputEntry

__all__ = [
    "verified_design",
    "handed_design",
    "handed_entries",
    "handed_over",
    "record_outputs",
    "recorded_entries",
    "recorded_outputs",
    "declared_output_files",
]


def declared_output_files(results: dict[str, Any]) -> list[Path]:
    """Return the paths of the output records in `results`, for retention and delivery."""
    return [
        Path(entry[key])
        for recorded in (results.get("outputs") or {}).values()
        for entry, key in recorded_files(recorded)
    ]


def recorded_outputs(
    record: RunRecord, equivalences: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Return the outputs that `record` holds, in the form that `recorded_entries` checks.

    A file output is a table `{path, sha}` (a list of them for a list output) with the absolute
    path of the file in the run directory of the record, and with the `equivalence` digest where
    the type has one. A design output is the table of `_design_entries`, with the absolute path of
    each generated file. An output that the run did not write is not in the result. The stored
    form (`RunRecord.outputs`) is read here and nowhere else.

    `equivalences` are the equivalence digests of the run (`Trace.output_equivalences`), by the name
    of the file in the run directory. The record does not hold them."""
    found: dict[str, Any] = {}
    for name, output in record.outputs.items():
        if output.design is not None:
            digests = iter(output.digests)
            sources = []
            for source in thaw(output.design["sources"]):
                if "generated" in source:
                    sources.append(
                        {
                            "generated": str(record.run_dir / source["generated"]),
                            "type": source["type"],
                            "sha": next(digests),
                        }
                    )
                else:
                    sources.append(source)
            found[name] = {
                "kind": DESIGN_RECORD_KIND,
                "top": output.design["top"],
                "input_design": output.design["input_design"],
                "sources": sources,
            }
        elif output.names:
            entries = []
            for path, digest in zip(output.names, output.digests):
                entry: dict[str, Any] = {"path": str(record.run_dir / path), "sha": digest}
                if equivalences and path in equivalences:
                    entry["equivalence"] = equivalences[path]
                entries.append(entry)
            found[name] = entries if output.cardinality == "many" else entries[0]
    return found


def record_outputs(flow: Flow, *, record: bool = True) -> list[str]:
    """Check the enabled declared outputs of a run, record them, and return the problems found.

    With `record=False` the function checks the outputs and returns the problems, and it records
    nothing: a rejected run hands over no output, but its outputs must still be valid.

    Required scalar and list outputs, and optional outputs that are switched on, must be
    present. Only complete output groups are recorded. Disabled outputs are ignored even if
    assigned.
    """
    declarations = declared_outputs(type(flow))
    if not declarations:
        return []
    problems: list[str] = []
    records: dict[str, Any] = {}
    for name, declaration in declarations.items():
        if not output_enabled(flow.settings, declaration):
            continue
        value = getattr(flow.outputs, name, None)
        if declaration.is_design:
            design_record = _design_record(flow, name, value, problems)
            if design_record is not None:
                records[name] = design_record
            continue
        many = declaration.cardinality == "many"
        if value is not None and (
            (many and (not isinstance(value, list) or not all(isinstance(p, Path) for p in value)))
            or (not many and not isinstance(value, Path))
        ):
            problems.append(f"{flow.name}'s output `{name}` has an invalid value")
            continue
        paths = [] if value is None else (value if many else [value])
        needed = output_required(type(flow), flow.settings, declaration)
        if not paths:
            if needed:
                problems.append(f"{flow.name} did not produce its output `{name}`")
            continue
        entries = []
        for path in paths:
            label = f"{flow.name}'s output `{name}` ({path})"
            try:
                located = flow.run_directory.inside(path)
                if not located.resolve().is_relative_to(flow.run_directory.path):
                    problems.append(f"{label} is not inside its run directory {flow.run_path}")
                    continue
                if not located.is_file() or not flow.wrote_output(located):
                    problems.append(f"{label} was not written by this run")
                    continue
                digest, equivalence = digests(located, declaration.types)
            except RunDirectoryError:
                problems.append(f"{label} is not inside its run directory {flow.run_path}")
                continue
            except (OSError, ValueError, RuntimeError) as error:
                problems.append(f"{label} cannot be read: {error}")
                continue
            entry = {"path": str(located), "sha": digest}
            if equivalence is not None:
                entry["equivalence"] = equivalence
            entries.append(entry)
        if len(entries) == len(paths):
            records[name] = entries if many else entries[0]
    if record:
        flow.results["outputs"] = records
    return problems


def _source_file(design: Design, index: int) -> str:
    """Return the path of source `index` of `design` relative to its root, as the record names it.

    The path may begin with `..` for a source outside the root. It is `posix` text, whatever the
    system. A source with no relative path (on another drive) is named by its absolute path."""
    return root_relative_text(design.root_path, design.rtl.sources[index].file)


def _design_record(flow: Flow, name: str, value: Any, problems: list[str]) -> dict[str, Any] | None:
    """Return the record of the design output `name`, or None after adding to `problems`.

    Each generated file must be inside the run directory and written by this run. Each source
    that passes through must be a source of the design the flow was given."""
    if value is None:
        problems.append(f"{flow.name} did not produce its output `{name}`")
        return None
    if not isinstance(value, DerivedSources):
        problems.append(f"{flow.name}'s output `{name}` has an invalid value")
        return None
    before = len(problems)
    listed = flow.design.rtl.sources
    sources: list[dict[str, Any]] = []
    for entry in value.entries:
        if isinstance(entry, Passed):
            if not 0 <= entry.index < len(listed):
                problems.append(
                    f"{flow.name}'s output `{name}` passes through source {entry.index}, and "
                    f"the design lists {len(listed)}"
                )
                continue
            sources.append(
                {
                    "source": entry.index,
                    "file": _source_file(flow.design, entry.index),
                    "type": listed[entry.index].type.name,
                }
            )
            continue
        label = f"{flow.name}'s output `{name}` ({entry.path})"
        try:
            located = flow.run_directory.inside(entry.path)
            if not located.resolve().is_relative_to(flow.run_directory.path):
                problems.append(f"{label} is not inside its run directory {flow.run_path}")
                continue
            if not located.is_file() or not flow.wrote_output(located):
                problems.append(f"{label} was not written by this run")
                continue
            sources.append(
                {
                    "generated": str(located),
                    "type": entry.type.name,
                    "sha": content_digest(located),
                }
            )
        except RunDirectoryError:
            problems.append(f"{label} is not inside its run directory {flow.run_path}")
        except (OSError, ValueError, RuntimeError) as error:
            problems.append(f"{label} cannot be read: {error}")
    if len(problems) > before:
        return None
    return {
        "kind": DESIGN_RECORD_KIND,
        "top": value.top,
        "input_design": flow.design.parts_hash(DESIGN_PARTS),
        "sources": sources,
    }


def _design_entries(recorded: Any, label: str) -> tuple[OutputEntry, ...]:
    """Return the generated files of a recorded design output, in order.

    The record is a table with the `kind` `design`, a `top` (text or none), the `input_design`
    hash and a list of `sources`. A source is a generated file (`generated`, `type`, `sha`) or a
    source of the input design that passes through (`source`, `file`, `type`). Otherwise a
    `FlowDependencyFailure` names `label`."""
    invalid = FlowDependencyFailure(f"{label} has an invalid record")
    if (
        not isinstance(recorded, dict)
        or set(recorded) != {"kind", "top", "input_design", "sources"}
        or recorded["kind"] != DESIGN_RECORD_KIND
        or not (recorded["top"] is None or isinstance(recorded["top"], str))
        or not isinstance(recorded["input_design"], str)
        or not isinstance(recorded["sources"], list)
    ):
        raise invalid
    types = {member.name for member in SourceType}
    entries = []
    seen: set[Any] = set()
    for item in recorded["sources"]:
        if not isinstance(item, dict) or item.get("type") not in types:
            raise invalid
        if set(item) == {"generated", "type", "sha"}:
            if (
                not isinstance(item["generated"], str)
                or not isinstance(item["sha"], str)
                or re.fullmatch(r"[0-9a-f]{32}", item["sha"]) is None
            ):
                raise invalid
            key: Any = ("generated", item["generated"])
            entries.append(OutputEntry(path=item["generated"], sha=item["sha"]))
        elif set(item) == {"source", "file", "type"}:
            if (
                not isinstance(item["source"], int)
                or isinstance(item["source"], bool)
                or item["source"] < 0
                or not isinstance(item["file"], str)
            ):
                raise invalid
            key = item["source"]
        else:
            raise invalid
        if key in seen:
            raise invalid
        seen.add(key)
    return tuple(entries)


def recorded_entries(
    recorded: Any, many: bool, label: str, *, design: bool = False
) -> tuple[OutputEntry, ...]:
    """Return the files of one output as `results.json` records them.

    The record must have the shape that the cardinality of the output gives it (a list for a
    list output, a table for any other), and each file must have a path and a digest.
    Otherwise a `FlowDependencyFailure` names `label`. This is the one rule of a recorded
    output, for the hand-over and for a dry run, which reads no file.

    With `design`, the output is a design (`Out(DESIGN, ...)`): the record is the table of
    `_design_entries`, and the files are the ones it generated."""
    if design:
        return _design_entries(recorded, label)
    if isinstance(recorded, list) != many or (many and not recorded):
        raise FlowDependencyFailure(f"{label} has an invalid record")
    entries = []
    for entry in recorded if many else [recorded]:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("path"), str)
            or not isinstance(entry.get("sha"), str)
            or re.fullmatch(r"[0-9a-f]{32}", entry["sha"]) is None
            or not (entry.get("equivalence") is None or isinstance(entry["equivalence"], str))
        ):
            raise FlowDependencyFailure(f"{label} has an invalid record")
        entries.append(
            OutputEntry(path=entry["path"], sha=entry["sha"], equivalence=entry.get("equivalence"))
        )
    return tuple(entries)


def handed_entries(producer: Flow, output: str) -> tuple[OutputEntry, ...]:
    """Return the files of the `output` of `producer` as its run recorded them.

    `handed_over` has verified them. They are what a consumer is handed, to be compared with
    what it consumed. Call this after `handed_over` has accepted the output."""
    recorded = (producer.results.get("outputs") or {})[output]
    declaration = declared_outputs(type(producer))[output]
    return recorded_entries(
        recorded,
        declaration.cardinality == "many",
        f"{producer.name}'s output `{output}`",
        design=declaration.is_design,
    )


def _recorded_output(producer: Flow, output: str) -> tuple[OutputDeclaration, Any]:
    """Return the declaration and the record of `output` of `producer`, which must have one."""
    declaration = declared_outputs(type(producer)).get(output)
    records = producer.results.get("outputs")
    recorded = records.get(output) if isinstance(records, dict) else None
    if (
        declaration is None
        or not producer.succeeded
        or not output_enabled(producer.settings, declaration)
        or recorded is None
    ):
        raise FlowDependencyFailure(
            f"{producer.name} recorded no output `{output}` in {producer.run_path}: "
            "run it again (--rebuild-all)"
        )
    return declaration, recorded


def _verified_files(
    run_directory: RunDirectory, entries: tuple[OutputEntry, ...], label: str
) -> list[Path]:
    """Check each recorded file, and return the paths.

    A file must be an absolute path inside the run directory (`run_directory`, the one of the
    producer, held under its read lease), exist, and have the digest the record gives it."""
    paths: list[Path] = []
    for entry in entries:
        path = Path(entry.path)
        try:
            if (
                not path.is_absolute()
                or not run_directory.holds(path)
                or not path.resolve().is_relative_to(run_directory.path)
                or not path.is_file()
            ):
                raise FlowDependencyFailure(f"{label} is missing or outside its run directory")
            digest = content_digest(path)
        except (OSError, ValueError, RuntimeError) as error:
            raise FlowDependencyFailure(f"{label} ({path}) cannot be read: {error}") from error
        if digest != entry.sha:
            raise FlowDependencyFailure(
                f"{label} ({path}) changed since its run recorded it. Another Xeda process "
                "may have rebuilt or cleaned it: run again"
            )
        paths.append(path)
    return paths


def handed_over(producer: Flow, output: str) -> list[Path]:
    """Validate a recorded output under the held read lease of its producer, and return its paths.

    The returned paths keep the recorded list order and may name internal symbolic links. A
    missing, malformed, unreadable or changed file raises `FlowDependencyFailure`. For a design
    output the files are the ones the producer generated (`handed_design` also checks the sources
    that pass through, against a design).
    """
    declaration, recorded = _recorded_output(producer, output)
    label = f"{producer.name}'s output `{output}`"
    entries = recorded_entries(
        recorded, declaration.cardinality == "many", label, design=declaration.is_design
    )
    return _verified_files(producer.run_directory, entries, label)


def verified_design(
    recorded: Any, run_directory: RunDirectory, label: str, design: Design
) -> tuple[Design, tuple[OutputEntry, ...]]:
    """Verify the record of a design output against `design`, and derive the design from it.

    This is the one rule of the hand-over (`handed_design`, under the read lease of the producer)
    and of the dry run, which reads the files and holds no lease. `run_directory` is the one of
    the producer. The record has the shape `recorded_entries` checks. The generated files are
    checked as `handed_over` checks them. Each source that passes through must still be the same
    file, of the same type, at its index in `design.rtl.sources`: a design that was edited or
    reordered since the run recorded it is a changed record. The `input_design` of the record is provenance: nothing compares it, since
    the trace of the producer decides whether its design changed. Return the derived design and
    the entries of the generated files, in order, which the consumer compares with what it
    consumed."""
    entries = recorded_entries(recorded, False, label, design=True)
    _verified_files(run_directory, entries, label)
    for item in recorded["sources"]:
        if "source" not in item:
            continue
        index = item["source"]
        if (
            index >= len(design.rtl.sources)
            or _source_file(design, index) != item["file"]
            or design.rtl.sources[index].type.name != item["type"]
        ):
            raise FlowDependencyFailure(
                f"{label} changed since its run recorded it: source {index} of the design "
                f"was {item['file']} ({item['type']}) and is not any more. Run again"
            )
    try:
        derived = derive_design(design, recorded, run_directory.path)
    except ValueError as error:
        raise FlowDependencyFailure(f"{label} cannot derive a design: {error}") from error
    return derived, entries


def handed_design(
    producer: Flow, output: str, design: Design
) -> tuple[Design, tuple[OutputEntry, ...]]:
    """Validate the design output of `producer` against `design`, and derive the design from it.

    `design` is the design the producer was given. The checks are those of `verified_design`,
    made under the held read lease of the producer."""
    declaration, recorded = _recorded_output(producer, output)
    label = f"{producer.name}'s output `{output}`"
    if not declaration.is_design:
        raise FlowDependencyFailure(f"{label} is no design")
    return verified_design(recorded, producer.run_directory, label, design)
