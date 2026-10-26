*********************
The design file
*********************

A design file says what your design is. It is a YAML, TOML or JSON file, and YAML is the
recommended format. Every design file has a name (the file stem when ``name`` is missing) and an
``rtl`` section with the sources of the design. It can also name the top module, the clock ports,
the testbench, the language standards and the settings of individual flows. The top module and the
clocks are optional, and a design can have several clocks.

The file does not say which tools run or how. The flows hold the commands of the tools, and the
file only gives them settings. One key runs a command of its own: ``rtl.generator`` is a command
(or a generator table) that produces sources before any flow runs. See :ref:`generators`.

The JSON Schema of the design file is the machine-readable definition. Print it with::

    xeda design-schema            # JSON Schema, for validation or generation

YAML parsing
============

Xeda reads YAML 1.2 with the core schema. ``yes``, ``no``, ``on``, ``off``, ``y`` and ``n`` are
strings. ``010`` is the decimal number 10, ``0o17`` is 15, ``0x1F`` is 31 and ``1e3`` is a float.
Dates, sexagesimal values (``1:30``) and numbers with underscores (``1_000``) are strings.
``true`` and ``false`` are booleans, and ``null`` and ``~`` are null. Quote values that are meant
as HDL text, and quote source paths.

A boolean setting accepts only ``true`` and ``false``, in every format. On the command line,
``-s key=true`` does the same. ``yes`` and ``1`` are errors. For example, ``debug: yes`` and
``-s debug=on`` fail with a message that ``yes`` (or ``on``) is text and not a boolean, and that
you should write ``true``. A field that is not a boolean gives the same advice for a word that
YAML 1.1 reads as a boolean (``nthreads: on``).

Mapping keys must be strings. A duplicate key is an error that names the file, the key and both
lines. Xeda accepts ordinary aliases and the explicit tags of the core schema. It rejects merge
keys, recursive aliases and other tags. Use two-space indentation, and write each source as one
list item. TOML and JSON designs and projects are also accepted.

.. _dotted-keys:

A dotted key stands for nested tables: ``tb.top: x`` is the same as ``tb: {top: x}``. Dotted keys
work in a design file, in a design entry and in the ``flows`` table of a project file, in a target
and in design overrides. A key cannot be both a value and a table. ``tb: 3`` together with
``tb.top: x`` is an error that names both keys.

Write one table in one spelling inside a mapping. ``fpga.part: P`` together with an ``fpga:``
block in the same mapping is an error that names both spellings, in whichever order they come.
Two dotted keys under one table (``fpga.part`` and ``fpga.speed``) are fine, and so is one block
(``fpga: {part: P, speed: -6}``). Settings from separate origins, such as the design file and
``-s``, are not one mapping: they merge, and the higher origin wins.

A setting that holds a dictionary keyed by names from a tool or a netlist takes a dotted key as
one key. ``flows.openroad.ir_drop.instance_power.u_core.reg_1: 1 mW`` sets the power of the
instance ``u_core.reg_1``, and ``ir_drop: {instance_power: {"u_core.reg_1": 1 mW}}`` means the
same. :ref:`The settings reference <opaque-keys>` lists these settings.

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
     - Unique design name. It defaults to the stem of the design file (without a ``.xeda``
       ending). It names the run directory, so it starts with a letter and holds only letters,
       digits, ``_`` and ``-``.
   * - ``rtl``
     - yes [1]_
     - The design itself. See `rtl`_.
   * - ``tb``
     - no
     - The testbench. Simulation flows need it. See `tb`_.
   * - ``description``
     - no
     - A one-line description.
   * - ``authors``
     - no
     - One ``"Name <email>"`` string or a list of them.
   * - ``language``
     - no
     - Language standards. See `language`_.
   * - ``flows``
     - no
     - Settings for individual flows. See `flows`_.
   * - ``dependencies``
     - no
     - Other designs that this design needs: the path of a design file, or a Git repository
       written ``git+<url>#<design file in the repository>``. A URL without ``git+`` is refused.
   * - ``license``, ``version``, ``url``
     - no
     - Metadata. No flow uses it.
   * - ``design_root``
     - no
     - The directory that relative paths resolve against. It defaults to the directory of the
       design file, which is almost always what you want. A relative ``design_root`` in a design
       file is relative to the directory of that file.
   * - ``targets``
     - no
     - Overlays for individual boards. See :ref:`targets`.

.. [1] A design without ``rtl`` has no RTL sources. The keys of ``rtl`` (``sources``, ``top``,
   ``clocks``, ``parameters``, ``defines`` and ``generator``) belong in the ``rtl`` section, not
   at the top level: Xeda refuses them there and names the key to use.

.. _rtl:

``rtl``: the design
===================

