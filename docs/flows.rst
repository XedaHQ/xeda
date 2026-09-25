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
2. a ``xedaproject.toml``'s ``flows.<flow_name>`` section
3. the design file's ``[flows.<flow_name>]`` section
4. the command line, via ``-s``/``--settings``

They merge key by key: ``-s yosys.flatten=true`` changes that one setting of a ``yosys`` section
given in the design file, rather than replacing the whole section.

Command-line settings take dotted keys for nested values, and several can be given at once::

    xeda run vivado_synth sqrt.toml -s clock.period=4.5 synth.strategy=Flow_PerfOptimized_high

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

Settings a flow shares with its dependency -- ``fpga``, ``clocks`` and ``board`` for the FPGA
flows -- are resolved when the dependency is launched: the flow's own value is used for both, and
if the flow leaves a shared setting unset, the value given in the dependency's nested settings is
used instead.

With ``--cached-dependencies``, a dependency whose recorded design and settings hashes match a
previous successful run is skipped and its results reused.

Open-source FPGA flow targets and tuning
========================================

``yosys_fpga`` uses the target's Yosys synthesis pass. It supports ECP5, iCE40, Nexus,
Xilinx and Gowin synthesis. ``nextpnr`` adds verified device selection, constraints and output
formats for ECP5 (Trellis ``textcfg``), iCE40 (``asc``) and Nexus (``fasm``). The
``openfpgaloader`` chain packs and programs ECP5 with ``ecppack`` and iCE40 with ``icepack``.
It rejects families without a verified bitstream packer. For Xilinx 7-series placement and
routing, use the separate ``openxc7`` flow.

The default FPGA synthesis keeps ABC9 and the device pass's DSP and memory inference. The
``nowidelut`` restriction is off: it can reduce area or timing on one design and worsen the
other on another. iCE40 UltraPlus DSP and SPRAM inference are available as
``yosys.ice40_dsp`` and ``yosys.ice40_spram``. A supplied iCE40 part such as
``iCE40HX1K-TQ144`` selects the matching Yosys timing model and nextpnr device.

nextpnr uses each backend's own default timing-driven placer and router. Specify the actual
part, package, speed grade and timing constraints before comparing results. ``clock.period``
sets the target frequency; ``lpf_cfg`` (ECP5), ``pcf_cfg`` (iCE40), ``pdc_cfg`` (Nexus) and
``sdc`` accept files relative to the design root. A fixed ``seed`` makes comparisons
reproducible; try several fixed seeds when optimizing a particular design. The common
``placer``, ``router``, HeAP weights and hook settings allow measured experiments. Experimental
``tmg_ripup``, ``parallel_refine`` and iCE40 ``opt_timing`` stay opt-in. A faster or smaller
Yosys netlist alone does not establish an improvement in routed Fmax.

For example::

    xeda run nextpnr blinky.toml -s fpga.part=iCE40HX1K-TQ144 \
      -s clock.period=20 -s pcf_cfg=pins.pcf -s seed=2
    xeda run openfpgaloader blinky.toml -s write_flash=true -s verify=true

Use ``xeda list-settings yosys_fpga --json``, ``nextpnr --json`` or
``openfpgaloader --json`` to inspect all named settings. ``synth_flags``, ``extra_args`` and
``packer_args`` expose target/version-specific switches that do not have dedicated settings;
the selected installed tool must support those switches. Placement and programming are
different operations: ``openfpgaloader`` is the only flow here that writes hardware.

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
