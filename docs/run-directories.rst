******************************
Run directories and results
******************************

Every flow execution gets its own directory. Everything Xeda decided and everything the tools
produced is there, which makes a run reproducible and a failure diagnosable after the fact.

Where it goes
=============

The parent is ``./xeda_run`` by default; change it with ``--xeda-run-dir`` or the
``XEDA_RUN_DIR`` environment variable. ``--cwd`` runs directly in the current directory instead.

Within the parent, the layout depends on two options:

.. list-table::
   :header-rows: 1
   :widths: 46 54

   * - Options
     - Path
   * - *(default)*
     - ``<design>/<flow>/``
   * - ``--cached-dependencies``
     - ``<design>/<flow>_<flow_settings_hash>/``
   * - ``--cached-dependencies --no-incremental``
     - ``<design>_<design_hash>/<flow>_<flow_settings_hash>/``

Hashes appear only when ``--cached-dependencies`` is in effect, because that is when Xeda needs to
tell runs of different settings apart in order to reuse them.

``--incremental`` (the default) reuses one directory per flow, which is what you want while
iterating: incremental tool state survives between runs. ``--no-incremental`` backs up or removes
the existing directory first, so each run starts clean.

What is in it
=============

.. code-block:: text

    xeda_run/sqrt/vivado_synth/
      settings.json          the run's input settings, and what the flow made of them
      results.json           what was parsed back out of the reports
      vivado_synth.tcl       generated tool script
      clock.xdc              generated constraints
      vivado.log             the tool's own log
      reports/               reports the tool produced
      outputs/               netlists, bitstreams, and other deliverables
      checkpoints/           tool checkpoints, where the flow writes them

``settings.json``
-----------------

``flow_settings`` holds the run's *input*: the flow's defaults, the project's and design file's
``[flows.<flow>]`` sections and the command-line ``-s`` overrides, merged key by key. It is exactly
what the run is identified by, so it can be fed back to reproduce the run.
``effective_flow_settings`` holds what the flow made of them while preparing and executing the run
(resolved paths, derived options and generated outputs) -- the difference between what you asked
for and what was asked of the tool, and the first thing to read when a run did something you did
not expect. It also records the design as Xeda resolved it.

It records ``design_hash``, ``flowrun_hash`` and ``xeda_version`` too. Dependency caching compares
the two hashes; the version is diagnostic metadata and is not part of run identity. Both hashes
depend on what the inputs mean, not on where anything is: the design hash covers each source's
relative path/layout, ordered source contents and compilation metadata, plus behavior-affecting
RTL/testbench metadata, and ``flowrun_hash`` covers settings, with a path counted as its text
relative to the design (``$DESIGN_ROOT/c.xdc``). Moving a design together with its source layout
keeps those relative paths and therefore keeps the hashes; starting Xeda from another directory
does too. No file or directory a setting names is ever read, so a constraint file whose content
should decide reuse belongs among the design's sources.

``results.json``
----------------

What the flow parsed out of the tool's reports. Every flow reports:

.. list-table::
   :header-rows: 1
   :widths: 22 78

   * - Key
     - Meaning
   * - ``success``
     - Whether the flow succeeded. This decides the process exit status.
   * - ``runtime``
     - Wall-clock duration of the run, in fractional seconds.
   * - ``tools``
     - The executables invoked, with the versions Xeda detected.
   * - ``artifacts``
     - Files the flow produced, by name.
   * - ``design``, ``flow``
     - Names of what was run.
   * - ``design_hash``, ``flow_hash``
     - Hashes identifying the design and the effective settings.
   * - ``run_path``
     - Absolute path of this directory.
   * - ``timestamp``
     - When the run finished, as ``YYYY-MM-DD-HHMMSS``.

On top of that each flow reports its own keys - ``xeda list-results <flow>`` lists them. Keys
beginning with ``_`` are internal detail and may change without notice.

Reading results from a script
=============================

``xeda run --json`` writes the whole run as one JSON document to stdout, with tool output and log
messages on stderr, so no file-hunting is needed::

    xeda run vivado_synth sqrt.toml --json | jq '.results.Fmax'

The exit status is non-zero when the flow fails. The JSON document then carries an ``error``
object alongside any parsed results. See :doc:`machine-readable`.

Cleaning up
===========

.. list-table::
   :header-rows: 1
   :widths: 34 66

   * - Option
     - Effect
   * - ``--clean``
     - Empty the run directory before running.
   * - ``--post-cleanup``
     - After a run, keep only ``settings.json``, ``results.json`` and the artifacts.
   * - ``--post-cleanup-purge``
     - After a run, remove the run directory entirely.
   * - ``--scrub``
     - Before running, remove previous run directories of the same flow. Asks for confirmation.

``xeda scrub <flow> <design_name>`` removes previous run directories of one flow for one design,
without running anything.