.. list-table::
   :header-rows: 1
   :widths: 18 12 70

   * - Key
     - Required
     - Meaning
   * - ``sources``
     - yes
     - The source files, in compilation order. Relative paths resolve against the design root.
       See `sources`_.
   * - ``top``
     - no
     - The RTL top module (a Verilog module or a VHDL entity). Synthesis flows need it.
   * - ``parameters``
     - no
     - Parameters of the top module (Verilog parameters or VHDL generics), as a mapping or a
       list of ``{name, value}`` objects.
   * - ``defines``
     - no
     - Verilog preprocessor macros, as a mapping.
   * - ``clocks``
     - no
     - A list of clocks. Each clock is a port name, or a table with ``port`` and, optionally,
       ``name``. A clock without a ``name`` is named by its port.
   * - ``attributes``
     - no
     - HDL attributes for objects of the design, as a mapping of attribute name to a mapping of
       object path to value.
   * - ``generator``
     - no
     - A command or a generator table that produces the sources before any flow runs, for
       example a Chisel elaboration. The generator runs again only when something it reads or
       produced has changed. See :ref:`generators`.

A clock in ``rtl`` is logical: it names a clock port of the design. The physical period or
frequency is a flow setting, because it is a constraint of one build and not a property of the
design. A design with one clock usually needs only this:

.. code-block:: yaml

    rtl:
      clocks: [{port: clk}]

In each flow, set ``clocks.main_clock.period`` or ``clocks.main_clock.freq`` with a unit: ``clocks.main_clock.period: 5.5 ns`` or
``clocks.main_clock.freq: 200 MHz``. A number without a unit is an error. Units are case-sensitive, as in SI:
``200 mhz`` is an error that names ``MHz``. See :ref:`quantities` for the kinds and the units.

Xeda refuses the design key ``clock_port`` and the flow setting ``clock_period``, and names the
replacement: write ``clocks: [{port: <port>}]`` in ``rtl`` and ``clocks.<name>.period`` in a flow.

.. _tb:

``tb``: the testbench
=====================

The section takes the same ``sources``, ``parameters`` and ``defines`` keys as ``rtl``, and these
keys:

.. list-table::
   :header-rows: 1
   :widths: 18 82

   * - Key
     - Meaning
   * - ``top``
     - The top module of the testbench. Give one name, or a list of at most two: a primary top
       module and a secondary one (for example, a VHDL configuration next to an entity). A
       simulation flow needs ``tb.top`` when ``tb.sources`` holds a source in a hardware
       description language (Verilog, SystemVerilog, VHDL, Bluespec or Chisel) and the testbench
       does not use cocotb. Without it, the simulator would run ``rtl.top``, which has no
       stimulus. The flow refuses such a design and names ``tb.top``. Some simulators do not need
       it. ``ghdl_sim`` finds the top of a VHDL testbench itself. ``yosys_sim`` runs a C++ driver
       of the design, whatever HDL the testbench also holds, and so does ``verilator`` with
       ``main: false``.
   * - ``uut``
     - The instance name of the unit under test in the testbench. Flows use it as the scope of an
       SDF annotation when ``sdf.root`` is not set (``ghdl_sim``, ``modelsim``, ``vivado_sim``
       and ``vivado_postsynth_sim``), as the scope of the SAIF activity of the two Vivado
       simulation flows, and as the scope of the VCD dump of ``modelsim``. A simulator with an
       ``sdf`` setting (``ghdl_sim``, ``modelsim`` and the two Vivado flows) refuses an SDF file
       when neither ``sdf.root`` nor ``uut`` is set, and the two Vivado flows refuse a design
       without ``uut`` when they record SAIF activity.
   * - ``cocotb``
     - ``true``, or a table with ``module``, ``toplevel`` and ``testcase``. Xeda sets it when a
       ``.py`` source is present, so ``cocotb: true`` is usually redundant.
   * - ``python_packages``
     - A list of the Python packages that the testbench imports, for example ``cocotb``. Give
       only the distribution name, with no version and no extras. A cocotb simulation checks
       these packages in the Python environment selected by ``cocotb-config`` when it starts.
       Other simulations check the Python environment that runs Xeda during planning. A remote
       run installs the versions found in Xeda's local environment on the host (see :doc:`remote`).

.. _sources:

Source files
============

A path string is enough when its extension identifies the type:

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
   * - ``.lib`` / ``.def`` / ``.gds`` / ``.cdl`` / ``.vlt``
     - ``Liberty`` / ``Def`` / ``Gds`` / ``Cdl`` / ``Vlt``

