"""Converted designs: a flow that turns the sources of a design into sources of another language.

A converter flow (bsc turns Bluespec into Verilog) declares one design output. The output is a
value of the design kind (`xeda.flow.io.DESIGN`), not a file. It stands for the design that the
flow derives from its own design: the sources the flow generated take the place of the sources it
converted, every other source passes through, and everything else is carried.

- `DesignConversion` is the static declaration of a converter: which languages it converts and
  which it writes (`Flow.design_conversion`). Planning needs nothing else.
- `DerivedSources` is what a converter sets as its design output. It lists the sources of the
  derived design in order, as generated files and as indices into the sources of the design that
  the flow got (`Generated`, `Passed`).
- `planned_design` builds the design that a converter will derive, before it ran, from the
  declaration alone, with one placeholder for generated sources. `derive_design` builds the
  design from the record of a run, with the actual generated and pass-through sources. Both use
  one helper to construct the design from their source entries; the planned source list can differ
  from the recorded one.

The functions are pure. They never validate the design again, so the generator of a design does
not run twice.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..design import Design, DesignSource, SourceType

__all__ = [
    "DESIGN_RECORD_KIND",
    "DerivedSources",
    "DesignConversion",
    "Generated",
    "Passed",
    "derive_design",
    "derived_sources",
    "planned_design",
    "recorded_files",
]

#: The `kind` of the record of a design output in `results.json`.
DESIGN_RECORD_KIND = "design"


@dataclass(frozen=True)
class DesignConversion:
    """The languages a converter flow converts and writes.

    `transforms` are the languages of `rtl.sources` that the flow converts. `produces` are the
    languages of the sources it writes. Every other source of `rtl` passes through unchanged."""

    transforms: frozenset[SourceType]
    produces: frozenset[SourceType]

    def __post_init__(self) -> None:
        for name in ("transforms", "produces"):
            value = getattr(self, name)
            if (
                not isinstance(value, frozenset)
                or not value
                or not all(isinstance(member, SourceType) for member in value)
            ):
                raise TypeError(f"DesignConversion.{name} is a nonempty frozenset of SourceType")
        if self.transforms & self.produces:
            raise TypeError("a converter writes no language that it converts")

    @property
    def written_type(self) -> SourceType:
        """The language that the planned design lists for the generated source."""
        return min(self.produces, key=lambda member: member.name)

    def passes_through(self, source: DesignSource) -> bool:
        """Return whether `source` passes through the converter unchanged."""
        return source.type not in self.transforms


@dataclass(frozen=True)
class Generated:
    """A source that the converter wrote: its file and its language."""

    path: Path
    type: SourceType


@dataclass(frozen=True)
class Passed:
    """A source of the input design that passes through: its index in `rtl.sources`."""

    index: int


@dataclass(frozen=True)
class DerivedSources:
    """The `rtl` of a derived design: the top module and the ordered sources.

    Each source is `Generated` or `Passed`, and appears once."""

    top: str | None
    entries: tuple[Generated | Passed, ...]

    def __post_init__(self) -> None:
        entries = tuple(self.entries)
        object.__setattr__(self, "entries", entries)
        seen: set[Any] = set()
        for entry in entries:
            if not isinstance(entry, (Generated, Passed)):
                raise TypeError(f"a derived source is Generated or Passed, not {entry!r}")
            key = ("generated", entry.path) if isinstance(entry, Generated) else entry.index
            if key in seen:
                raise ValueError(f"a derived design lists {entry!r} twice")
            seen.add(key)


def _derived(design: Design, sources: DerivedSources, *, checked: bool) -> Design:
    """Return a copy of `design` whose `rtl` is `sources`, with no validation.

    A generated file is a `file` of the design when `checked` (it exists), and a `path` otherwise
    (it does not exist yet). The top module comes from `sources`. The parameters and the generator
    of `rtl` are the converter's input and not the derived design's, so they are empty. Every
    other part of the design is carried."""
    copy = design.model_copy(deep=True)
    listed = []
    for entry in sources.entries:
        if isinstance(entry, Passed):
            if not 0 <= entry.index < len(copy.rtl.sources):
                raise ValueError(
                    f"the derived design takes source {entry.index} of the design, which lists "
                    f"{len(copy.rtl.sources)}"
                )
            listed.append(copy.rtl.sources[entry.index])
        else:
            listed.append(
                DesignSource({("file" if checked else "path"): str(entry.path)}, entry.type)
            )
    copy.rtl = copy.rtl.model_copy(
        update={"sources": listed, "top": sources.top, "parameters": {}, "generator": None}
    )
    copy.rtl.invalidate_cached_properties()
    copy.invalidate_cached_properties()
    return copy


def planned_design(design: Design, conversion: DesignConversion, where: Path) -> Design:
    """Return the design that a converter will derive from `design`, for planning only.

    The generated source is `<top>.v` in `where`, the run directory of the converter, as a `path`
    that need not exist. The sources that pass through follow it in design order. The result
    serves the checks of a plan: the languages the consumer will get, its metadata and its
    clocks. It is no identity."""
    top = design.rtl.top or design.name
    entries: list[Generated | Passed] = [
        Generated(where / f"{top}.v", conversion.written_type),
        *(
            Passed(index)
            for index, source in enumerate(design.rtl.sources)
            if conversion.passes_through(source)
        ),
    ]
    return _derived(design, DerivedSources(design.rtl.top, tuple(entries)), checked=False)


def derived_sources(record: Mapping[str, Any]) -> DerivedSources:
    """Return the sources that a design record lists (`results.json`, `outputs.design`).

    The record has the shape that `flow_runner.outputs.recorded_entries` checks. A malformed
    entry raises a `ValueError`."""
    entries: list[Generated | Passed] = []
    try:
        for item in record["sources"]:
            if "generated" in item:
                entries.append(Generated(Path(item["generated"]), SourceType[item["type"]]))
            else:
                entries.append(Passed(item["source"]))
        return DerivedSources(record["top"], tuple(entries))
    except (KeyError, TypeError) as error:
        raise ValueError(f"a design record is malformed: {error!r}") from error


def derive_design(design: Design, record: Mapping[str, Any]) -> Design:
    """Return the design that a converter derived from `design`, from the record of its run.

    `record` is the verified record of the design output. Whether the converter ran now or an
    earlier run is reused, the same record gives the same design."""
    return _derived(design, derived_sources(record), checked=True)


def recorded_files(recorded: Any) -> list[tuple[dict[str, Any], str]]:
    """Return the file entries of one recorded output, each with the key that holds its path.

    A file output records `{"path": ..., "sha": ...}` (a list of them for a list output). A design
    output records the files it generated under `"generated"`, in its `sources`. A source that
    passed through is no file of the output."""
    found: list[tuple[dict[str, Any], str]] = []
    for item in recorded if isinstance(recorded, list) else [recorded]:
        if not isinstance(item, dict):
            continue
        if item.get("kind") == DESIGN_RECORD_KIND:
            sources = item.get("sources")
            found += [
                (source, "generated")
                for source in (sources if isinstance(sources, list) else [])
                if isinstance(source, dict) and isinstance(source.get("generated"), str)
            ]
        elif isinstance(item.get("path"), str):
            found.append((item, "path"))
    return found
