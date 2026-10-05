*********************
The design file
*********************

A design description says *what* your design is. It is a single YAML, TOML or JSON file; YAML is
the recommended format. It holds no tool invocations - only, optionally, per-flow settings.

The authoritative, machine-readable definition is::

    xeda design-schema            # JSON Schema, for validation or generation

YAML parsing
============

YAML follows the 1.2 core schema. ``yes/no/on/off/y/n`` are strings, ``010`` is decimal 10,
``0o17`` is 15, ``0x1F`` is 31, and ``1e3`` is a float. Dates, sexagesimal values
(``1:30``) and underscore-separated numbers (``1_000``) are strings. ``true``/``false``
are booleans; ``null``/``~`` are null. Quote values intended as HDL text and source paths.

A boolean setting accepts only ``true`` and ``false`` (not ``yes`` or ``1``), in every format (and ``-s key=true`` on
the command line). ``debug: yes`` or ``-s debug=on`` is an error saying that ``yes`` is text,
not a boolean, and to write ``true``; a field that is not a boolean says the same about a word
YAML 1.1 would have read as one (``ncpus: on``).

Mapping keys must be strings. Duplicate keys fail naming the file, key and both lines.
Ordinary aliases and core explicit tags are accepted; merge keys, recursive aliases and
non-core tags are rejected. Use two-space indentation and a complete source entry per list
item. TOML and JSON designs and projects remain accepted.

Top level
=========

.. list-table::
   :header-rows: 1
   :widths: 18 12 70

   * - Key
     - Required
     - Meaning
   * - ``name``
     - no
     - Unique design name. Defaults to the design file's stem. It names the run directory, so keep
       it filesystem-friendly.
   * - ``rtl``
     - yes [1]_
     - The design itself. See `rtl`_.
   * - ``tb``
     - no
     - The testbench. Required by simulation flows. See `tb`_.
   * - ``description``
     - no
     - One-line description.
   * - ``authors`` (``author``)
     - no
     - One ``"Name <email>"`` string or a list of them.
   * - ``language`` (``hdl``)
     - no
     - Language standards. See `language`_.
   * - ``flows``
     - no
     - Per-flow settings. See `flows`_.
   * - ``dependencies``
     - no
     - Other designs this one depends on.
   * - ``license``, ``version``, ``url``
     - no
     - Metadata, not used by any flow.
   * - ``design_root``
     - no
     - Base directory for relative paths. Defaults to the directory holding the design file, which
       is almost always what you want.

.. [1] The loader also accepts ``sources``, ``top``, ``clock``, ``clocks``, ``parameters``,
   ``defines`` and ``generator`` at the top level. It folds them into ``rtl`` before validation.

.. _rtl:

``rtl`` - the design
====================

.. list-table::
   :header-rows: 1
   :widths: 18 12 70

   * - Key
     - Required
     - Meaning
   * - ``sources``
     - yes
     - Source files, **in compilation order**. Relative paths resolve against the design file's
       directory. See `sources`_.
   * - ``top``
     - no
     - Top-level module/entity name. Required by synthesis flows.
   * - ``parameters`` / ``generics``
     - no
     - Verilog parameters or VHDL generics for the top level, as a mapping. Use either
       interchangeable name; giving both is an error.
   * - ``defines``
     - no
     - Verilog preprocessor macros, as a mapping.
   * - ``clock_port``
     - no
     - Compatibility shorthand for a single-clock design. Prefer ``clock: {port: "..."}``.
   * - ``clock``
     - no
     - A single clock as ``{port: "...", name: "..."}``.
   * - ``clocks``
     - no
     - A list of clocks, for multi-clock designs.
   * - ``attributes``
     - no
     - HDL attributes to attach to objects, as ``attribute -> (object -> value)``.
   * - ``generator``
     - no
     - Command or generator class that produces the sources (e.g. Chisel elaboration) before the
       flow runs. It runs again only when what it reads or produced changed; see
       :ref:`generators`.

Clocks here are *logical*: they name the design's clock ports. The *physical* period or frequency
is a flow setting, because it is a constraint on a particular build rather than a property of the
design. A single-clock design usually needs only::

    rtl:
      clock: {port: clk}

and then, per flow, ``clock.period`` (ns) or ``clock.freq`` (MHz), as a number or with a unit
(``"5.5ns"``, ``"200MHz"``). Units are case-sensitive, as in SI: ``"200mhz"`` is an error that
names ``MHz``. The legacy ``clock_port`` and ``clock_period`` inputs are accepted for
compatibility, but cannot be combined with their canonical counterparts in the same layer.

