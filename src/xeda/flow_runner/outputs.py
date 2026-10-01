"""Declared outputs, recorded with their integrity and handed to the flows that read them.

Recording checks containment and current-run writes before hashing each enabled output.
Hand-over checks the recorded shape, containment and content under the producer's held read
lease. It never uses wrote_output: a reused producer's trace vouches for its recorded run.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..digest import content_digest
from ..flow import Flow, FlowDependencyFailure
from ..flow.io import declared_outputs, output_enabled
from ..run_dir import RunDirectoryError

__all__ = ["handed_over", "record_outputs", "declared_output_files"]


def declared_output_files(results: dict[str, Any]) -> list[Path]:
    """Only path leaves of output integrity records, for retention and delivery."""
    files = []
    for recorded in (results.get("outputs") or {}).values():
        for entry in recorded if isinstance(recorded, list) else [recorded]:
            if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                files.append(Path(entry["path"]))
    return files


def record_outputs(flow: Flow) -> list[str]:
    """Record enabled declared outputs after a successful run, returning useful problems.

    Required scalar/list outputs and switched-on optional outputs must be present. Only
    complete output groups are recorded; disabled outputs are ignored even if assigned.
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
        many = declaration.cardinality == "many"
        if value is not None and (
            (many and (not isinstance(value, list) or not all(isinstance(p, Path) for p in value)))
            or (not many and not isinstance(value, Path))
        ):
            problems.append(f"{flow.name}'s output `{name}` has an invalid value")
            continue
        paths = [] if value is None else (value if many else [value])
        needed = declaration.cardinality in ("one", "many") or declaration.enabled_by is not None
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
                digest = content_digest(located)
            except RunDirectoryError:
                problems.append(f"{label} is not inside its run directory {flow.run_path}")
                continue
            except (OSError, ValueError, RuntimeError) as error:
                problems.append(f"{label} cannot be read: {error}")
                continue
            entries.append({"path": str(located), "sha": digest})
        if len(entries) == len(paths):
            records[name] = entries if many else entries[0]
    flow.results["outputs"] = records
    return problems


def handed_over(producer: Flow, output: str) -> list[Path]:
    """Validate a recorded output under its producer's held read lease.

    The returned paths preserve recorded list order and may name internal symbolic links.
    Missing, malformed, unreadable or changed files raise FlowDependencyFailure.
    """
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
    label = f"{producer.name}'s output `{output}`"
    many = declaration.cardinality == "many"
    if isinstance(recorded, list) != many or (many and not recorded):
        raise FlowDependencyFailure(f"{label} has an invalid record")
    paths: list[Path] = []
    for entry in recorded if many else [recorded]:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("path"), str)
            or not isinstance(entry.get("sha"), str)
            or re.fullmatch(r"[0-9a-f]{32}", entry["sha"]) is None
        ):
            raise FlowDependencyFailure(f"{label} has an invalid record")
        path = Path(entry["path"])
        try:
            if (
                not path.is_absolute()
                or not producer.run_directory.holds(path)
                or not path.resolve().is_relative_to(producer.run_directory.path)
                or not path.is_file()
            ):
                raise FlowDependencyFailure(f"{label} is missing or outside its run directory")
            digest = content_digest(path)
        except (OSError, ValueError, RuntimeError) as error:
            raise FlowDependencyFailure(f"{label} ({path}) cannot be read: {error}") from error
        if digest != entry["sha"]:
            raise FlowDependencyFailure(
                f"{label} ({path}) changed since its run recorded it -- another xeda process "
                "may have rebuilt or cleaned it: run again"
            )
        paths.append(path)
    return paths
