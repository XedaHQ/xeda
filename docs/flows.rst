*****
Flows
*****

A flow is a named sequence of steps performed by one or more tools. ``xeda list-flows`` is the
authoritative list for your installed version; this page explains how flows are organized and how
to find your way around one.

Discovering flows and their settings
====================================

.. code-block:: bash

    xeda list-flows                     # name, aliases, category, description, dependencies,
                                        # declared inputs/outputs, what can follow it in a chain
    xeda list-settings <flow>           # every setting: name, type, default, meaning
    xeda list-results <flow>            # every key the flow writes to results.json

``list-flows`` reports a ``category`` derived from the flow's base class:

``simulation``
    Runs a testbench. Adds settings for ``stop_time``, waveform output, and cocotb.

``fpga_synthesis``
    Targets an FPGA. Adds an ``fpga`` setting (a part identifier, or a ``board`` name that fills
    it in) on top of the clock settings. A ``board`` is looked up in the bundled database
    (``xeda list-boards``) or, with ``custom_boards_file``, in your own: a TOML or YAML file,
    chosen by its suffix (``.toml``, ``.yaml`` or ``.yml``; any other suffix is an error), mapping
    each board name to its ``fpga``, programmer ``name`` and local ``lpf``. A relative
    ``custom_boards_file`` resolves against the design directory, and a board's local ``lpf``
    against the database file's own directory, in either format. YAML is read as YAML 1.2, like
    every other YAML file xeda reads::

        # boards.yaml
        MY_BOARD:
          name: ulx3s
          fpga:
            part: LFE5U-85F-6BG381C
          lpf: my_board.lpf

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

Every flow declares its own settings. They can be given in six places, in increasing order of
precedence:

1. the flow's own defaults
2. a ``xedaproject.yaml``'s ``flows.<flow_name>`` section
3. the design file's ``flows.<flow_name>`` section
4. the selected target's ``flows.<flow_name>`` section, if you chose a target with ``--target``
   (see :ref:`targets`)
5. the command line, via ``-s``/``--settings``
6. the API, via ``flow_overrides``

They merge key by key: ``-s clock.freq=100MHz`` changes that one setting of a ``clock`` section
given in the design file, rather than replacing the whole section. Precedence goes by where a
setting was given first (project, design, target, command line, API). A target's value wins over
the design's for the same key, and only for the keys the target writes.

A flow's settings are written in one place, its own ``flows.<flow_name>`` section, whichever
flow's run needs it: ``-s flows.yosys_fpga.flatten=true`` sets the synthesis that ``nextpnr``
places. ``nextpnr``'s former nested ``yosys`` section was removed and says so, naming
``flows.yosys_fpga.<key>``. The Vivado simulation and power flows follow the same rule:
``vivado_postsynth_sim``'s former ``synth`` section names ``flows.vivado_synth.<key>``, and
``vivado_power``'s former ``postsynthsim`` section and simulation controls (``elab_debug``,
``saif``, ``stop_time``, ``prerun_time``, ``timeout``, ``fail_severity``) name
``flows.vivado_postsynth_sim.<key>``. ``vivado_power`` asks that flow for timing activity, which
switches its ``timing_sim`` on, so ``timing_sim`` is no setting of power. ``openroad`` takes its
netlist from ``yosys`` the same way, so its synthesis is configured in ``flows.yosys``
(``optimize``, ``abc_driver_cell``, ...; ``-s flows.yosys.optimize=speed`` with ``xeda run
openroad``), and ``yosys`` derives everything else from the ``platform`` the two share.

``-s flows.<flow>.<key>=<value>`` sets a setting of any flow in the run: the requested flow, or one
of its declared dependencies. ``-s flows.nextpnr.seed=2`` and ``-s seed=2`` are the same setting
when ``nextpnr`` is the requested flow; giving both different values is an error. A flow that is
not part of the run is an error too, with the close matches suggested.

``-s`` takes space-separated ``KEY=VALUE`` items. It ends at the next option, or at the first
token that is not ``KEY=VALUE``, so it never takes the design file for a setting; ``--`` ends the
options.

Command-line settings take dotted keys for nested values, and several can be given at once::

    xeda run vivado_synth sqrt.yaml -s clock.period=4.5 synth.strategy=Flow_PerfOptimized_high

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
chain leading to it. To name the stages yourself, see :ref:`flow-chains`.

.. code-block:: text

    openfpgaloader  ->  fpga_pack  ->  nextpnr  ->  yosys_fpga
    vivado_power    ->  vivado_postsynth_sim  ->  vivado_synth
    openroad        ->  yosys

