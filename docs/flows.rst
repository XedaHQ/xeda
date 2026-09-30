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

Every flow declares its own settings. They can be given in four places, in increasing order of
precedence:

1. the flow's own defaults
2. a ``xedaproject.toml``'s ``flows.<flow_name>`` section
3. the design file's ``[flows.<flow_name>]`` section
4. the command line, via ``-s``/``--settings``

They merge key by key: ``-s yosys.flatten=true`` changes that one setting of a ``yosys`` section
given in the design file, rather than replacing the whole section. Precedence goes by where a
setting was given first (project, design, command line, API); a nested section such as
``nextpnr``'s ``yosys`` refines the dependency's own ``[flows.yosys_fpga]`` section only within
one of these.

``-s flows.<flow>.<key>=<value>`` sets a setting of any flow in the run: the requested flow, or one
of its declared dependencies. ``-s flows.nextpnr.seed=2`` and ``-s seed=2`` are the same setting
when ``nextpnr`` is the requested flow; giving both different values is an error. A flow that is
not part of the run is an error too, with the close matches suggested.

``-s`` takes space-separated ``KEY=VALUE`` items. It ends at the next option, or at the first
token that is not ``KEY=VALUE``, so it never takes the design file for a setting; ``--`` ends the
options.

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

A dependency runs in its own run directory, a sibling of the flow that launched it, and its
settings are reachable from the parent as a nested settings key. For example, to change how
``nextpnr``'s synthesis dependency behaves while running ``openfpgaloader``::

    xeda run openfpgaloader blinky.toml -s nextpnr.yosys.flatten=true

Settings a flow shares with its dependency -- ``fpga``, ``clocks`` and ``board`` for the FPGA
flows -- are resolved when the dependency is launched: the flow's own value is used for both, and
if the flow leaves a shared setting unset, the value given in the dependency's nested settings is
used instead.

Runs are make-like by default: a dependency whose trace still matches what it would consume now
is skipped and its recorded results reused (``--rebuild-all`` runs every flow). See :doc:`run-directories`.

Open-source FPGA flow targets and tuning
========================================

``yosys_fpga`` uses the target's Yosys synthesis pass. It supports ECP5, iCE40, Nexus,
Xilinx and Gowin synthesis. ``nextpnr`` adds verified device selection, constraints and output
formats for ECP5 (Trellis ``textcfg``), iCE40 (``asc``) and Nexus (``fasm``). The
``openfpgaloader`` chain packs and programs ECP5 with ``ecppack`` and iCE40 with ``icepack``.
Both reject a family they have no tested mapping or packer for when they start, before any
synthesis runs. For Xilinx 7-series placement and routing, use the separate ``openxc7`` flow.

The options of Yosys' synthesis passes changed between releases, so ``yosys_fpga`` chooses them
by the installed Yosys version (0.63 is the minimum; flags are checked through 0.69). ABC9 is
the default mapper everywhere: opt-in ``-abc9`` for Xilinx before 0.69, built in otherwise.
``abc9 = false`` maps with classic ABC and ``retime`` enables ``-retime``;
Yosys 0.69 removed both choices, so there they are errors. On iCE40, ``noabc`` maps LUTs without
ABC altogether. ``flatten`` left unset keeps each pass's own choice (Xilinx keeps the hierarchy,
the Lattice, iCE40 and Gowin passes flatten it); ``true`` or ``false`` overrides it. A setting the
selected pass has no option for is an error rather than being ignored. The ``nowidelut``
restriction is off: it can reduce area or timing on one design and worsen the other on another.
iCE40 UltraPlus DSP and SPRAM inference are available as ``yosys.ice40_dsp`` and
``yosys.ice40_spram``.

A Lattice ordering code as ``fpga.part`` selects the Yosys timing model and the nextpnr device
and package: ``iCE40UP5K-SG48I`` or ``iCE40HX8K-CT256`` (temperature grade and tape-and-reel
suffixes are accepted), ``LFE5U-85F-8BG381C``, and for Nexus the full device nextpnr-nexus
names, ``LIFCL-40-9BG400C`` (``nextpnr-nexus --list-devices``).

nextpnr uses each backend's own default timing-driven placer and router. Specify the actual
part, package, speed grade and timing constraints before comparing results. ``clock.period``
sets the target frequency; ``lpf_cfg`` (ECP5), ``pcf_cfg`` (iCE40), ``pdc_cfg`` (Nexus),
``sdc``, the hooks and ``py_script`` name files relative to the design root (or with
``$DESIGN_ROOT``). A setting of another architecture is an error. A fixed ``seed`` makes comparisons
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

Bluespec
========

``bsc`` and ``bsc_sim`` compile Bluespec (BSV, ``.bsv``, and BH -- Bluespec Classic, ``.bs``)
with the Bluespec compiler, ``bsc``. ``bsc`` generates Verilog for ``rtl.top``; ``bsc_sim``
compiles a Bluespec testbench and simulates it. Both need bsc 2026.07.1 or newer, checked when
the flow starts.

