*******************************************
Stability
*******************************************

From version 1.0.0, Xeda keeps the interfaces on this page stable. A change that breaks one of them
happens only in a major release, and it has no deprecation period. The same rules apply to the
next major release.

What is public
==============

- The Python modules ``xeda``, ``xeda.errors`` and ``xeda.introspect``: their names, signatures
  (parameter names, kinds, order, defaults and annotations), and documented fields and attributes.
- The command-line interface: the commands, the options and the exit statuses.
- The documents that Xeda writes and reads (see :doc:`machine-readable`): ``results.json``,
  the output of ``xeda run --json``, and the other JSON documents of the commands.
- The built-in flows: their names, the names, types and defaults of their settings, the names,
  kinds and cardinality of their outputs, the paths, types and units of their metrics, and their
  artifact keys.
- The error codes.
- The order of the settings layers, the grammars of the specification text, the names and keys of
  the private files and the session variables, and the layout of the run directories (see
  :doc:`run-directories`).

What is not public
==================

Everything else may change in any release. This includes ``settings.json``, ``trace.json``, hash
values, the text of logs, the text forms of objects, and the API for writing flows.

Additive and breaking changes
=============================

These changes can happen in any release:

- a new public name, method, optional field, or keyword-only parameter with a default;
- a new error subclass;
- a new member of an open set (see below);
- a new built-in flow, or a new setting with a default, output, metric or artifact of a flow;
- acceptance of a syntax that is reserved now (``,`` and ``@`` in specifications).

These changes happen only in a major release:

- removing or renaming a public item;
- changing the type, unit, default, cardinality, positional order or meaning of a public item;
- adding a member to a closed set.

Closed and open sets
====================

A closed set has a fixed list of members. A reader of an older version cannot judge a record that
holds a new member, so a new member is a breaking change. The closed sets are ``Outcome``,
``Disposition``, the ``PlanNode.state`` values, ``MetricInfo.type`` and
``OutputRecord.cardinality``.

An open set can get new members in any release. Keep an unknown value as text. The open sets are
the error codes and ``Event.kind``.

Extension points
================

The first release fixes the interfaces in the left columns, so that the additions in the right
column are not breaking changes.

.. list-table::
   :header-rows: 1
   :widths: 20 45 35

   * - Later feature
     - What the release fixes
     - The addition
   * - User flows
     - ``Spec.flow`` is text that Xeda parses after the design. The name grammar is fixed. No public
       name exposes a flow class. Design and project files refuse unknown keys.
     - a ``python`` key, ``introspect`` functions that take ``design=``, and ``xeda.authoring``
       (provisional)
   * - Several roots
     - ``,`` is reserved. Settings and metric paths are scoped to a node. ``Result.roots`` is a
       tuple. The aggregate verdict covers all roots.
     - the accepted syntax; ``Result.record`` raises a new ``XedaError`` subclass for several roots
   * - Flow instances
     - ``@`` is reserved in chains and selectors.
     - ``flow@name`` selectors
   * - Typed metric quantities
     - ``MetricInfo.unit`` is in every record.
     - ``RunRecord.quantity(path)``
   * - Run history
     - ``run_id``, ``producer_run_id``, ``RunReplacedError``, and ``RunRecord.run_dir`` as the
       place where the record is stored now
     - ``Workspace(history=...)`` and ``Workspace.lookup(run_id)``
   * - Action chains
     - An action precedes another element only through a declared output.
     - a programming receipt output of ``openfpgaloader`` and a tester flow that reads it
   * - Report channel
     - Reports are artifacts. Artifact keys and their conditions are documented and fixed for each
       flow. A record lists the artifacts that a run wrote.
     - a flow declares the reports that it reads, each by producer node and artifact key
   * - DSE strategies
     - the internal optimizer loop over ``sweep``
     - ``Workspace.explore(...)`` and a module-level ``explore``
   * - Remote attach and cancel
     - ``Remote(keep_running=True)`` and ``remote_detached``
     - ``Workspace.attach(launch_id)``
   * - Scrub of dependency storage
     - No scrub removes ``.dependencies/``.
     - a scrub option for it
   * - Remote user flows
     - The refusal is a ``SpecError``.
     - lifting the refusal
   * - Session variables that a flow reads
     - the fixed list of session variables
     - a flow declares the session variables that its tools read; they join its freshness evidence
   * - Async
     - The synchronous calls are blocking, thread-safe and cancelable.
     - an async wrapper that bridges task cancellation to ``CancelToken``
   * - Quantity objects as input
     - Quantities are text with a unit.
     - acceptance of a public quantity type

How breaks are detected
=======================

The test suite compares four snapshots with the release baseline in
``tests/baselines/<major>.<minor>/``: the public Python API; the built-in flows and the error
codes; the name grammars, the private files, the session variables, the length of the run hash and
the CLI; and the JSON Schema of each document that Xeda writes or reads (:ref:`document-schemas`).
For a document that Xeda writes, a removed key, a changed type, any change of an enum or a key that
is no longer required is a break, and a new optional key is an addition. For the design file, which Xeda reads, a
narrower type or enum, a removed key and a new required key are breaks, and a new optional key or a
wider type is an addition. Nobody rewrites a baseline after its release. A breaking difference
fails a test. Their own tests check the other rules on this page, such as the order of the settings layers
and the exit statuses.

For an additive change, regenerate the snapshots in ``tests/snapshots/current/`` and the shipped
schemas in ``src/xeda/data/schemas/``, and commit them:

.. code-block:: bash

    python tools/api_baseline.py

For a major release, write the new baseline and the report of the breaking changes:

.. code-block:: bash

    python tools/api_baseline.py --release X.0 --accept-breaking
