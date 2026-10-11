"""The equivalence digest of a file: what its type calls the same.

Two files of one type can differ in a way that no consumer of some kinds cares about. A yosys JSON
netlist records, in the `src` attribute of each module, cell, net and memory, the file and line it
came from: a comment added above the logic moves every line number and nothing else. The
equivalence digest of such a file leaves that out.

The type defines the normalizer (`NORMALIZERS`, one entry per type); a consumer opts in
(`In(..., equivalence=True)`), because only the consumer knows whether it reads what the
normalizer drops. A producer records the digest of every output of a type that defines one,
whichever consumer asks. A file that does not parse has no digest, and a comparison without a
digest on both sides falls back to the content digest: a re-run, never a wrong reuse. Others are
added only with evidence from a real run that the difference they drop is all that varies.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .design import SourceType

__all__ = ["NORMALIZERS", "equivalence_digest", "has_normalizer"]


def _drop_source_location(node: Any) -> None:
    """Remove the `src` key of `node`'s `attributes` mapping, if it has one."""
    if isinstance(node, dict) and isinstance(node.get("attributes"), dict):
        node["attributes"].pop("src", None)


def json_netlist_digest(path: Path) -> str | None:
    """The digest of a yosys JSON netlist without the `src` attribute of its modules, cells, nets
    and memories, and nothing else: a port, net, cell or module that is named `src`, and a `src`
    key anywhere but in an `attributes` mapping of those four, are the design's. None for a file
    that is not a JSON object. The text is parsed and written back with sorted keys, so the
    digest does not depend on key order or spacing. The yosys version is in the file
    (`creator`): another yosys is another digest."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError, MemoryError):
        return None
    if not isinstance(document, dict):
        return None
    modules = document.get("modules")
    if isinstance(modules, dict):
        for module in modules.values():
            _drop_source_location(module)
            if isinstance(module, dict):
                for kind in ("cells", "netnames", "memories"):
                    objects = module.get(kind)
                    if isinstance(objects, dict):
                        for item in objects.values():
                            _drop_source_location(item)
    text = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha3_256(text.encode("ascii")).hexdigest()[:32]


#: the normalizer of each type that defines one: a path to its digest, or None
NORMALIZERS: dict[SourceType, Callable[[Path], str | None]] = {
    SourceType.JsonNetlist: json_netlist_digest,
}


def has_normalizer(types: Sequence[SourceType]) -> bool:
    """Whether every one of `types` defines the same normalizer (and there is one)."""
    normalizers = {NORMALIZERS.get(t) for t in types}
    return bool(types) and None not in normalizers and len(normalizers) == 1


def equivalence_digest(path: Path, types: Sequence[SourceType]) -> str | None:
    """The equivalence digest of the file at `path`, an output or input of `types`; None if the
    types define none or the file cannot be normalized."""
    if not has_normalizer(types):
        return None
    normalizer = NORMALIZERS[types[0]]
    return normalizer(path)