Every source has a type. Xeda matches a suffix exactly as written: it infers no type for
``TOP.VHD``, and the error names ``.vhd``. A source needs an explicit ``type`` when its suffix gives
it none. Xeda infers nothing from ``.json``, ``.cfg`` and ``.config``, because these suffixes are
used for many kinds of file. The error suggests ``JsonNetlist`` for ``.json`` and ``EcpConfig`` for
``.cfg`` and ``.config``. ``.bin``, ``.dcp`` and ``.odb`` each name several of the types of Xeda:
``.bin`` is a ``Bitstream`` or a ``Chipdb``, ``.dcp`` is a ``SynthCheckpoint`` or a
``RoutedCheckpoint``, and ``.odb`` is the OpenROAD database of one stage (``OpenroadDbPlaced``,
``OpenroadDbCts``, ``OpenroadDbGlobalRouted`` or ``OpenroadDbRouted``). A suffix that is not in the table names nothing that Xeda knows. Some types are only ever given this
way: ``JsonNetlist``, ``EcpConfig``, ``VerilogNetlist``, ``GenericVerilogNetlist``, ``VhdlNetlist``, ``FpgaNetlist``,
``FpgaTimingNetlist``, ``SynthCheckpoint``, ``RoutedCheckpoint``, the four ``OpenroadDb`` types, the
four ``OpenroadSdc`` types, ``Chipdb``, ``Data`` and the ``Checkpoint`` and ``Odb``, which no
flow takes. Type names are case-tolerant, and suffixes are not.
``Data`` is for a file with no automatic HDL front end, such as test vectors or the input of a
script. A flow or the design can still read it. An invalid ``type`` is an error that names the
closest types. A ``.v`` file is ``Verilog``. To list a gate-level netlist, write ``{file: "net.v",
type: VerilogNetlist}``, or ``type: GenericVerilogNetlist`` for a netlist of the cells of Yosys.

Xeda types a netlist and a checkpoint by what it is, and not only by its file format. The
following table lists the netlist, checkpoint and database types. Each type has three
properties:

.. list-table::
   :header-rows: 1

   * - Type
     - Container
     - Abstraction
     - Target
     - Meaning
   * - ``VerilogNetlist``
     - verilog
     - mapped
     - standard_cells
     - A Verilog netlist mapped to the cells of a Liberty library.
   * - ``GenericVerilogNetlist``
     - verilog
     - generic
     - none
     - A Verilog netlist of the cells of yosys (gates, LUTs or coarse cells). No Liberty library holds them.
   * - ``JsonNetlist``
     - json
     - mapped
     - fpga_primitives
     - The JSON netlist that the family pass of yosys writes, with the primitives of an FPGA family.
   * - ``Edif``
     - edif
     - mapped
     - fpga_primitives
     - An EDIF netlist of the primitives of an FPGA family.
   * - ``FpgaNetlist``
     - verilog
     - mapped
     - fpga_primitives
     - The functional Verilog netlist of FPGA primitives that Vivado writes.
   * - ``FpgaTimingNetlist``
     - verilog
     - mapped
     - fpga_timing_primitives
     - The timing Verilog netlist of FPGA primitives that Vivado writes. A simulation reads it with an SDF file.
   * - ``SynthCheckpoint``
     - vivado_checkpoint
     - mapped
     - fpga_primitives
     - A Vivado checkpoint after synthesis.
   * - ``RoutedCheckpoint``
     - vivado_checkpoint
     - routed
     - fpga_primitives
     - A Vivado checkpoint after routing.
   * - ``OpenroadDbPlaced``
     - openroad_database
     - placed
     - standard_cells
     - The OpenROAD database after placement.
   * - ``OpenroadDbCts``
     - openroad_database
     - clock_tree
     - standard_cells
     - The OpenROAD database after clock tree synthesis.
   * - ``OpenroadDbGlobalRouted``
     - openroad_database
     - global_routed
     - standard_cells
     - The OpenROAD database after global routing.
   * - ``OpenroadDbRouted``
     - openroad_database
     - routed
     - standard_cells
     - The OpenROAD database of the routed design.
   * - ``VhdlNetlist``
     - vhdl
     - unspecified
     - unspecified
     - A VHDL netlist. No flow declares it.
   * - ``Blif``
     - blif
     - unspecified
     - unspecified
     - A BLIF netlist. No flow declares it.
   * - ``Checkpoint``
     - vivado_checkpoint
     - unspecified
     - unspecified
     - A Vivado checkpoint of no stated stage. No flow takes it.
   * - ``Odb``
     - openroad_database
     - unspecified
     - unspecified
     - An OpenROAD database of no stated stage. No flow takes it.

The container is the file format that a reader parses. The abstraction is what the cells are, or
the stage of a physical result. The target is the cell library that the file instantiates. The
concrete library or family stays in the shared settings: ``platform``, ``corner`` and ``liberty``
name the standard cells, and ``fpga`` names the FPGA family. The value ``unspecified`` marks a
type that no flow declares.

Verilog has no suffix of its own for a netlist type, so a listed one needs its ``type``. A
``VerilogNetlist`` is a gate-level netlist of the cells of a Liberty library. A
``GenericVerilogNetlist`` is a netlist of the cells of Yosys, and no flow takes it as an input. An
``FpgaNetlist`` is a Verilog netlist of FPGA primitives, as Vivado writes it, and
``vivado_postsynth_sim`` simulates it. An ``FpgaTimingNetlist`` is the timing netlist of the same
design, and that flow simulates it with an SDF file. Vivado writes a checkpoint (``.dcp``) after
synthesis (``SynthCheckpoint``) and after routing (``RoutedCheckpoint``), and ``vivado_power``
reports power only on the routed one. The ``.dcp`` suffix therefore needs its ``type``, and Xeda
never takes a netlist or checkpoint of one type in place of the other. A Vivado netlist that you
list as ``VerilogNetlist`` is taken by no Vivado flow. The type ``Checkpoint``, for a checkpoint
of no stated stage, is taken by no flow, so a source with that type feeds nothing.

