"""Walk the artifact trees shared by local cleanup and remote result collection.

Artifacts are paths, grouped by mappings, lists or tuples. Mapping keys are labels, never
paths; empty strings and non-path values are left alone. Walkers do not resolve paths: each
runner knows the run directory against which relative paths must be interpreted.
"""

import logging
import os
from collections.abc import Callable, Iterator, Mapping
from typing import Any

log = logging.getLogger(__name__)

ArtifactPath = str | os.PathLike[str]


def iter_artifact_paths(artifacts: Any) -> Iterator[ArtifactPath]:
    """Yield nonempty path leaves in artifact order, including repeated paths."""
    if isinstance(artifacts, Mapping):
        for value in artifacts.values():
            yield from iter_artifact_paths(value)
    elif isinstance(artifacts, (list, tuple)):
        for value in artifacts:
            yield from iter_artifact_paths(value)
    elif isinstance(artifacts, (str, os.PathLike)) and os.fspath(artifacts):
        yield artifacts


def map_artifact_paths(artifacts: Any, transform: Callable[[ArtifactPath], Any]) -> Any:
    """Copy an artifact tree, transforming paths while retaining keys and container shape."""
    if isinstance(artifacts, Mapping):
        return {key: map_artifact_paths(value, transform) for key, value in artifacts.items()}
    if isinstance(artifacts, list):
        return [map_artifact_paths(value, transform) for value in artifacts]
    if isinstance(artifacts, tuple):
        return tuple(map_artifact_paths(value, transform) for value in artifacts)
    if isinstance(artifacts, (str, os.PathLike)) and os.fspath(artifacts):
        return transform(artifacts)
    return artifacts


def filter_artifact_paths(artifacts: Any, keep: Callable[[ArtifactPath], bool]) -> Any:
    """Copy an artifact tree without the path leaves `keep` rejects, otherwise retaining keys and
    container shape. A label or group whose paths were all rejected goes with them; one that
    never held a path (`None`, `""`, `[]`, ...) is kept as it was. A rejected single path is
    `None`."""
    if isinstance(artifacts, Mapping):
        filtered = {key: filter_artifact_paths(value, keep) for key, value in artifacts.items()}
        return {
            key: value for key, value in filtered.items() if not _emptied(artifacts[key], value)
        }
    if isinstance(artifacts, (list, tuple)):
        kept = [
            new
            for old, new in ((value, filter_artifact_paths(value, keep)) for value in artifacts)
            if not _emptied(old, new)
        ]
        return kept if isinstance(artifacts, list) else tuple(kept)
    if isinstance(artifacts, (str, os.PathLike)) and os.fspath(artifacts):
        return artifacts if keep(artifacts) else None
    return artifacts


def _emptied(before: Any, after: Any) -> bool:
    """Whether filtering took every path from a value that had some."""
    return next(iter_artifact_paths(before), None) is not None and (
        next(iter_artifact_paths(after), None) is None
    )


def drop_unwritten_artifacts(
    artifacts: Mapping[str, Any], written: Callable[[ArtifactPath], bool], flow_name: str
) -> dict[str, Any]:
    """The artifacts of a failed run of `flow_name` without the paths it did not write (`written`
    tells), with one warning naming each dropped label and path.

    A flow records its outputs where it knows their names, usually before its tool runs, and a
    failed tool may never write them. Listed anyway, they send every consumer of the results
    after files that are not there: the remote runner's transfer raised on the first one, so a
    failed remote run crashed instead of reporting its failure. The launcher applies this to the
    runs it reports, and the remote runner to what a remote reports, which may be an older xeda.
    """
    checked: dict[str, bool] = {}

    def was_written(path: ArtifactPath) -> bool:
        name = os.fspath(path)
        if name not in checked:
            checked[name] = written(path)
        return checked[name]

    dropped = [
        f"{label}: {os.fspath(path)}"
        for label, value in artifacts.items()
        for path in iter_artifact_paths(value)
        if not was_written(path)
    ]
    if dropped:
        log.warning(
            "%s failed; dropping the artifacts it did not write: %s", flow_name, ", ".join(dropped)
        )
    return filter_artifact_paths(artifacts, was_written)