A dependency runs in its own run directory, a sibling of the flow that launched it. Its settings
are its own section's (``flows.yosys_fpga``), set from the command line with
``-s flows.<flow>.<setting>``. For example, to change how the synthesis stage behaves while
running ``openfpgaloader``::

    xeda run openfpgaloader blinky.yaml -s flows.yosys_fpga.flatten=true

Some flows declare inputs and outputs (``xeda list-flows --json`` exposes them). ``nextpnr``
reads a ``netlist``, by default the one ``yosys_fpga`` writes. A source of type ``JsonNetlist``
in ``rtl.sources`` supplies that input instead and skips synthesis:

.. code-block:: yaml

    rtl:
      sources: [{file: top.json, type: JsonNetlist}]
      top: top

Xeda resolves the declared producers and their settings before anything runs. A scalar input
requires exactly one matching source. Settings for a producer displaced by sources are unused
and logged. The first declared outputs are ``yosys_fpga.netlist`` (enabled by ``netlist_json``)
and ``nextpnr.config`` (the selected ECP5 ``textcfg``, iCE40 ``asc`` or Nexus/Xilinx ``fasm``).
``fpga_pack`` packs that configuration, or a typed ``EcpConfig``, ``IceAsc`` or ``Fasm`` source,
into its ``bitstream`` output; ``openfpgaloader`` programs that bitstream, or a typed
``Bitstream`` source, and declares no output.
An enabled configuration that is missing or stale fails the run; disabled outputs are omitted.
Output paths are set by the flow, verified and recorded with content digests in ``results.json``.
Consumers get only those checked records, for newly run and reused producers alike.