``GenericVerilogNetlist`` as the only source of a design gives the error that the flow reads none of the design's sources.
Among other readable sources, Xeda ignores it, as it ignores any source type that no selected
flow takes.

.. _listed-netlist:

A netlist that you list as a source is your statement of what it is. The type names the container
and the kind of cells: a ``VerilogNetlist`` has the cells of a Liberty library, and a
``JsonNetlist`` has the primitives of an FPGA family. Xeda does not parse the netlist to find its
cells or its target. It does not compare the netlist with ``platform``, ``liberty`` or ``fpga``.
The listed file replaces the flow that would make it, so no setting of that flow agrees with it.
The tool that reads the netlist checks the content and reports a cell that it does not know. The
same rule holds for a ``.dcp`` or ``.odb`` source.

Two more limits apply:

- The ``liberty`` agreement covers the Liberty files that ``yosys`` maps to. It does not cover the
  cells that other settings of ``yosys`` instantiate: ``sequential.dff_liberty`` (a Liberty file
  that ``dfflibmap`` reads in place of the shared list), and the maps ``latch_map``,
  ``clockgate_map``, ``adder_map``, ``techmap_files``, ``dff_map`` and ``memory.libmap``.
  OpenROAD reports a cell that it does not find when it links the design.
- Xeda appends the ``synth_flags`` of ``yosys_fpga`` to the family pass as given. A flag that
  stops the pass early (such as ``-run begin:coarse``) gives a JSON netlist with unmapped cells.
  Xeda does not parse flags, and ``nextpnr`` reports the unknown cells. The ``JsonNetlist`` type
  means the JSON netlist that the family pass writes under normal settings.

Xeda follows ``include_file(...)`` in a Liberty file when it reads the time unit and the facts of
a platform, as OpenSTA does. OpenSTA takes the text between the parentheses as the name, as written,
and opens it in its working directory. A name therefore must be an absolute path, without quotes
and without spaces just inside the parentheses. A relative name and a quoted name cannot be opened by
OpenSTA, and Xeda refuses them. Each included file is an input of the run, like the Liberty file
that includes it. The Liberty reader of Yosys does not read ``include_file``, so ``yosys`` refuses a
Liberty file that includes another, and only the OpenROAD flows can use one. A remote run ships
each file that a ``liberty`` file includes to a place of its own on the remote. It writes copies of
the Liberty files that name each included file by that place.

An OpenROAD stage writes a database (``.odb``) and the SDC file that goes with it. Each stage has
its own type for both. For example, the clock tree stage writes ``OpenroadDbCts`` and
``OpenroadSdcCts``. Clock tree synthesis changes the SDC file, so an ``.sdc`` file of your design
never stands in for the SDC file of a stage. To start a chain at a later stage, list both files of
that stage with their types. A listed database with the SDC file of a flow is an error, and so is
the reverse: both files come from your sources, or both from one flow.

A source of a later stage stands in for the flows that would build it. A ``JsonNetlist`` skips
synthesis for ``nextpnr``. An ``Edif`` netlist skips it for ``vivado_impl``. ``fpga_pack`` packs a
``Fasm``, ``EcpConfig`` or ``IceAsc`` configuration without placing, and ``openfpgaloader``
programs a ``Bitstream`` with nothing built:

.. code-block:: yaml

    rtl:
      sources:
        - "top.v"
        - {file: "build/top.bit", type: Bitstream}   # or just "build/top.bit": typed by its suffix
      top: top

A flow reads only the source types that it declares. For example, ``vivado_synth`` passes over an
``Lpf`` source. Before the flow runs, Xeda refuses a source in a language that the flow cannot
read: a Bluespec source for ``vivado_synth``, a VHDL source for ``verilator``, or a SystemVerilog
source for ``ghdl_sim`` or ``nvc``. Xeda also refuses a design with no source that the flow
reads. A design whose only netlist is an ``.edf`` file gives ``yosys_fpga``, ``vivado_synth`` and
every simulator nothing to read. The error names the sources that the design lists, with their
types, and the types that the flow reads. It also stops ``nextpnr``, because the ``yosys_fpga``
synthesis that ``nextpnr`` would plan has no source to read. When nothing else in the plan needs
that synthesis, the error adds that a ``JsonNetlist`` source would replace it.

One source of a type that the flow reads is enough, a constraint file included. A design with an
``.edf`` file and an ``.xdc`` file therefore still plans for ``vivado_synth``, which reads only
the constraints. A flow that reads the testbench (every simulator, ``bsc`` and
``vivado_project``) checks ``tb.sources`` as well. The other flows check ``rtl.sources``. Xeda
never refuses a C or C++ file, because it can be a driver or a helper for another flow.
``nextpnr``, ``fpga_pack``, ``openfpgaloader``, ``vivado_impl`` and ``vivado_power``
read no design source themselves. Each takes what it reads through its declared inputs, from a
producer or from a typed source. ``nextpnr`` takes the ``Sdc`` files of the design that way,
and ``vivado_impl`` takes its ``Xdc`` and ``Sdc`` files. Header files reach the include and search
paths.