.. _tb:

``tb`` - the testbench
======================

The aliases ``test`` and ``tests`` are also accepted. The section takes the same ``sources``,
``parameters``/``generics`` and ``defines`` keys as ``rtl``, plus:

.. list-table::
   :header-rows: 1
   :widths: 18 82

   * - Key
     - Meaning
   * - ``top``
     - Toplevel testbench module. Up to two may be given, as a list, when a secondary toplevel is
       needed (e.g. a VHDL configuration alongside an entity).
   * - ``uut``
     - Instance name of the unit under test inside the testbench. Some flows need it to locate
       signals for waveform dumping or activity capture.
   * - ``cocotb``
     - ``true``, or a table with ``module``, ``toplevel`` and ``testcase``. Detected automatically
       when a ``.py`` source is present, so ``cocotb: true`` is usually redundant.

.. _sources:

Source files
============

A path string suffices when its extension identifies a type:

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Extension
     - Type
   * - ``.vhd``, ``.vhdl``
     - ``Vhdl``
   * - ``.v`` / ``.sv``
     - ``Verilog`` / ``SystemVerilog``
   * - ``.vh`` / ``.svh``
     - ``VerilogHeader`` / ``SVHeader``
   * - ``.bsv``, ``.bs``, ``.bh``
     - ``Bluespec``
   * - ``.py``
     - ``Cocotb``
   * - ``.cc``, ``.cpp``, ``.cxx`` / ``.c`` / ``.h``, ``.hpp`` / ``.o``, ``.a``
     - ``Cpp`` / ``C`` / ``CHeader`` / ``ObjectFile``
   * - ``.sc``
     - ``Chisel``
   * - ``.xdc`` / ``.sdc`` / ``.lpf`` / ``.pcf`` / ``.pdc``
     - ``Xdc`` / ``Sdc`` / ``Lpf`` / ``Pcf`` / ``Pdc``
   * - ``.ucf`` / ``.xcf`` / ``.qsf`` / ``.ldc`` / ``.fdc``
     - ``Ucf`` / ``Xcf`` / ``Qsf`` / ``Ldc`` / ``Fdc``
   * - ``.tcl``
     - ``Tcl``
   * - ``.mem``, ``.init``, ``.hex``
     - ``MemoryFile``
   * - ``.asc`` / ``.fasm`` / ``.bit``, ``.sof``
     - ``IceAsc`` / ``Fasm`` / ``Bitstream``
   * - ``.blif`` / ``.edf``, ``.edif``
     - ``Blif`` / ``Edif``
   * - ``.sdf`` / ``.spef`` / ``.saif``
     - ``Sdf`` / ``Spef`` / ``Saif``
   * - ``.vcd`` / ``.fst`` / ``.ghw`` / ``.vpd`` / ``.fsdb``
     - ``Vcd`` / ``Fst`` / ``Ghw`` / ``Vpd`` / ``Fsdb``
   * - ``.dcp`` / ``.lib`` / ``.def`` / ``.odb`` / ``.gds`` / ``.cdl`` / ``.vlt``
     - ``Checkpoint`` / ``Liberty`` / ``Def`` / ``Odb`` / ``Gds`` / ``Cdl`` / ``Vlt``