Along a declared edge, shared settings -- ``fpga``, ``board``, ``custom_boards_file``, ``clocks``,
``prjxray_db``, ``platform``, ``corner``, ``dont_use_cells``, where both endpoints declare them --
must agree. A ``platform`` is one value, compared by what it describes (a bundled name and the path
to that platform's ``config.toml`` agree) and handed on whole; a ``corner`` is compared by the
corner it selects. Leaves given on one node
apply to both; disjoint leaves combine. Different values for the same leaf, for example ``flows.nextpnr.fpga.part`` in a
project and ``flows.yosys_fpga.fpga.part`` in a design, fail before anything runs, naming both
files and sections. A command-line leaf (``-s fpga.part=...`` or
``-s flows.yosys_fpga.fpga.part=...``) wins for the connected group, preserving unrelated leaves.
API overrides retain their separate highest-precedence origin. Normal origin-first precedence
still applies within each node.

Undeclared edges (a flow that still calls ``add_dependency``; no built-in flow does) keep the
legacy rule: the depending flow's nonempty value, else its dependency's nested value.
Undeclared flows may launch declared ones.

While a flow reads any completed dependency's outputs, it holds a verified shared lease on that
run directory until its own launch ends. Another Xeda process cleaning, rebuilding or scrubbing
the producer waits (POSIX only). Missing or changed completion evidence refuses hand-over.

Planning without running
------------------------

``xeda run nextpnr blinky.yaml --dry-run`` prints the immutable plan the launcher would execute:
producers first, directories, hashes, each declared input's source/producer origin and optional
outputs switched on for consumers. Add ``--json`` for a document (see :doc:`machine-readable`).
It runs no tools and changes no run roots, markers, locks or deliveries. Invalid settings,
unsupported targets and shared-setting conflicts fail during planning.

Loading that needs a generator or a Git dependency fetch is refused before those side effects;
materialize the sources first, or pass an already materialized ``Design`` to the library's
``DefaultRunner.plan``. An undeclared node's runtime dependencies are unknown: its ``init()``
is not called while planning. Freshness and always-run decisions are not evaluated, and
``--dry-run --remote`` is refused.

Runs are make-like by default: a dependency whose trace still matches what it would consume now
is skipped and its recorded results reused (``--rebuild-all`` runs every flow). See :doc:`run-directories`.

.. _flow-chains:

Flow chains and input bindings
==============================

``xeda run`` also takes a chain of flows joined by ``+``. Its last flow is the one you ask for;
each flow before it supplies the next one's inputs:

.. code-block:: bash

    xeda run yosys_fpga+nextpnr+fpga_pack design.yaml

That is the build the default route already gives ``xeda run fpga_pack design.yaml``, so a chain
is for naming stages explicitly: to pick an alternate producer, to override a saved binding, or
to see in one line what a request will run. ``xeda run fpga_pack+openfpgaloader design.yaml``
builds the bitstream and then programs it. The chain is a *path*, not a list of waypoints: Xeda
checks each neighboring pair and never searches for a missing stage.

**What a chain means.**

* ``+`` is the only separator, and each flow may appear once. A flow is named as anywhere else
  (canonical name, aliases, dashes), and ``FLOW.OUTPUT`` names the output of a producer that
  has several: ``nextpnr.config+fpga_pack``.
* A flow supplies **every compatible required input** of the next one; optional inputs are not
  bound. An output that fits no input of the next flow, or two outputs that fit, is an error that
  names the choices. A refused chain suggests a valid one only when another output of the
  producer, or the flows that the required declared default producers name between the pair,
  fix it: ``nextpnr+openfpgaloader`` suggests ``nextpnr+fpga_pack+openfpgaloader``. Nothing
  searches all flows, so a refusal with no such route carries no suggestion.
* A flow that programs a device (``openfpgaloader``) can only end a chain.
* Only flows that declare their file inputs and outputs (``declared`` in ``xeda list-flows
  --json``) can be chained: ``yosys_fpga``, ``nextpnr``, ``fpga_pack``, ``openfpgaloader`` and,
  for the outputs they write, ``vivado_synth`` and ``vivado_alt_synth``, and ``vivado_postsynth_sim``
  and ``vivado_power`` today. A flow without
  declarations (``bsc``, ``bsc_sim``, ``vivado_project``, ``vivado_sim``, ...) runs alone and is refused inside a chain, naming it. No stage is ever fed by reading another flow's
  ``artifacts``.
* ``xeda list-flows`` shows, for each declared flow, what it takes and makes and which flows can
  come directly after it (JSON: ``can_precede``, ``can_follow``); shell completion offers only
  the flows that can follow the chain typed so far.

Completion returns the whole token, so ``<TAB>`` after a prefix extends the chain:

.. code-block:: text

    xeda run yosys_fpga+ne<TAB>      ->  yosys_fpga+nextpnr
    xeda run fpga_pack+<TAB>         ->  fpga_pack+openfpgaloader
    xeda run openfpgaloader+<TAB>    ->  (nothing: a programmer can only end a chain)

**Settings.** ``-s`` sets the last flow, as for any run, and ``-s flows.<flow>.<key>=<value>``
sets any other flow of the chain; a flow that is not in the run is an error with the close
matches suggested. A flow's settings are written in one place, its ``flows.<flow>`` section,
whether it runs because you named it or because it is somebody's default producer:

.. code-block:: bash

    xeda run yosys_fpga+nextpnr+fpga_pack design.yaml -s flows.nextpnr.seed=2 flows.yosys_fpga.flatten=true

Shared leaves (``fpga``, ``board``, ``clocks``, ...) given at any one node apply to every node
along the chain's edges. Two different values in files are a conflict that names both nodes and
their files; a command-line value wins for the whole group.

**Results.** ``flow``, ``results``, ``--help-settings`` and the exit status are the last flow's.
``--json`` adds ``request`` (the chain, element by element) and, for every flow of the plan, its
``node`` name and resolved ``inputs``; a flow that was planned but never entered after a failure
is listed as ``"state": "not run"`` (see :doc:`machine-readable`). ``--outputs-to`` delivers the
last flow's outputs, only after the whole chain succeeded. ``--dry-run`` shows the graph and runs
no tools.

Input sources and saved bindings
--------------------------------

Every declared input has one origin, chosen in this order:

1. an **explicit binding** -- a chain adjacency or a saved ``inputs`` entry (below);
2. otherwise a **typed source** in ``rtl.sources``, in source order -- a ``JsonNetlist`` supplies
   ``nextpnr``'s ``netlist`` and no synthesis runs;
3. otherwise the input's **default producer** (``yosys_fpga`` for ``nextpnr``'s ``netlist``).

Typed sources name their kind (``{file: top.json, type: JsonNetlist}``; ``xeda design-schema``
lists the kinds), and a source never mixes with a producer for one input. A binding is saved in a
design or project file under ``flows.<consumer>.inputs`` and names a producer by flow name,
optionally with its output: ``yosys_fpga.netlist``. It is a reference to a flow's output, never a
file path; give an external file as a typed source instead. A list input takes an ordered list
of references.

.. code-block:: yaml

    # routed_demo.yaml
    name: routed_demo
    rtl:
      top: top
      sources:
        - top.v
    flows:
      nextpnr:
        fpga:
          part: LFE5U-85F-6BG381C
        inputs:
          netlist: yosys_fpga.netlist
      fpga_pack:
        inputs:
          config: nextpnr.config