.. note::
   The ``bsc`` flow compiles BH (Bluespec Classic) only from ``.bs`` files. It rejects a ``.bh``
   source with a settings error that names the file to rename.

When the suffix is not enough, write a table in place of the string:

.. code-block:: yaml

    rtl:
      sources:
        - "pkg.vhdl"
        - {file: "legacy.v", type: SystemVerilog}
        - {path: "generated/top.v"}   # not checked for existence

A source table has either ``file`` or ``path``, and optionally a ``type``. Any other key is an
error. In particular, a source has no ``standard`` or ``variant`` of its own. The VHDL standard is
one setting of the whole design (see `language`_), because GHDL and NVC compile all VHDL sources
of a run under one standard.

A ``file`` must exist, and must be a file and not a directory, when Xeda loads the design. A
``path`` is not checked, so it can name a source that a generator produces later. Each source adds
its content hash and its path relative to the design root to the design hash that decides whether a
result is up to date. The path and the type of the source, but not its content, are also part of
the identity of a run that reads the source (the run directory name with ``--hashed-run-dirs``). A tool
can find another file from the location of a source. For example, a Verilog ``include`` searches
the directory of the including file first, and a TCL script can use ``[file dirname [info
script]]``. So moving the sources around inside the design makes it a different design. Moving the
whole design never does.

A source that contains ``*`` is a pattern (``"src/*.vhd"``). ``*`` is the only pattern character.
``?``, ``[`` and ``]`` are ordinary characters of a file name, so ``"rtl/fifo[1].v"`` names exactly
that file, and ``"rtl/fifo[1]_*.v"`` matches ``fifo[1]_a.v`` but not ``fifo1_a.v``. Xeda inserts
the matches of a pattern in sorted order, so the compilation order and the hash of the design do
not depend on the file system. Only files match: a directory whose name fits the pattern is
skipped. A variable in a pattern (``$DESIGN_ROOT`` or any environment variable) stands for the
place it names, so a ``*`` in its value is not pattern syntax. A pattern that matches no file is
an error, so a mistyped pattern cannot quietly add nothing. The ``sources`` of a generator follow
the same rules.