Every source has a type. A suffix is matched exactly as written: ``TOP.VHD`` is not inferred (the
error names ``.vhd``). ``.json``, ``.bin``, ``.cfg`` and ``.config`` name several kinds of file,
and a suffix not in the table names nothing xeda knows, so a source with one needs its ``type``;
``JsonNetlist``, ``EcpConfig``, ``VerilogNetlist``, ``VhdlNetlist``, ``Chipdb`` and ``Data`` are
only ever given that way. Explicit type names are case-tolerant; suffixes are not. ``Data``
is for a file with no automatic HDL frontend (test vectors, a script's input); a flow or the
design can still read it. An invalid explicit ``type`` is an error naming the closest types.
A ``.v`` file is ``Verilog``; give a gate-level netlist explicitly as
``{ file = "net.v", type = "VerilogNetlist" }``.

A source of a later stage's type stands in for the flows that would build it: a ``JsonNetlist``
skips synthesis for ``nextpnr``, a ``Fasm``, ``EcpConfig`` or ``IceAsc`` configuration is packed
by ``fpga_pack`` without placing, and a ``Bitstream`` is what ``openfpgaloader`` programs, with
nothing built:

.. code-block:: yaml

    rtl:
      sources:
        - top.v
        - {file: build/top.bit, type: Bitstream}   # or just build/top.bit: typed by its suffix
      top: top

Source-consumption contracts apply to ``vivado_synth``, ``vivado_alt_synth``, ``vivado_project``,
``quartus``, ``diamond_synth``, ``ise_synth``, ``dc`` and ``yosys_fpga``. They read only the types
they declare: ``vivado_synth`` passes over an ``Lpf`` source, for example. A source in an
unsupported language (a Bluespec source for ``vivado_synth``) is an error naming it before the
flow runs. Contracts cover ``rtl.sources``; ``vivado_project`` also checks ``tb.sources``.
Headers reach include/search paths; no template turns a source type's name into a tool command.

.. note::
   The ``bsc`` flow compiles BH (Bluespec Classic) only from ``.bs`` files; it rejects a ``.bh``
   source with a settings error naming the file to rename.

When inference is not enough, give a table instead of a string:

.. code-block:: yaml

    rtl:
      sources:
        - pkg.vhdl
        - {file: legacy.v, type: SystemVerilog}
        - {file: old.vhdl, standard: '93'}
        - {path: generated/top.v} # not checked for existence

``file`` must exist, and be a file rather than a directory, when the design is loaded; ``path``
is not checked, for sources a generator will produce. Every source carries a content hash and a
path relative to the design root in its design identity. A tool can resolve another file from a
source's location -- for example, a Verilog ``include`` searches the including file's directory
first, and a TCL script can use ``[file dirname [info script]]``. Re-arranging sources is a
different design; moving the whole design never is.

A source containing ``*`` is a pattern (``"src/*.vhd"``), and ``*`` is the only pattern
character: ``?``, ``[`` and ``]`` are ordinary characters of a file name, so ``"rtl/fifo[1].v"``
names exactly that file, and ``"rtl/fifo[1]_*.v"`` matches ``fifo[1]_a.v`` but not ``fifo1_a.v``.
A pattern's matches are inserted in sorted order, so the compilation order -- and the design's
hash -- do not depend on the filesystem. Only files match: a directory whose name fits the
pattern is passed over. A variable in a pattern (``$DESIGN_ROOT``, or any environment variable)
stands for the place it names, so a ``*`` in its value is not pattern syntax either. A pattern
that matches no file is an error, so that a mistyped one cannot quietly contribute nothing. A
generator's ``sources`` follow the same rules.

Xeda hands each tool a file by its own name, however it is spelled. Yosys, whose readers would
expand ``fifo[1].v`` as a pattern of their own (and read ``fifo1.v``), is given it escaped;
TCL-scripted tools are given every source and every constraint or script file you name as one
literal word, so a space, ``[`` or ``$`` in it is never split, substituted or run. Such names are
verified end to end with yosys, and with Vivado's ``read_verilog``, ``read_vhdl`` and
``read_xdc``. Vivado's ``add_files`` -- which ``vivado_project`` uses for every file, and
``vivado_synth`` for memory files, ``xdc_files`` and ``tcl_files`` --
refuses a name containing ``[``, ``]`` or ``$``: rename such a file for Vivado.

.. _generators:

``generator`` - sources xeda builds first
=========================================

A design can have its sources produced before any flow runs: ``rtl.generator`` is a shell
command, a list of arguments, or a table configuring one::

    name: generated
    rtl:
      sources: ["gen/top.v"]
      top: top
      generator:
        executable: python3
        args: ["soc.py", "--build-dir", "gen"]
        sources: ["soc.py"]
        packages: ["litex", "litex_boards", "migen"]

The generator runs while the design is loaded, and runs **again only when something it reads or
produced changed**, judged by content, never by a modification time:

``sources``
    the files it reads, as a design's own ``sources`` name them (patterns included). Each one has
    to exist, since its content is read. A ``touch``, a ``chmod`` or a branch round-trip of one
    is not a change; an edit given back its old timestamp is. A source naming a *directory* (a
    Chisel ``src/main/scala``, a directory of templates) counts as every file in it, so a file
    edited inside one, or added to one, runs the generator again.

``packages``
    installed Python packages, by import name, that it reads and that no design could list as
    files -- a SoC description reading ``litex``, ``litex_boards`` and ``migen`` from the virtual
    environment. Every file of each one is digested (its bytecode left out), so installing,
    upgrading or editing one runs the generator again. A package nothing provides is an error
    naming it. Each package is digested once per ``xeda`` invocation, so a package changed
    *during* one invocation is noticed by the next.

``generated_sources``
    which of ``rtl.sources`` the generator writes, when it does not write them all. Left out,
    every declared source is judged, so editing a hand-written one runs the generator too.

``always_runs``
    run it on every load. For a generator whose inputs cannot be judged at all -- they are not
    files, or they cannot be listed. A generator that declares neither ``sources`` nor
    ``packages`` runs on every load anyway, since nothing says when it is out of date.

What xeda keeps about a generation -- the digest of every source it left -- is a record under
``<run root>/.cache/generators/``, beside the chip databases and everything else of xeda's: the
design's own tree holds nothing of xeda's. So a design loaded with no run root in sight (a
``Design`` built by hand, outside ``xeda run``) has nowhere to keep that record, and its
generator runs on every load -- the direction xeda takes wherever it cannot prove something is up
to date. The run root is made to *write* that record, after a generation that succeeded, never to
look one up, so a generator that fails leaves no run root behind; ``xeda run --dry-run`` creates
nothing at all, reads an existing record, and refuses to plan a design that would have to
generate.

A generator given as a shell command (``generator: "python soc.py"``) or as a list of arguments
declares nothing it reads, so it runs on every load. Write it as a table with ``sources`` to have
it judged.

``--rebuild-all`` (and ``--clean``, which implies it) runs the generator whatever its record says,
as it runs every flow of the launch, and records what that generation leaves: that -- not a
``touch`` -- is what forces a regeneration when something xeda cannot see has changed. An
environment variable the generator reads is such a thing; so is anything its ``sources`` and
``packages`` do not name.

Writing the design's tree is what a generator is *for*, so what it writes there is its own
business -- a litex build directory, for instance. Xeda itself writes nothing outside its run root
while a design loads: the record of the generation is the only thing it keeps, and it keeps it
there.

.. _language:

``language`` - standards
========================

.. code-block:: yaml

    language:
      vhdl:
        standard: '2008' # or '93', '2019', ...
      verilog:
        standard: '2005'

``version`` is accepted as an alias of ``standard``. Two-digit forms (``08``) and four-digit forms
(``2008``) both work.

.. _flows:

``flows`` - per-flow settings
=============================

Settings for a specific flow live under ``flows.<flow_name>``. They apply only when that flow
runs, so one design file can carry constraints for several targets:

.. code-block:: yaml

    flows:
      vivado_synth:
        fpga.part: xc7a100tftg256-2L
        clock.period: 5.0
      openroad:
        platform: sky130hd
        clock.period: 10.0
      ghdl_sim:
        stop_time: 100us

``xeda list-settings <flow>`` lists what a given flow accepts. Unknown keys are rejected, so a
typo fails loudly rather than being ignored.

A section may also hold ``inputs``: where a flow that declares file inputs reads each from, as
``<producer flow>`` or ``<producer flow>.<output>`` (a list for an input that takes several).
It is saved wiring that the resolver reads, not one of the flow's settings -- it is absent from
``xeda list-settings`` and from the flow's ``settings.json`` -- and it names a flow's output,
never a file; an external file is a typed source. A saved binding is chosen before a typed
source and before the input's default producer:

.. code-block:: yaml

    # bound_demo.yaml
    name: bound_demo
    rtl:
      top: top
      sources:
        - top.v
        - file: top.json       # a prebuilt netlist ...
          type: JsonNetlist
    flows:
      nextpnr:
        fpga:
          part: LFE5U-85F-6BG381C
        inputs:
          netlist: yosys_fpga.netlist   # ... that this binding chooses not to use

The same section in a project file has the same meaning, below the design's. ``xeda run
a+b+c`` chains and bindings are described in :ref:`flow-chains`, and a chain given on the command
line replaces a saved binding of the same input.

Environment variables in paths
==============================

Settings typed as paths expand ``$PWD``, ``$DESIGN_ROOT`` and ``$DESIGN_DIR``, which keeps a
design file portable across machines:

.. code-block:: yaml

    flows:
      dc:
        target_libraries: [$DESIGN_ROOT/lib/SAED90/saed90nm_typ_ht.db]

Design sources expand environment variables too, except ``$PWD``. There ``$DESIGN_ROOT`` and
``$DESIGN_DIR`` name the design root, the directory a relative source is resolved against, so
``"$DESIGN_ROOT/src/*.vhd"`` and ``"src/*.vhd"`` are the same sources.

Multiple designs in one project
===============================

A ``xedaproject.yaml`` can hold several designs, plus top-level ``flows`` settings that are merged
into each. ``xedaproject.yml`` and ``xedaproject.toml`` are also accepted; automatic discovery
requires exactly one of these names in the directory. Select a design with ``--design-name``, or
let Xeda prompt you interactively.