These bindings say what the defaults say, so the plan and every hash are those of the file
without them. They matter when they differ from what the sources or defaults would choose:

.. code-block:: yaml

    # prebuilt_demo.yaml
    name: prebuilt_demo
    rtl:
      top: top
      sources:
        - file: top.json
          type: JsonNetlist
    flows:
      nextpnr:
        fpga:
          part: LFE5U-85F-6BG381C

With it, the source is used unless something binds the input (a saved ``netlist: yosys_fpga``
does, as does the chain):

.. code-block:: bash

    xeda run nextpnr prebuilt_demo.yaml                 # reads top.json, runs no synthesis
    xeda run yosys_fpga+nextpnr prebuilt_demo.yaml      # synthesizes anyway

``--dry-run`` shows which origin each input has and, for a saved binding, the file and section
it came from.

``inputs`` is wiring that the resolver reads, not a setting of the flow: it does not appear in
``xeda list-settings``, is not part of a flow's ``settings.json`` or of the design hash, and is
split out of each origin before the settings are validated, so ``flows.nextpnr.inputs`` never
collides with a ``nextpnr`` setting. What a node's inputs come from does change its identity: the
same flow with the same settings has another hashed run directory when another producer feeds it
(see :doc:`run-directories`).

**Where a binding can be given.** From the lowest rank to the highest: a project file, the
design file, the command line (a chain, ``-s flows.<consumer>.inputs.<input>=<reference>``, or
``-s inputs.<input>=<reference>`` for the requested flow) and the API; a higher rank replaces a
lower one input by input. A chain is command-line input, so it replaces a binding saved in a
design or project for the same input, and the plan says so
(``overridden``). A chain and an explicit command-line or API binding of the same input are an
error, even when they are equal and before either is checked:

.. code-block:: text

    The chain position 1 (yosys_fpga) -> 2 (nextpnr) binds input 'netlist'; the command line:
    flows.nextpnr.inputs.netlist also binds that input. Give the binding in one place, even when equal.

A project file given with ``--xedaproject project.yaml`` holds the same ``flows.<flow>.inputs``
sections, below the design's:

.. code-block:: yaml

    # project.yaml
    flows:
      nextpnr:
        inputs:
          netlist: yosys_fpga

Bindings, like chains, are local. ``--remote`` and ``xeda dse`` refuse a chain and any binding
the request reaches before connecting or starting a worker; a binding saved for a flow the
request does not reach is ignored.

What is supported today
-----------------------

The open FPGA flows -- ``yosys_fpga``, ``nextpnr``, ``fpga_pack``, ``openfpgaloader`` -- for
ECP5, iCE40, Nexus and Xilinx 7-series are what chains cover. The build-only chain
``yosys_fpga+nextpnr+fpga_pack`` runs no programmer and touches no hardware; only a chain that
ends at ``openfpgaloader`` programs a device. The test suite runs those chains against fake
tools, and never starts a real programmer.

Chains that start at Bluespec or go through ``vivado_project`` or ``vivado_sim`` are **not**
available yet, and the command below is refused today (``Flow `bsc` has
no declared I/O and can only be run alone``). They need the remaining flows to declare their
inputs and outputs, a design value that ``bsc`` produces and the flows after it read, and
showcase designs and targets that use them:

.. code-block:: bash

    # not available yet
    xeda run bsc+yosys_fpga+nextpnr+fpga_pack+openfpgaloader knight.yaml

``vivado_synth`` and ``vivado_alt_synth`` declare their outputs, each switched on by its own
setting (``write_netlist``, ``write_timing_netlist``, ``write_checkpoint``, ``bitstream``), so
``vivado_synth+openfpgaloader`` is a chain: the loader switches the ``bitstream`` on. It needs
the design's ``fpga`` and a clock, and it programs a device: plan it with ``--dry-run``.

Binding an input called ``design`` is refused for the same reason: no flow declares one.

Open-source FPGA flow targets and tuning
========================================