Xeda gives each tool a file by its real name. Yosys would expand ``fifo[1].v`` as a pattern of its
own and read ``fifo1.v``, so Xeda escapes the name for it. For TCL-scripted tools, Xeda passes
every source, constraint file and script file that you name as one literal word. A space, ``[`` or
``$`` in a name is therefore never split, substituted or run. Such names are tested end to end
with Yosys and with the ``read_verilog``, ``read_vhdl`` and ``read_xdc`` commands of Vivado.
Vivado's ``add_files`` refuses a name that contains ``[``, ``]`` or ``$``. ``vivado_project`` uses
``add_files`` for every file, and ``vivado_synth`` uses it for memory files, ``xdc_files`` and
``tcl_files``. Rename such a file for Vivado.

.. _generators:

Generated sources
=================

A design can have its sources produced before any flow runs. ``rtl.generator`` has three forms, and
the design keeps them all as one table:

* A command line for a shell becomes ``{shell: <text>}``. Xeda runs the text with
  ``/bin/sh -c`` and expands nothing in it.
* A list of arguments becomes ``{executable: <first item>, args: [<other items>]}``. No shell
  reads it.
* A table stays as it is.

.. code-block:: yaml

    # generator_shell.yaml
    name: generator_shell
    rtl:
      sources:
        - gen/top.v
      top: top
      generator: echo generating gen/top.v
    flows:
      yosys_fpga:
        fpga:
          part: LFE5U-25F-6BG381C

.. code-block:: yaml

    # generator_list.yaml
    name: generator_list
    rtl:
      sources:
        - gen/top.v
      top: top
      generator: [echo, generating, gen/top.v]
    flows:
      yosys_fpga:
        fpga:
          part: LFE5U-25F-6BG381C

The table form has the most options. Its keys ``shell``, ``command`` and ``executable`` name what
runs, and one table has only one of them. ``shell`` is a command line for a shell, ``command`` is
a program with its arguments in one text, and ``executable`` is a program that takes its
``args`` as a list. A table with ``shell`` and another of these keys, or with ``shell`` and
``args``, is an error that names the keys. Every generator finds ``DESIGN_ROOT`` in its
environment, one with ``shell`` too:

.. code-block:: yaml

    # generator_table.yaml
    name: generator_table
    rtl:
      sources:
        - gen/top.v
      top: top
      generator:
        shell: echo generating gen/top.v
        always_runs: true
    flows:
      yosys_fpga:
        fpga:
          part: LFE5U-25F-6BG381C

A table can also say what the generator reads:

.. code-block:: yaml

    name: generated
    rtl:
      sources:
        - "gen/top.v"
      top: top
      generator:
        executable: python3
        args: ["soc.py", "--build-dir", "gen"]
        sources: ["soc.py", "../litex/litex", "../migen/migen"]

The designs above run ``echo`` when they load. A real generator is the program that writes the
sources.

The generator runs while Xeda loads the design. It runs again only when something that it reads
or produced has changed. Xeda judges this by content and never by modification time.

A generator may write sources of any language that a flow reads or converts. If it writes Bluespec,
``bsc`` converts the generated files as it converts hand-written ones (see
:ref:`converted-designs`). A change to what the generator reads changes the Bluespec, and
``bsc`` runs again. Each flow after ``bsc`` runs again only if one of its own inputs changed, such
as the Verilog that ``bsc`` writes or a source that ``bsc`` passes through.

``sources``
    The files that the generator reads, named as the ``sources`` of a design are (patterns
    included). Each one must exist, because Xeda reads its content. A ``touch``, a ``chmod`` or a
    branch round trip of a file is not a change. An edit that restores the old timestamp is a
    change. A source that names a directory (a Chisel ``src/main/scala``, a directory of
    templates) counts as every file in it, so a file edited or added there makes the generator
    run again. A path may lie outside the design root. The example names the library trees of
    editable ``litex`` and ``migen`` clones next to the design.

A generator is an external tool, and Xeda assumes nothing about its language or environment. List
what it reads as ``sources``, including directories outside the design such as an editable LiteX
clone. Xeda cannot see anything else, for example a package upgrade in a virtual environment. For
that, use ``--rebuild-all`` or ``always_runs``. Xeda digests a directory as it is on disk. A file
that the generator writes there while it runs (Python's ``__pycache__``, for one) changes what the
generator read. The first generation then keeps no record. The next one finds the directory as the
first run left it, and keeps a record.

Xeda also identifies the selected executable by its content, including the ``mill`` or ``bloop``
command that a Chisel generator selects. It resolves the executable without running it, and the
same helper builds the command line that starts it. The generator script itself still belongs in
``sources``. The identity does not cover indirect tools and dependencies.

After a successful run, Xeda keeps a record only if the declared file inputs and the executable
still have the identity they had when the run started. The record holds digests of what the
generator left. These digests do not prove that an untracked environment, an indirect tool or an
external input would produce the same bytes in another run. Declare those inputs where you can,
or use ``always_runs`` or ``--rebuild-all``.

``generated_sources``
    Which of ``rtl.sources`` the generator writes, when it does not write all of them (or a
    directory that holds some of them). When it is left out, Xeda judges every declared source,
    so editing a hand-written source also runs the generator. An entry that is none of
    ``rtl.sources`` is an error, because the source that the generator really writes would
    otherwise go unjudged.

``always_runs``
    Run the generator on every load. Use it for a generator whose inputs cannot be judged: they
    are not files, or they cannot be listed. A generator that declares no ``sources`` runs on
    every load anyway, because nothing says when it is out of date.

Xeda keeps the record of a generation (the digest of every source that the generator left) under
``<run root>/.cache/generators/``, beside the chip databases and the other files of Xeda. The
tree of the design holds nothing of Xeda's. A design that is loaded with no run root in sight,
such as a ``Design`` built by hand outside ``xeda run``, has nowhere to keep the record, so its
generator runs on every load. Xeda takes this direction wherever it cannot prove that something is
up to date. Xeda makes the run root only to write the record after a successful generation, and
never to look one up. A generator that fails therefore leaves no run root behind. ``xeda run
--dry-run`` creates nothing and reads an existing record. It refuses to plan a design that would
have to generate: a design with a stale record and, with ``--rebuild-all`` or ``--clean``, any
generated design, because that launch would run its generator.

On POSIX, a read-only lock on the resolved design-root directory serializes the generators of that
directory, including the first run and runs with different input identities. The lock creates no
extra file and does not create the run root early. A lock on the record of each identity still
protects that record. These locks do not coordinate different design roots that write to the same
external output. Windows has no interprocess lock. Planning takes no lock.

Before a generator runs, Xeda judges the request on the design as the design file declares it. The
generated sources are in ``rtl.sources`` with their types, whether they exist yet or not. Xeda
refuses at once, and does not run the generator, what these declarations already rule out: a flow
that reads none of the design's sources, a source in a language that the flow cannot read, a
testbench without its top module, a setting that the flow does not have, and a run directory in
which the flow cannot work. This holds for ``xeda run``, ``--dry-run``, ``--remote`` and ``xeda
dse``. With ``--remote`` the build happens on the remote host, so Xeda does not judge the run
directory.

Xeda also checks, at this point, a setting that names a file that Xeda reads to check the setting
(``custom_boards_file`` or a platform file). That file must exist before the generator runs. The
generator may rewrite the file, but it cannot be the only step that creates it.

Some designs say too little before the generator has run. Xeda then judges the request after the
generator:

* a design with a pattern in ``rtl.sources`` (such as ``gen/*.v``), because the pattern stands for
  the files that exist once the generator has run;
* a design with a Git dependency, because the dependency can bring sources, a top module and a
  testbench;
* a design that does not load before its generator runs because files are missing. It lists, as a
  file that must exist, another file that the generator writes, such as a testbench or the design
  file of a local dependency.

A design that also has any other error, such as a wrong value, is refused at once, and its
generator does not run.

A generator written as a command line or as a list declares nothing that it reads, so it runs on
every load. Write it as a table with ``sources`` to have it judged. A table with ``shell`` may
list ``sources`` too.

``--rebuild-all`` (and ``--clean``, which implies it) runs the generator whatever its record
says, as it runs every flow of the launch, and records what that generation leaves. A ``touch``
does not force a regeneration, and ``--rebuild-all`` does. Use it when something that Xeda cannot
see has changed, such as an environment variable that the generator reads or anything that its
``sources`` do not name.

With ``--remote``, the generator runs on your machine, when Xeda loads the design, and Xeda sends
the generated sources to the host. ``--rebuild-all`` and ``--clean`` force the generator here, and
they also act on the run directories of the host.

Writing into the tree of the design is the purpose of a generator, so what it writes there is its
own business (a LiteX build directory, for example). Xeda itself writes nothing outside its run
root while it loads a design. The record of the generation is the only thing that Xeda keeps, and
it keeps it in the run root.

.. _language:

``language``: standards
=======================

.. code-block:: yaml

    language.vhdl.standard: 2008   # or 93, 2019, ...
    language.verilog.standard: 2005

A flow accepts a two-digit standard (``08``)
and a four-digit standard (``2008``). The standard applies to every source of the language. A
source cannot have another standard.

.. _flows:

``flows``: settings for individual flows
========================================

Settings for one flow go under ``flows.<flow_name>``. They apply only when that flow runs, so one
design file can carry the settings of several flows:

.. code-block:: yaml

    flows:
      vivado_synth:
        fpga.part: xc7a100tftg256-2L
        clocks.main_clock.period: 5 ns
      yosys:
        platform: nangate45
        clocks.main_clock.period: 10 ns
      ghdl_sim:
        stop_time: 100us

``xeda list-settings <flow>`` lists what a flow accepts. Xeda rejects an unknown setting of a
flow, so a typo is an error and not silently ignored.

A flow section can also hold ``inputs``. It says where a flow that declares file inputs reads each
of them from: ``<producer flow>`` or ``<producer flow>.<output>``, or a list of these for an input
that takes several. ``inputs`` is saved wiring that the resolver reads. It is not one of the
settings of the flow: ``xeda list-settings`` and the ``settings.json`` of the flow do not show it.
It names an output of a flow and never a file. An external file is a typed source. Xeda uses a
saved binding before a typed source and before the default producer of the input:

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

The same section in a project file means the same, with a lower rank than the design's. :ref:`Flow
chains and input bindings <flow-chains>` describes ``xeda run a+b+c`` chains and bindings. A chain
given on the command line replaces a saved binding of the same input.

.. _targets:

Targets
========

One design file can describe the design for several boards. Each entry of ``targets`` is an
overlay on the design. It takes the keys of the design file, merged over them, and ``--target
NAME`` selects one. Without ``--target``, Xeda uses the design itself. So the top level of the file
can hold the settings that suit most flows (simulation, ASIC, Vivado), and the targets can hold the
choices that conflict on one flow, such as the FPGA family of ``yosys_fpga``.

.. code-block:: yaml

    name: knight
    rtl:
      sources:
        - "Knight.bsv"
        - "por_sync.v"
      top: mkKnight
      clocks: [{port: CLK}]
    targets:
      arty:
        rtl.sources: ["arty.xdc"]
        rtl.defines.CLK_HZ: 100000000
        flows:
          nextpnr:
            board: arty_a7_100t
      ulx3s:
        rtl.sources: ["ulx3s.lpf"]
        rtl.defines.CLK_HZ: 25000000
        flows:
          nextpnr:
            board: ulx3s_85f

.. code-block:: bash

    xeda run bsc_sim knight.yaml                   # the design itself, with no target
    xeda run nextpnr knight.yaml --target ulx3s    # the design with the target ulx3s

* A target takes ``rtl``, ``tb``, ``flows`` and every other key of a design file. In a target
  they mean exactly what they mean at the top of the file. ``name``, ``targets`` and
  ``design_root`` belong to the design alone. An unknown key is an error, as it is in the design.
* Mappings merge key by key, at every depth (``defines``, ``parameters`` and each
  ``flows.<flow>`` section). The ``sources`` of ``rtl`` and ``tb`` are appended after the
  design's, in order. Any other list replaces the design's. Xeda merges each ``flows.<flow>``
  section by the rules of the settings layers of that flow (see :doc:`flows`). A null or empty ``flows``
  table, or flow section, adds nothing: the design's section stays as it is. Set a key to change
  it.
* A short form means the table it stands for. When the design and the target write one key in
  different forms, Xeda reads each as that table before they merge. ``parameters`` as a list of
  ``{name, value}`` objects is the mapping they give. ``language.vhdl: "08"`` is
  ``language.vhdl: {standard: "08"}``. ``tb.cocotb: true`` is ``tb.cocotb: {}``, so it keeps the
  ``module`` of the design, and ``false`` means no cocotb, which replaces the table of the design.
* The ``rtl.clocks`` of a target, in any form, replace the list of clocks of the design.
  ``clocks: null`` and an empty table in the list mean no clock, in the design and in a target. A
  clock that is neither text nor a table (``clocks: [0]``) is an error at the key as written, in a
  target as well (at ``targets.<name>.rtl.clocks``). Xeda checks a target that is not selected
  only for the form of its values. A removed key (``clock_port``, or a key of ``rtl`` written at
  the top level) is an error in the design, in a target (selected or not) and in
  ``--design-overrides``. The error names the key to write.
* A value that is no table, and no short form of one, where a table is expected is an error,
  whichever target you select. Examples are ``tb: 3`` and ``keep: 3`` in ``rtl.attributes``,
  whose entries are tables. A table that a target writes over the value does not hide the error.
  Xeda checks the ``flows`` table of the design and the ``flows`` table of every target as well,
  the latter at ``targets.<name>.flows``. It checks the overlay of every target, selected or not,
  at ``targets.<name>.<key>``.
* A target overrides the design. It is the design author saying "for this board, use these
  values", so where the design and the target write the same key, the value of the target wins,
  key by key. The ``flows.nextpnr.board`` of a target replaces the ``flows.nextpnr.board`` of the
  design without an error, and a ``seed`` that the target does not write stays the design's. A
  setting can come from these places, lowest first: the defaults of the flow, the project file,
  the design file, the target, the command line (``-s``) and the API. So ``-s seed=9`` and an API
  override still win over a target. The loader records the name of the selected target, and a
  design override cannot set or change it. Two flows of one run that disagree about a setting
  they share, such as ``yosys_fpga`` and ``nextpnr`` that name different boards, are a different
  case, and that is an error.
* A path in a target resolves against the design root, like the paths of the design.
* A target is optional. Without ``--target``, Xeda uses the design itself, with no overlay, whatever
  the number of targets. A lone target is not selected automatically. ``--target NAME`` selects
  that target. An unknown name is an error, and the error lists the targets. ``--target`` on a
  design without ``targets`` is an error. From Python, write
  ``Design.from_file(path, target="ulx3s")``. ``--design-overrides`` are an overlay on the
  selected design, like a target, and merge by the same rules: a short form means
  its table, and an override does not hide a mistake in the design. One rule differs. Only a
  target adds its ``sources`` to the design's, because its job is to add the files of a board. A
  ``sources`` override (``rtl.sources`` or ``tb.sources``) replaces the whole list, the sources of
  the selected target included, as an override of any other list does. The designs of a project
  take overrides in the same way.
* A target name starts with a letter and holds only letters, digits, ``_`` and ``-``. It is not
  the name of a flow. There is one spelling, ``targets.<name>``. A design file has no
  ``target`` key.
* A selected target yields an ordinary design, with the same hash and results as the file written
  out flat with the keys of the target applied by hand. The name of the target is reported
  (``target`` in the ``--json`` documents, ``null`` without one), and it is no part of the
  identity of the design or of a run.
* A selected target has run directories of its own, ``<run root>/<design>/<target>/<flow>``
  (``<flow>_<hash>`` with ``--hashed-run-dirs``). A run without ``--target`` keeps
  ``<design>/<flow>``, whether or not the design has targets. Two targets that
  yield the same design therefore build separately, and each stays up to date, although they have
  the same hashes. A launch for a target leaves the run without a target alone.
  ``xeda scrub FLOW DESIGN --target NAME`` removes only the runs of that target.
  ``xeda scrub FLOW DESIGN`` removes the runs of every target and the runs without a target too.

``xeda run`` and ``xeda dse`` take ``--target``. The designs in a project file take targets in the
same way.

Two limits apply:

* ``board``, ``fpga`` and ``custom_boards_file`` cannot be written once at the top level of a
  target. Xeda refuses them and names where to write them: they are settings of each flow, under
  ``flows.<flow>`` of the target. A clock constraint goes there too, as ``flows.<flow>.clock``.
* A target cannot inherit from another target.

Environment variables in paths
==============================

A setting that holds a path expands ``$PWD``, ``$DESIGN_ROOT`` and ``$DESIGN_DIR``. This keeps a
design file portable across machines:

.. code-block:: yaml

    flows:
      dc:
        target_libraries: ["$DESIGN_ROOT/lib/SAED90/saed90nm_typ_ht.db"]

Design sources expand environment variables too, except ``$PWD``. In a source, ``$DESIGN_ROOT``
and ``$DESIGN_DIR`` name the design root, the directory that a relative source resolves against.
So ``"$DESIGN_ROOT/src/*.vhd"`` and ``"src/*.vhd"`` are the same sources.

Several designs in one project
==============================

A ``xedaproject.yaml`` can hold several designs, plus top-level ``flows`` settings that Xeda merges
into each design. ``xedaproject.yml`` and ``xedaproject.toml`` are also accepted. Automatic
discovery needs exactly one of these names in the directory. Select a design with
``--design-name``. When the project has several designs and you give no name, Xeda asks you to
choose one.

A project file has the keys ``designs`` and ``flows``. Any other key at its root is an error that
names the accepted keys.
