"""The digests of the code that a run depends on: Xeda's own and that of a plugin flow.

A trace holds both digests. A change to either one makes a run stale (`trace.check_trace`).

- `xeda_code_digest` covers every file of the installed `xeda` package: code, templates and
  bundled data. An editable install keeps one version string across edits, so this digest, and
  not the version, notices a change to a helper that a flow uses.
- `flow_code_digest` covers the code of a flow that is not part of Xeda (a plugin flow). It
  holds every Flow class of the MRO of the flow class that Xeda does not define.

A digest is of the code that this process imported (`code_epoch`): of the content of its files
when the process recorded them, and of nothing else, not of where the files lie. A name in a
digest is relative to the root of its code, so a moved tree has the same digest. Each call checks
the files on disk first, by their metadata, and raises `StaleRuntimeError` when one is not what the
process imported: the process runs other code than the files say, so no trace may name either.
Only a new process runs the new code. A launch computes each digest once (`CodeDigests`), and
checks the files again before it writes a trace or accepts a reuse.

The root of the code of a Flow class that is not part of Xeda is:

- The top-level package, for a class defined in a package: the directory with the `__init__.py`
  files from the module up. Every file under it counts (a helper module, a template, data).
- The module and the modules that it imports, for a class defined in a module that is not in a
  package. Its directory is not a root: it can be a design directory or a working directory with
  any number of unrelated files, and an edit of one of them must not make a run stale. The
  modules that count are those in the directory of the module that the module imports (the
  `import` statements in its source, found without running it), and so on for each module found.
  A package of the directory counts as a whole. The `templates` directory next to the module
  counts too.

An import that the source does not show (`importlib.import_module(name)`) is not found. Put such
a module in a package.

A Flow class without a source file on disk (defined in a notebook cell, in the interactive
interpreter or by `exec`) has no digest. `Flow.always_runs` returns a reason for it, so Xeda runs
it every time and computes no digest for it.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Type

from .. import code_epoch
from ..code_epoch import XEDA_PACKAGE, digest_pairs, root_files
from ..flow import Flow, FlowFatalError
from ..flow.source_file import class_label, find_source_file


def xeda_code_digest() -> str:
    """Return a digest of every file of the xeda package that this process imported.

    That covers code, templates and bundled data. It checks the files first
    (`code_epoch.verify`): a file that changed since the process imported it raises
    `StaleRuntimeError`."""
    return code_epoch.xeda_digest()


def _source_file(cls: type) -> Path:
    """Return the file that defines `cls`.

    A launch asks for the digest of a flow class only when `Flow.always_runs` found a source file
    for every class of it. So this raises `FlowFatalError` only for a direct call with a class
    that has none."""
    file, why = find_source_file(cls)
    if file is None:
        raise FlowFatalError(
            f"The flow class {class_label(cls)} has no source file on disk ({why}), so it has no "
            "code digest. A flow that has no source file must always run: an override of "
            "`Flow.always_runs` must call `super().always_runs()`."
        )
    return file


def plugin_classes(flow_class: Type[Flow]) -> list[type]:
    """Return the classes of the MRO of `flow_class` that are Flow classes outside Xeda.

    Raise `FlowFatalError` if the code of one of them cannot be found (`_source_file`)."""
    return [
        cls
        for cls in flow_class.__mro__
        if isinstance(cls, type)
        and issubclass(cls, Flow)
        and not _source_file(cls).is_relative_to(XEDA_PACKAGE)
    ]


def _named_files(cls: type) -> Iterable[tuple[str, Path]]:
    """Return the `(name, file)` pairs of the code of `cls`, a class outside Xeda.

    A name is relative to the root of the code, and for a package it starts with the name of the
    package."""
    return root_files(_source_file(cls))


def flow_code_digest(flow_class: Type[Flow]) -> str:
    """Return a digest of the code of `flow_class` outside Xeda, as this process imported it.

    The code of a class is found as the module docstring says. The digest is "" when the xeda
    package defines all the Flow classes of the MRO: `xeda_code_digest` covers that case. It
    raises `FlowFatalError` for a class that has no source file on disk (`Flow.always_runs` runs
    such a flow every time, so a launch never asks for its digest), and `StaleRuntimeError` when a
    file of its code changed since the process recorded it (`code_epoch.plugin_root`)."""
    pairs: list[tuple[str, str]] = []
    roots: list[code_epoch.CodeRoot] = []
    for cls in plugin_classes(flow_class):
        root = code_epoch.plugin_root(_source_file(cls))
        if root not in roots:  # classes of one tree count the tree once
            roots.append(root)
            pairs += root.verified_pairs()
    return digest_pairs(pairs) if pairs else ""


def check_code(flow_class: Type[Flow]) -> None:
    """Raise `StaleRuntimeError` if the code of Xeda or of `flow_class` on disk is not the code
    that this process imported.

    A launch calls this after it built a trace and before it writes it, and before it accepts a
    reuse: the digests that it computed at its start are those of the imported code, so they
    vouch for nothing that changed since."""
    xeda_code_digest()
    flow_code_digest(flow_class)


def code_fingerprint(flow_classes: Iterable[Type[Flow]]) -> dict[str, str]:
    """Return the digests of the code that this process imported: Xeda's (`xeda`) and that of
    each of `flow_classes` (by its flow name), after a check.

    Every name in the digests is relative to the root of its code, so two processes that
    imported the same code at other places have the same fingerprint."""
    return {
        "xeda": xeda_code_digest(),
        **{flow_class.name: flow_code_digest(flow_class) for flow_class in flow_classes},
    }


class CodeDigests:
    """The code digests of one launch.

    A launch needs the same digests for each of its flows. This computes each one once, at its
    first use, and keeps it for the launch. The digests are those of the imported code, so they do
    not change during the launch: a change of a file raises `StaleRuntimeError` instead."""

    def __init__(self) -> None:
        self._xeda: str | None = None
        self._flows: dict[Type[Flow], str] = {}

    def xeda(self) -> str:
        """Return `xeda_code_digest()`, computed once."""
        if self._xeda is None:
            self._xeda = xeda_code_digest()
        return self._xeda

    def flow(self, flow_class: Type[Flow]) -> str:
        """Return `flow_code_digest(flow_class)`, computed once."""
        if flow_class not in self._flows:
            self._flows[flow_class] = flow_code_digest(flow_class)
        return self._flows[flow_class]