``yosys_fpga`` uses the target's Yosys synthesis pass. It supports ECP5, iCE40, Nexus,
Xilinx and Gowin synthesis. ``nextpnr`` adds verified device selection, constraints and output
formats for ECP5 (Trellis ``textcfg``), iCE40 (``asc``), Nexus (``fasm``) and Xilinx 7-series
(``fasm``, with openXC7's ``nextpnr-himbaechel``). ``fpga_pack`` packs ECP5 with ``ecppack``,
iCE40 with ``icepack`` and 7-series with ``fpga-as``; ``openfpgaloader`` programs the bitstream.
Each rejects a family it has no tested mapping or packer for before any synthesis runs. The
former ``open_xc7`` flow was removed: use ``fpga_pack`` to build, ``openfpgaloader`` to program.

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
sets the target frequency. Put pin files in ``rtl.sources`` with types ``Lpf`` (ECP5),
``Pcf`` (iCE40), ``Pdc`` (Nexus), or ``Xdc`` (Xilinx). Files of the selected family are
concatenated in source order; when none are supplied, the selected board's pin file is used.
Typed ``Sdc`` sources are concatenated separately, followed by the optional ``sdc`` setting's
file, and passed once through ``--sdc``. A clock may have only one timing constraint across
pin files, SDC files and flow settings: duplicates name both original paths/lines or the
clock setting and its period. File-only clocks supply timing without a generated frequency.
The removed ``lpf_cfg``, ``pcf_cfg`` and ``pdc_cfg`` settings give a typed-source migration error.
The ``sdc`` setting, hooks and ``py_script`` name files relative to the design root (or with
``$DESIGN_ROOT``). A setting of another architecture is an error. A fixed ``seed`` makes comparisons
reproducible; try several fixed seeds when optimizing a particular design. The common
``placer``, ``router``, HeAP weights and hook settings allow measured experiments. Experimental
``tmg_ripup``, ``parallel_refine`` and iCE40 ``opt_timing`` stay opt-in. A faster or smaller
Yosys netlist alone does not establish an improvement in routed Fmax.

For example, include ``{ file = "pins.pcf", type = "Pcf" }`` in ``rtl.sources``, then::

    xeda run nextpnr blinky.yaml -s fpga.part=iCE40HX1K-TQ144 \
      -s clock.period=20 -s seed=2
    xeda run openfpgaloader blinky.yaml -s write_flash=true -s verify=true

Use ``xeda list-settings yosys_fpga --json``, ``nextpnr --json`` or
``fpga_pack --json`` or ``openfpgaloader --json`` to inspect all named settings.
``synth_flags``, ``extra_args`` and ``fpga_pack``'s ``packer_args`` expose target/version-specific switches that do not have dedicated settings;
the selected installed tool must support those switches. Placement and programming are
different operations: ``openfpgaloader`` is the only flow here that writes hardware.

Running only the Yosys synthesis pass
-------------------------------------

By default ``yosys_fpga`` adds Xeda's preparation and cleanup stages around
``synth_<target>``. ``synth_pass_only = true`` omits those Xeda-owned pre- and post-synthesis
stages and reads the design's sources as a bare ``yosys <files>`` does: a Verilog source with
plain ``read_verilog``, a SystemVerilog source with ``read_verilog -sv``. The reader follows
each source's ``type``, which its suffix gives unless the design states another, so a ``.v``
file typed ``SystemVerilog`` (or a ``.sv`` typed ``Verilog``) is read as that type, not by its
suffix as yosys would. The design
parameters, ``synth_flags`` and an explicitly selected ABC9 script still apply in either mode.
Settings that request extra Xeda stages, or that make the mode read differently from
``yosys <files>``, are refused in pass-only mode rather than silently ignored:

``prep``, ``pre_synth_opt``, ``post_synth_opt``, ``splitnets``, ``post_synth_rename``,
``black_box``, ``keep_hierarchy``, ``set_attribute``, ``set_mod_attribute``, ``clockgate_map``,
``rmports``, ``stop_after``, ``rtl_json``, ``rtl_verilog``, ``rtl_graph``, ``sta``, ``ltp``, a
nonempty ``read_verilog_flags``, a ``systemverilog`` front end other than ``default`` and a
nonempty ``read_systemverilog_flags``.

Three of these are Xeda's own reader choices, which the full recipe keeps, so the mode needs
their native values written out: ``read_verilog_flags: []``, ``systemverilog: default`` and no
``read_systemverilog_flags``. ``read_verilog_flags`` defaults to ``-sv``, which Yosys does not
pass for a ``.v`` file (and ``-sv`` changes what such a file accepts: ``logic`` is a keyword
under it and an identifier without it). ``systemverilog`` defaults to ``slang``, a plugin whose
``read_slang`` is not what ``yosys file.sv`` runs; ``default`` is Yosys' built-in
``read_verilog -sv``, which a ``.sv`` source is read with. They are refused by value, so the
defaults are refused too. On the command line the empty list is ``-s read_verilog_flags=`` (an
empty value); ``-s "read_verilog_flags=[]"`` is the single flag ``[]``.

The default ABC9 behavior depends on the mode. In the full Xeda recipe, an unset ABC9 script
selects ``flow3`` and a constrained clock supplies a clock-derived ABC9 delay. In pass-only mode,
an unset script leaves Yosys' synthesis pass choice in effect and Xeda does not add that
clock-derived delay. ``abc9_script`` can explicitly choose one of Yosys' included scripts in
either mode: ``default``, ``default.area``, ``default.fast``, ``flow``, ``flow2``, ``flow3`` or
``flow3mfs``. Script names are taken from the installed Yosys build. The legacy ``flow3`` setting
is still accepted: ``true`` selects ``flow3`` and ``false`` leaves the script to Yosys. Do not set
both ``abc9_script`` and ``flow3``. ABC9 script selection matters only when ABC9 mapping is enabled.

.. code-block:: bash

    xeda run yosys_fpga blinky.yaml -s synth_pass_only=true read_verilog_flags= \
      systemverilog=default
    xeda run yosys_fpga blinky.yaml -s synth_pass_only=true read_verilog_flags= \
      systemverilog=default abc9_script=flow2
    xeda run fpga_pack blinky.yaml -s flows.yosys_fpga.synth_pass_only=true \
      flows.yosys_fpga.read_verilog_flags= flows.yosys_fpga.systemverilog=default

Pass-only mode does not by itself guarantee the same result as a native Yosys command. To compare
them, match the Yosys version and target, source paths and order, parameters, synthesis-pass
flags and ABC9 script; the reader is matched by the mode itself. For plain Verilog and for
SystemVerilog read by Yosys' built-in front end it reproduces the native netlist cell for cell and
name for name (checked against the installed Yosys). Yosys also embeds source paths in generated
names, so use the same path spellings on both sides.

The target synthesis pass reads its own primitive libraries. If ``verilog_lib`` also names one
of those files, Xeda skips that duplicate read, comparing the file itself (including paths through
links) against the installed Yosys data directory. Every ``read_verilog`` advances Yosys' shared
generated-name counter, and ABC9 maps by generated names; an extra library read can therefore
change the resulting netlist. Xeda gets the data directory from ``yosys-config`` beside the
selected Yosys executable (or in the selected container image). Cell-count differences alone do
not establish a quality difference.

Xilinx 7-series with openXC7
----------------------------

Artix-7, Kintex-7, Spartan-7, Virtex-7 and Zynq-7000 parts build with the openXC7 1.0 toolchain:
its ``yosys``, ``nextpnr-himbaechel`` (the Xilinx backend) and ``fpga-as``. Put its ``bin``
directory on ``PATH``; Xeda finds the tool data from the resolved ``nextpnr-himbaechel``
executable, in ``<prefix>/share/nextpnr/himbaechel`` and ``<prefix>/share/nextpnr/prjxray-db``,
and reads no ``CHIPDB_DIR``-style environment variable. A design for the Arty A7-100T:

.. code-block:: yaml

    name: blinky
    rtl:
      sources:
        - blinky.v
        - blinky.xdc        # pins: an Xdc source, typed by its suffix
      top: blinky
      clock: {port: clk}
    flows:
      yosys_fpga:
        fpga: {part: xc7a100tcsg324-1}
      nextpnr:
        clock: {freq: 100MHz}

``xeda run fpga_pack blinky.yaml`` synthesizes, places and routes, and packs
``outputs/blinky.bit`` in ``fpga_pack``'s run directory; nothing is programmed. ``xeda run
openfpgaloader blinky.yaml`` builds the same bitstream if it is not up to date and loads it.
The part is given once: ``fpga`` is shared along the declared edges.

``fpga.part`` must be the full ordering part -- device, package, pin count and speed grade
(``xc7a100tcsg324-1``) -- as the Project X-Ray database lists it; a bare device name is refused
before synthesis. ``board: ARTY_A7_100T`` or ``ARTY_A7_35T`` fills it in (``xeda list-boards``).

**Constraints.** ``Xdc`` sources carry the pins (``set_property`` with ``PACKAGE_PIN``/``LOC``
and ``IOSTANDARD``) and may carry ``create_clock``; with no ``Xdc`` source and a ``board``, the
board's bundled pin file is used. The bundled Arty files are active, pin-only derivatives of
Digilent's master XDC and keep its port names (``CLK100MHZ``, ``led[0]``, ``sw[0]``,
``btn[0]``, ...), so a design relying on the fallback names its top-level ports that way; the
ULX3S file likewise uses the board's own names (``clk_25mhz``, ``led[0]``, ...). They contain no
clock constraint: timing comes from the flow's ``clock``/``clocks`` or the design's own files,
one authority per clock. With no clock constraint at all, nextpnr analyzes at its 12 MHz default
and Xeda says so.

