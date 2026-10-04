"""Shared safe YAML reader with YAML 1.2 core scalars and string mapping keys."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.error import Mark
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

_TAG = "tag:yaml.org,2002:"
_CORE_SCALARS = {
    "null": re.compile(r"^(?:null|Null|NULL|~|)$"),
    "bool": re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    # YAML 1.2.2, 10.3.2: only the base 10 form takes a sign (`[-+]? [0-9]+`); `0o` and `0x`
    # do not, so `-0x1F` is text -- as written, not an oversight.
    "int": re.compile(r"^(?:[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$"),
    "float": re.compile(
        r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?"
        r"|[-+]?(?:\.inf|\.Inf|\.INF)|(?:\.nan|\.NaN|\.NAN))$"
    ),
}


class _CoreLoader(yaml.SafeLoader):
    # Own both tables: registering this profile must never modify PyYAML's global loaders.
    yaml_implicit_resolvers: dict[Any, Any] = {}
    yaml_constructors = {
        _TAG + "str": yaml.SafeLoader.construct_yaml_str,
        None: yaml.SafeLoader.construct_undefined,
    }

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Any, Any]:
        if not isinstance(node, MappingNode):
            raise ConstructorError(None, None, "expected a mapping", node.start_mark)
        result = {}
        marks: dict[str, Mark] = {}
        for key_node, value_node in node.value:
            # Merge keys are outside the core schema. Quoted "<<" is an ordinary string key.
            if (
                isinstance(key_node, ScalarNode)
                and key_node.value == "<<"
                and (key_node.style is None or key_node.tag == _TAG + "merge")
            ):
                raise ConstructorError(
                    None, None, "YAML merge keys are not supported", key_node.start_mark
                )
            key = self.construct_object(key_node, deep=True)
            if not isinstance(key, str):
                raise ConstructorError(
                    None,
                    None,
                    "mapping keys must be strings; quote this key",
                    key_node.start_mark,
                )
            if key in result:
                raise ConstructorError(
                    f"first occurrence of key {key!r}",
                    marks[key],
                    f"duplicate mapping key {key!r}",
                    key_node.start_mark,
                )
            marks[key] = key_node.start_mark
            result[key] = self.construct_object(value_node, deep=True)
        return result


def _construct_core_scalar(loader: _CoreLoader, node: ScalarNode) -> Any:
    kind = node.tag.removeprefix(_TAG)
    text = loader.construct_scalar(node)
    if not _CORE_SCALARS[kind].fullmatch(text):
        raise ConstructorError(
            None, None, f"invalid YAML 1.2 core {kind} value {text!r}", node.start_mark
        )
    if kind == "int":
        if text.startswith("0o"):
            return int(text[2:], 8)
        if text.startswith("0x"):
            return int(text[2:], 16)
        return int(text, 10)
    if kind == "float":
        return float(text.lower().replace(".inf", "inf").replace(".nan", "nan"))
    if kind == "bool":
        return text.lower() == "true"
    return None


def _construct_sequence(loader: _CoreLoader, node: SequenceNode) -> list[Any]:
    # Immediate construction rejects recursive aliases; ordinary aliases are supported.
    return loader.construct_sequence(node, deep=True)


for _kind, _pattern in _CORE_SCALARS.items():
    _CoreLoader.add_implicit_resolver(_TAG + _kind, _pattern, None)
    _CoreLoader.add_constructor(_TAG + _kind, _construct_core_scalar)
_CoreLoader.add_constructor(_TAG + "map", _CoreLoader.construct_mapping)
_CoreLoader.add_constructor(_TAG + "seq", _construct_sequence)


def load_yaml(path: str | Path) -> Any:
    """Read one YAML document, retaining its filename in parser/constructor diagnostics.

    Scalars follow exactly the YAML 1.2 core schema (including decimal leading zeros,
    scientific floats and no timestamps). Every mapping key must be a string, and duplicate
    keys are errors naming both occurrences. Core explicit tags and nonrecursive aliases are
    supported; merge keys, recursive aliases and non-core tags are rejected. The caller
    validates the document's required shape, e.g. a design or project mapping.
    """
    with Path(path).open("rb") as stream:
        return yaml.load(stream, Loader=_CoreLoader)
