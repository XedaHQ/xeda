"""Walk the artifact trees shared by local cleanup and remote result collection.

Artifacts are paths, grouped by mappings, lists or tuples. Mapping keys are labels, never
paths; empty strings and non-path values are left alone. Walkers do not resolve paths: each
runner knows the run directory against which relative paths must be interpreted.
"""

import os
from collections.abc import Callable, Iterator, Mapping
from typing import Any

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