**The chip database.** nextpnr needs a database for the die, which openXC7 generates from the
Project X-Ray data. Xeda generates it on first use and keeps it under the run root, in
``.cache/xilinx-chipdb/<identity>/<die>.bin``, where every design using that run root shares it.
The first build of an ``xc7a100t`` takes about a minute and about 3.5 GB of memory (larger dies
take more); later launches start no generator. The entry is identified by content -- the nextpnr
and ``bbasm`` executables, the generator and the database family's files -- so another
installation or an updated one gets its own entry. Entries are never modified, and ``xeda
scrub`` and ``--clean`` leave them; delete the ``.cache`` directory to reclaim the space.
``chipdb`` names an existing database file to use instead (nothing is generated), and
``prjxray_db`` another Project X-Ray database root, which ``nextpnr`` and ``fpga_pack`` must
agree on. Generation and nextpnr's Python hooks run with bytecode writing off: the installation
is never written to.

**Tuning.** ``delay_matrix`` (``build``, the backend's default, or ``off``), ``hold_fix``
(``true``, or a pass limit) and ``hold_detour_max`` are the Xilinx backend's own options;
``seed``, ``placer``, ``router`` and the hooks are common to every nextpnr backend.
``placement`` names a location the placement dump is delivered to.

**Results.** ``ff`` counts ``SLICE_FFX`` cells, ``bram`` ``RAMB18E1`` plus ``RAMB36E1``, ``dsp``
``DSP48E1`` and ``io`` pads; ``CARRY4`` and the other cell types are reported under their own
names. ``device`` is the part and ``fabric`` the die that was routed: every ``available`` total
in the utilization describes the fabric, so an ``xc7a35t`` (routed as an ``xc7a50t``) shows the
larger die's totals. ``clock_port`` is given only when the one reported clock domain is itself
a top-level port; a domain named for a buffer's or a clock generator's output net keeps its raw
name alone. A run that misses its timing constraint fails with nextpnr's own "Max frequency ...
(FAIL at ...)" line, unless ``timing_allow_fail`` is set.

``lut`` is reported per toolchain and per stage, and ``LUT:STAGE`` and ``LUT:METHOD`` say which:
after ``nextpnr`` it is the number of distinct LUT locations (tile, site and one of A-D) the
placement occupies -- a location whose two outputs are both used counts once, and LUTs used as
distributed RAM or shift registers are included -- while ``yosys_fpga`` estimates it from the
mapped primitives (``LUT:LOGIC``, ``LUT:RAM``, ``LUT:SRL``) before packing. The two differ for
one design, and neither is certified comparable with Vivado's utilization report: compare a
design against itself across settings or seeds with one toolchain, not across toolchains.

**Packing and programming.** ``fpga_pack`` runs ``fpga-as`` with the Project X-Ray family
database and the part, into a scratch file in its run directory, and publishes the bitstream
only when the packer succeeded with a nonempty file: a failed run leaves no partial bitstream.
``openfpgaloader`` loads into SRAM by default; ``write_flash`` programs the flash, and
``verify`` is accepted only with it.

What is not noticed: an in-place change of the installed Project X-Ray database alone, with
``fpga-as`` itself unchanged, when packing a prebuilt ``Fasm`` source (the files a tool reads
from its own installation are not inputs; ``--rebuild-all`` runs everything).

The ``open_xc7`` flow was removed. Running it, or keeping a ``flows.open_xc7`` section in a
design or project file, fails with "``open_xc7`` was removed: use fpga_pack to build,
openfpgaloader to program". Move its ``nextpnr`` settings to ``flows.nextpnr``, its synthesis
settings to ``flows.yosys_fpga``, and pin files into ``rtl.sources``. ``openfpgaloader``'s
former ``nextpnr``, ``packer_args`` and ``bitstream_file`` settings are removed the same way:
use the ``nextpnr`` and ``fpga_pack`` sections, and a ``Bitstream`` source for a prebuilt file.
``xeda scrub open_xc7 <design>`` still removes the run directories the removed flow left.

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
source sees them only through the C preprocessor: set ``cpp: true``, which also feeds the macros
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
bsc supports. Bluesim, Verilator and Icarus require an observed ``$finish`` or a confirmed
requested limit, with no runtime event reaching ``fail_severity`` (default ``error``).
``$finish(n)``'s argument is a verbosity level, not an exit status. A requested Bluesim
``max_cycles`` passes only with the measured cycle count and final simulated time; an early
explicit finish remains valid. A silent exit 0 and a drained event queue fail. Other bsc
backends still await evidence conversion.

.. code-block:: bash

    xeda run bsc examples/bluespec/gcd/gcd.yaml
    xeda run bsc_sim examples/bluespec/gcd/gcd.yaml -s simulator=verilator

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
which every test was skipped fails. Each non-cocotb simulator must report a recognized end:
``$finish`` (including VHDL ``std.env.finish`` or ``std.env.stop``), a requested and confirmed
``stop_time``, a measured requested ``max_cycles``, or a user-owned C++ driver's observed exit
status 0 where that driver contract is supported. A missing or malformed record, unknown end,
silent exit 0, unrequested stop, or event queue that drains without a finish fails. A nonzero
tool/driver exit always fails, even after a finish. The normalized record is saved as
``sim.evidence`` alongside ``sim.ended_by``, ``sim.time``, ``sim.time_unit``, ``sim.errors`` and
``sim.warnings``.

Every ``SimFlow`` exposes ``timeout`` and ``fail_severity``. The default severity is ``error``;
the choices are ``warning``, ``error``, ``failure`` and ``fatal`` (``failure`` and ``fatal`` have
the same rank). Any observed event at or above the selected threshold fails. ``timeout`` bounds
each subprocess invocation that contains simulation, including analysis and elaboration when
they share that invocation; it is not a cumulative dependency deadline. An unset timeout adds no
wall-clock limit. Verilog ``$stop`` is an error-rank event and fails at the default threshold. VHDL
``std.env.stop`` is accepted as completion.

GHDL and nvc retain the simulator's actual end time. For their sparse-clock behavior, a stop
whose observed time differs from the requested ``stop_time`` fails; Xeda does not rewrite the
reported time to the request. NVC uses a passive VHPI end-time helper, so a C++ compiler is required
when that helper must be built. Its former ``exit_severity`` setting is removed; use
``fail_severity``. Bluesim's ``max_cycles`` passes only when the recorded cycle count equals the
request and the record includes a measured final time and unit. CXXRTL links an exit monitor and
RTL assertion hook into a user-owned C++ driver: an observed driver exit 0 is valid evidence even
when simulated time is unknown, but missing records or abnormal exits fail. CXXRTL rejects
``stop_time`` because the driver controls scheduling.

ModelSim batch runs require matching native end reason, time and TESTSTATUS observations from
this run's checkpoint and bounded runtime logfile. Compilation and loading messages do not
affect the runtime verdict. VHDL ``std.env.stop`` is accepted with native VHDL break evidence,
while Verilog ``$stop`` fails. VHDL failure and SystemVerilog fatal share the native fatal rank.
This contract was verified with ModelSim-Intel Starter 2020.1. Vivado 2024.2 was used to verify
xsim completion, severity, source-qualified VHDL stop and absolute ``stop_time`` behavior across
prerun and runtime. The requested bound is measured against the actual current time, and further
execution is skipped after finish or after the bound. Its ``elab_debug`` must preserve source
information; explicitly disabling it is rejected because VHDL-stop evidence would otherwise be
ambiguous.

VCS and Questa adapters are documentation-only and have not been verified against licensed real tools.
They fail closed when native evidence is not recognizable: VCS quiet ``$finish(0)`` and VHDL
completion fail without a native finish diagnostic, and a UCLI time checkpoint alone does not
prove HDL completion. Questa requires the ModelSim-style owned runtime transcript and checkpoint;
an unrecognized native stop reason or missing source-qualified VHDL evidence fails. Do not treat
their synthetic-tool coverage as real-tool certification.
The accepted ``bsc_sim`` backends are Bluesim, Verilator, Icarus, ModelSim, Questa, VCS, vcsi
and xsim. The legacy ``cvc``, ``cver``, ``isim``, ``ncverilog`` and ``veriwell`` engines are
rejected before compilation because they lack a certified evidence adapter. Icarus's real
runtime evidence and builtin-task capability checks remain mandatory Linux CI gates; they were
not run on macOS.

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

Every new simulator flow must inherit ``SimFlow`` and translate that simulator's current-run
records into ``SimEvidence`` for the shared ``judge_evidence`` verdict. Add its accepted backend
to the behavioral oracle and test positive completion, silent/drained failure, severity,
requested limits, timeout and stale or malformed evidence. Never add an exit-only exemption.

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
