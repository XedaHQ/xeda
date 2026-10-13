"""The equivalence digest of a file: a digest that ignores differences its type calls irrelevant.

Two files of one type can differ in a way that some consumers do not care about. A yosys JSON
netlist records, in the `src` attribute of each module, cell, net and memory, the file and line it
came from. A comment added above the logic moves every line number and nothing else. The
equivalence digest of such a file leaves the `src` attributes out.

The type defines the normalizer (`NORMALIZERS`, one entry per type). A consumer opts in
(`In(..., equivalence=True)`), because only the consumer knows whether it reads what the
normalizer drops. A producer records the digest of every output of a type that defines one,
whichever consumer asks. A file that does not parse has no digest. A comparison without a digest
on both sides falls back to the content digest, which causes another run and never a wrong reuse.
A type gets a normalizer only with evidence from a real run that the difference it drops is all
that varies.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .design import SourceType
from .digest import content_digest

__all__ = ["NORMALIZERS", "digests", "equivalence_digest", "has_normalizer"]


def _drop_source_location(node: Any) -> None:
    """Remove the `src` key of `node`'s `attributes` mapping, if it has one."""
    if isinstance(node, dict) and isinstance(node.get("attributes"), dict):
        node["attributes"].pop("src", None)


def json_netlist_digest(data: bytes) -> str | None:
    """Return the digest of a yosys JSON netlist (`data`) without the `src` attributes.

    Only the `src` key of the `attributes` mapping of a module, cell, net or memory is dropped.
    A port, net, cell or module named `src`, and a `src` key anywhere else, belong to the design.
    Return None if `data` is not a JSON object. The text is parsed and written back with sorted
    keys, so the digest does not depend on key order or spacing. The file records the yosys
    version (`creator`), so another yosys gives another digest.
    """
    try:
        document = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError, MemoryError):
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


#: The normalizer of each type that defines one: the content of a file to its digest, or None.
NORMALIZERS: dict[SourceType, Callable[[bytes], str | None]] = {
    SourceType.JsonNetlist: json_netlist_digest,
}


def has_normalizer(types: Sequence[SourceType]) -> bool:
    """Return whether `types` is nonempty and every type in it defines the same normalizer."""
    normalizers = {NORMALIZERS.get(t) for t in types}
    return bool(types) and None not in normalizers and len(normalizers) == 1


def equivalence_digest(path: Path, types: Sequence[SourceType]) -> str | None:
    """Return the equivalence digest of the file at `path`, an output or input of `types`.

    Return None if the types define no normalizer or the file cannot be normalized.
    """
    if not has_normalizer(types):
        return None
    try:
        return NORMALIZERS[types[0]](path.read_bytes())
    except (OSError, MemoryError):
        return None


def digests(path: Path, types: Sequence[SourceType]) -> tuple[str, str | None]:
    """Return the content digest of the file at `path` (`digest.content_digest`) and its
    equivalence digest, both from one read of the file: two reads could see two files if it is
    edited between them.

    The file is read into memory only when `types` define a normalizer. Otherwise it is streamed,
    as for every file.
    """
    if not has_normalizer(types):
        return content_digest(path), None
    data = path.read_bytes()
    return hashlib.sha3_256(data).hexdigest()[:32], NORMALIZERS[types[0]](data)