Bluespec compiles packages in source order, and the package that defines the top module must come
last: for ``bsc`` the last Bluespec source in ``rtl.sources`` must define ``rtl.top``; for
``bsc_sim`` the last one in ``tb.sources`` must define ``tb.top`` (or, for a design without a
testbench, the last in ``rtl.sources`` ``rtl.top``). A package's name is its file's stem, so a
multi-package design lists each package's file, the top's last.

``rtl.defines`` and ``rtl.parameters`` (and, for ``bsc_sim``, the testbench's over the RTL's) are
passed to bsc as preprocessor macros. bsc's own preprocessor reads BSV only, so a BH (``.bs``)
source sees them only through the C preprocessor: set ``cpp = true``, which also feeds the macros
to it; without it, a design that defines macros but whose Bluespec sources are all BH is warned
about.

``bsc``'s ``artifacts.verilog`` is the file set a downstream synthesis or simulation flow needs:
the generated modules, the design's own Verilog sources (listed where they are), and the bsc
library and ``import "BVI"``-imported modules the design instantiates that are found on the
Verilog search path, copied into ``verilog_out_dir``. A module the generated Verilog
instantiates that no Verilog file defines (a vendor primitive, for instance) is left out of the
list, with a warning: the downstream tool must provide it. With ``positive_reset`` (on by
default), every file ``bsc`` generated or copied begins with a Verilog macro definition for
``BSV_POSITIVE_RESET``, so the generated and library modules reset active-high. The design's own
Verilog sources are left unchanged; a first file, ``bsv_defines.v``, defines the macro ahead of
them, which sets their reset polarity only where they read the macro themselves. Name the reset
port with ``reset_prefix`` if a downstream flow expects one.

``bsc_sim`` runs the testbench with Bluesim (the default), bsc's own cycle-based simulator, or,
through bsc's ``-vsim`` link step, a Verilog simulator: Verilator, Icarus Verilog, or another one
bsc supports. The run fails when the simulation exits with a failure status -- a testbench's
``$fatal`` or a failing ``dynamicAssert`` -- and passes otherwise; ``$finish(n)``'s argument is a
verbosity level, not a status, and of Bluesim, Verilator and Icarus Verilog, ``$error`` fails
the run only under Verilator.

.. code-block:: bash

    xeda run bsc examples/bluespec/gcd/gcd.toml
    xeda run bsc_sim examples/bluespec/gcd/gcd.toml -s simulator=verilator

See ``examples/bluespec/`` for self-checking designs in both BSV and BH, including a
multi-package design, one sized by macros, one importing Verilog with ``import "BVI"``, and one
mixing BSV and BH.

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

Simulation results
==================

A simulation passes only on evidence that it ended, not on the simulator's exit status alone. A
cocotb run, on any simulator, needs at least one test that ran and none that failed; a run in
which every test was skipped fails. Of the simulator flows, only ``verilator`` is converted so
far. ``bsc_sim`` takes a ``timeout``, but is otherwise judged as before, by its exit status; so
are the other simulators.

Verilator
---------

Verilator is driven by Xeda's own C++ main, and Xeda's hooks in Verilator's runtime record how the
run ended: the ``sim.ended_by`` and ``sim.time`` result keys, among others. The run passes when it
ends by ``$finish`` or at the requested ``stop_time`` and nothing reported reaches
``fail_severity`` (``warning``, ``error``, ``failure`` or ``fatal``; default ``error``); an event
queue that runs empty without a ``$finish`` fails, and ``timeout`` stops a run that does not end.
A design with its own C++ driver keeps it, and the hooks still record how it ended: it passes when
the driver exits with status 0 and nothing reported reaches ``fail_severity``. With cocotb,
cocotb's results decide the run. The model's output is also copied to ``sim.log`` in ``sim_dir``,
except under cocotb, whose output goes straight to the terminal.

The simulated top (``--top-module``) is the testbench's ``tb.top``, or else ``rtl.top``; with
cocotb, the module cocotb drives: ``tb.cocotb.toplevel``, or else ``rtl.top``. ``rtl.parameters``
apply only when the RTL top is the simulated top; ``tb.parameters`` always do.

Changed behavior:

* ``random_init`` defaults to false, as in Verilator, and ``random_seed`` is used only with it;
  ``x_initial`` and ``x_assign`` default to ``"0"`` (they were ``"unique"``).
* ``stop_time`` is enforced by Xeda's own driver only: with cocotb, or with a design's own C++
  driver, it is an error.
* ``generate_systemc`` needs the design's own ``sc_main`` among its C++ sources: without one it is
  an error, since Xeda's driver runs a C++ model.
* Verilator 5.024 or newer is required.

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
