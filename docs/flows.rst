*****
Flows
*****

A flow is a named sequence of steps performed by one or more tools. ``xeda list-flows`` is the
authoritative list for your installed version; this page explains how flows are organized and how
to find your way around one.

Discovering flows and their settings
====================================

.. code-block:: bash

    xeda list-flows                     # name, aliases, category, description, dependencies
    xeda list-settings <flow>           # every setting: name, type, default, meaning
    xeda list-results <flow>            # every key the flow writes to results.json

``list-flows`` reports a ``category`` derived from the flow's base class:

``simulation``
    Runs a testbench. Adds settings for ``stop_time``, waveform output, and cocotb.

``fpga_synthesis``
    Targets an FPGA. Adds an ``fpga`` setting (a part identifier, or a ``board`` name that fills
    it in) on top of the clock settings.

``asic_synthesis``
    Targets a standard-cell process. Adds a ``platform`` setting naming a PDK.

``synthesis``
    Synthesis that is neither specifically FPGA nor ASIC.

Aliases are folded into their flow rather than listed separately, so ``ghdl`` appears as an alias
of ``ghdl_sim`` rather than as a flow of its own. Either name works everywhere.

Names are forgiving: ``vivado_synth``, ``vivado-synth`` and ``VivadoSynth`` all resolve to the
same flow. An unrecognized name suggests close matches.

Settings
========

Every flow declares its own settings. They can be given in three places, in increasing order of
precedence:

1. the flow's own defaults
2. the design file's ``[flows.<flow_name>]`` section (or a ``xedaproject.toml``'s ``flows``
   section)
3. the command line, via ``-s``/``--settings``

Command-line settings take dotted keys for nested values, and several can be given at once::

    xeda run vivado_synth sqrt.toml -s clock_period=4.5 synth.strategy=Flow_PerfOptimized_high

Unknown settings are a hard error, not a warning. This is deliberate: a mistyped setting that was
silently ignored would produce a result that looks fine and is not what you asked for.

Settings shared by every flow
-----------------------------

Beyond the flow-specific ones, every flow accepts:

``ncpus`` (alias of ``nthreads``)
    Maximum number of threads the tools may use.

``dockerized`` / ``docker``
    Run the flow's tools from a container image instead of the local installation.

``clean``
    Remove the run directory's contents before running.

``quiet`` / ``verbose`` / ``debug``
    How much the flow and its tools say.

``redirect_stdout``
    Send tool output to files rather than the console.

``lib_paths``
    Additional HDL libraries, as ``(name, path)`` pairs.

``xeda list-settings <flow>`` lists these under *Common settings*; pass ``--no-common`` to see
only the flow-specific ones.

Dependencies between flows
==========================

A flow may declare that it needs another flow's output. ``xeda list-flows`` shows these under
*Depends on*, and the runner satisfies them automatically - you run the flow you want, not the
chain leading to it.

.. code-block:: text

    openfpgaloader  ->  nextpnr  ->  yosys_fpga
    vivado_power    ->  vivado_postsynth_sim  ->  vivado_synth
    openroad        ->  yosys

A dependency runs in a nested run directory, and its settings are reachable from the parent as a
nested settings key. For example, to change how ``nextpnr``'s synthesis dependency behaves while
running ``openfpgaloader``::

    xeda run openfpgaloader blinky.toml -s nextpnr.yosys.flatten=true

With ``--cached-dependencies``, a dependency whose recorded design and settings hashes match a
previous successful run is skipped and its results reused.

Results
=======

After the tools run, a flow parses their reports into ``results``, written to ``results.json`` in
the run directory. ``results.success`` decides the flow's - and the process's - exit status.

Every flow reports a common set of keys (``success``, ``runtime``, ``tools``, ``design``,
``flow``, ``run_path``, ``timestamp``, hashes). On top of that, each flow reports its own; run
``xeda list-results <flow>`` for the list.

Some quantities were historically reported under different names by different flows. The runner
now also records a canonical name alongside whatever the flow reported, so a script does not need
to know which flow produced the file:

.. list-table::
   :header-rows: 1

   * - Canonical key
     - Also reported as
   * - ``Fmax``
     - ``f_max``, ``maximum_frequency``
   * - ``lut``
     - ``LUT``
   * - ``ff``
     - ``FF``

The original keys are kept, so nothing that already reads ``results.json`` breaks.

.. note::
   ``clock_frequency`` is deliberately *not* aliased to ``Fmax``. It is the frequency that was
   *constrained*, not the maximum that was *achieved*.

Writing a new flow
==================

Concrete flows live in ``src/xeda/flows/<tool>/``. A flow subclasses one of ``Flow``, ``SimFlow``,
``SynthFlow``, ``FpgaSynthFlow`` or ``AsicSynthFlow`` and implements:

``init()`` (optional)
    Runs after construction, once settings and design are known. This is where dependencies are
    registered with ``self.add_dependency(...)``. It is separate from ``__init__`` so that a flow
    can decide its dependencies based on its effective settings.

``run()``
    Generates scripts (Jinja2 templates in a ``templates/`` directory next to the flow module) and
    invokes the tools.

``parse_reports()`` (optional)
    Populates ``self.results``. Helpers: ``parse_report_regex()``, ``parse_regex()``,
    ``parse_xml()``.

Registration is automatic: ``Flow.__init_subclass__`` derives the flow name from the class name
(``VivadoSynth`` -> ``vivado_synth``) and registers it, with any extra names listed in ``aliases``.
Add the class to both the import list and ``__all__`` in ``src/xeda/flows/__init__.py`` so it is
shipped and discoverable.

Two things a new flow owes its users, and which the test suite enforces:

* a docstring of its own - it is what ``xeda list-flows`` shows
* a ``description=`` on every settings field, and a ``results_description`` describing the keys it
  reports (or ``results_description = {}`` if it reports none)
