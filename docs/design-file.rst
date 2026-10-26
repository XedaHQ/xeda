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
(or a generator table) that produces sources. Xeda runs it in the launch, before the first flow.
See :ref:`generators`.

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

Write one table in one spelling inside a mapping. ``clock.port: clk`` and a ``clock:`` block together are an error. So are
``fpga.part: P`` and an ``fpga:`` block in the same mapping that names both spellings, in whichever order they come.
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
       Planning reads them, and it fetches a Git repository into the run root. See
       `Git dependencies`_.
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
   * - ``clock``
     - no
     - One clock: ``clock.port: clk`` or ``clock: clk``.
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
     - A command or a generator table that produces sources, for example a Chisel elaboration.
       Xeda runs it in the launch, before the first flow, and runs it again only when something
       it reads or produced has changed. See :ref:`generators`.

A clock in ``rtl`` is logical: it names a clock port of the design. The physical period or
frequency is a flow setting, because it is a constraint of one build and not a property of the
design. A design with one clock usually needs only this:

.. code-block:: yaml

    rtl:
      clock.port: clk

In each flow, set ``clock.period`` or ``clock.freq`` with a unit: ``clock.period: 5.5 ns`` or
``clock.freq: 200 MHz``. A number without a unit is an error. Units are case-sensitive, as in SI:
``200 mhz`` is an error that names ``MHz``. See :ref:`quantities` for the kinds and the units.

Xeda refuses the design key ``clock_port`` and the flow setting ``clock_period``, and names the
replacement: write ``clock: <port>`` in the design and ``clock.period`` in a flow.

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
``path`` is not checked, so it can name a file that a tool writes later. A source that a generator
writes is listed in ``rtl.sources`` too (see :ref:`generators`). Each source adds its content hash and its path relative to the design root to the design hash that decides whether a
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
an error, so a mistyped pattern cannot quietly add nothing. A pattern of a source that a generator
writes is checked after the generator has run (see :ref:`generators`). The ``sources`` of a
generator follow the same rules.

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

A design can have its sources produced by a program, its generator. The generator runs in the
launch, after Xeda has checked everything that it can check without the sources. Loading a design
runs nothing and writes nothing.

``rtl.generator`` has three forms, and the design keeps them all as one table:

* A command line for a shell becomes ``{shell: <text>}``. Xeda runs the text with
  ``/bin/sh -c``, and the shell expands the variables in it.
* A list of arguments becomes ``{executable: <first item>, args: [<other items>]}``. No shell
  reads it.
* A table stays as it is.

.. code-block:: yaml

    # generator_shell.yaml
    name: generator_shell
    rtl:
      sources:
        - top.v
      top: top
      generator: printf 'module top; endmodule\n' > top.v
    flows:
      yosys_fpga:
        fpga:
          part: LFE5U-25F-6BG381C

.. code-block:: yaml

    # generator_list.yaml
    name: generator_list
    rtl:
      sources:
        - top.v
      top: top
      generator: [touch, top.v]
    flows:
      yosys_fpga:
        fpga:
          part: LFE5U-25F-6BG381C

The table form has the most options. Its keys ``shell``, ``command`` and ``executable`` name what
runs, and one table has only one of them. ``shell`` is a command line for a shell, ``command`` is
a program with its arguments in one text, and ``executable`` is a program that takes its
``args`` as a list. A table with ``shell`` and another of these keys, or with ``shell`` and
``args``, is an error that names the keys.

.. code-block:: yaml

    # generator_table.yaml
    name: generator_table
    rtl:
      sources:
        - top.v
      top: top
      generator:
        shell: printf 'module top; endmodule\n' > top.v
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
        args: ["$DESIGN_ROOT/soc.py", "--build-dir", "$XEDA_GENERATED/gen"]
        sources: ["soc.py", "../litex/litex", "../migen/migen"]

Where the generator runs and what it writes
-------------------------------------------

The generator writes into its storage, a directory under the run root:
``<run root>/.generated/<design>/<key>/``. Xeda writes nothing into the tree of the design. (A
tool that a Chisel generator starts in the design root, ``mill`` or ``bloop``, writes its own build
directories there.) The key
names one configuration (see below), so two variants of a design keep their own generated files
side by side. Xeda sets two variables in the environment of every generator:

``DESIGN_ROOT``
    The design root.

``XEDA_GENERATED``
    The absolute path of the storage.

The generator starts in the storage, so a relative path that it writes is a path in its output.
The words after the executable (``args``, or the words of ``command`` after the first one) expand
``$DESIGN_ROOT`` and ``$XEDA_GENERATED``. No other variable expands, and the executable does not.
A shell command line (``shell``) reads both variables from its environment. A relative executable,
and a relative entry of ``PATH``, resolve against the design root, or against ``cwd`` when the
recipe has one. A generator that must run in another directory, for example because a build tool
finds its project there, sets ``cwd`` (a directory below the design root; an absolute ``cwd``, or
one that leaves the root, is refused when the design loads). A Chisel generator starts in the
design root unless ``cwd`` says otherwise, because ``mill`` and ``bloop`` find their build files in their working directory. Pass
the storage to it with ``args``:

.. code-block:: yaml

    name: generator_chisel
    rtl:
      sources:
        - top.v
      top: top
      generator:
        class: chisel
        project: core
        args: ["--target-dir", "$XEDA_GENERATED"]
        sources: ["build.sc", "core"]

``env`` is the whole environment of the generator when the recipe has one, and the generator then
inherits nothing. A recipe may not set ``DESIGN_ROOT`` or ``XEDA_GENERATED`` in ``env``.

The sources that a generator writes
-----------------------------------

The entries of ``rtl.sources`` that the generator writes are names in its output. A name is
relative to the output, and may be a pattern: ``gen/top.v``, ``gen/*.v``. Xeda does not expand a
generated pattern and does not need the file to exist, when it loads the design. A generated
pattern takes its type from its extension, as every source does (``gen/*.v`` is Verilog). A pattern
whose extension does not say what its files are needs a ``type``, and the error shows the form:

.. code-block:: yaml

    rtl:
      sources:
        - {path: "gen/*", type: Verilog}

An entry is wholly the generator's or wholly written by hand. ``generated_sources`` says which are
the generator's, when it does not write all of them:

``generated_sources``
    The entries of ``rtl.sources`` that the generator writes. An item names an entry, a pattern, or
    a directory that holds some entries. When the list is empty, every entry of ``rtl.sources`` is
    the generator's. A pattern that would match both a generated and a written file is an error,
    and so is an item that no entry of ``rtl.sources`` relates to. Only ``rtl.sources`` can be
    generated: a file of ``tb.sources`` must exist when the design loads.

After a generation, each generated entry must match at least one file in the output. An entry
that matches none fails the node that reads it, with the code ``missing_generated`` and a message
that names the entry. The flows see the files that the entries match, in sorted order, in the
place of the entries. The design that you gave stays as it was.

When the generator runs
-----------------------

Xeda runs the generator in the launch, after it has refused everything that the declarations
already rule out: a flow that reads none of the design's sources, a source in a language that the
flow cannot read, a testbench without its top module, a setting that the flow does not have, and a
run directory in which the flow cannot work. The generator then runs before the first flow of the
launch, once for the whole launch.

Xeda runs it only when it must. The record ``generation.yaml`` in the storage holds the digest of
every generated file that the last generation left. The generator runs again only when:

* the storage has no record of this key, or a generated file is gone or differs from the record;
* the digest of the environment that the generator inherited differs from the record (below);
* the generator is one that cannot be judged (``always_runs``, or no ``sources``);
* the launch rebuilds everything (``--rebuild-all``, or ``--clean``, which implies it).

Xeda judges this by content and never by modification time. A ``touch``, a ``chmod`` or a branch
round trip of a file is not a change. An edit that restores the old timestamp is a change.

The key is 16 hexadecimal characters of the hash of: the recipe (its ``env`` included), the whole
configuration of the design without content (``rtl`` and ``tb`` with their metadata and the
ordered, typed entries of their sources with the owner of each, and the language), the names and
content digests of the files that the generator reads, and the executable. It does not hold the
bytes that the generator writes, the files that a pattern matches, a destination, the place of the
run root, the place of the design, or the target. A change to any part of the key is another
configuration, and so another storage.

``sources``
    The files that the generator reads, named as the ``sources`` of a design are (patterns
    included). Each one must exist, because Xeda reads its content. A source that names a
    directory (a Chisel ``src/main/scala``, a directory of templates) counts as every file in it,
    so a file edited or added there makes the generator run again. A path may lie outside the
    design root. The example names the library trees of editable ``litex`` and ``migen`` clones
    next to the design.

``always_runs``
    Run the generator in every launch. Use it for a generator whose inputs cannot be judged: they
    are not files, or they cannot be listed. A generator that declares no ``sources`` runs in
    every launch anyway, because nothing says when it is out of date.

A generator is an external tool, and Xeda assumes nothing about its language or environment. List
what it reads as ``sources``, including directories outside the design such as an editable LiteX
clone. Xeda cannot see anything else, for example a package upgrade in a virtual environment. For
that, use ``--rebuild-all`` or ``always_runs``. Xeda digests a directory as it is on disk. A file
that the generator writes there while it runs (Python's ``__pycache__``, for one) changes what the
generator read. Xeda then keeps no record of that run and runs the generator again. The second run
finds the directory as the first run left it, and keeps a record.

Xeda also identifies the selected executable by its content, including the ``mill`` or ``bloop``
command that a Chisel generator selects. It resolves the executable without running it, and the
same helper builds the command line that starts it. The generator script itself still belongs in
``sources``. The identity does not cover indirect tools and dependencies.

The inherited environment is evidence
-------------------------------------

A generator inherits the environment of the launch, unless its recipe has an ``env``. Xeda keeps
the digest of that environment, and the sorted names of the variables in it, in the record, and
never a value. A change of the environment makes Xeda run the generator again in the same
storage, because the key does not hold the environment. The digest leaves out the variables of a
shell session, a terminal and a login (``PWD``, ``OLDPWD``, ``SHLVL``, ``_``, ``COLUMNS``,
``LINES``, the session and agent variables of the terminal, ``tmux``, ``screen`` and SSH, and the
variables of the Xeda command line), and the two variables that Xeda sets. The log says which
variable was added or removed.

Planning
--------

``xeda run --dry-run`` and the ``plan`` calls run no generator, create nothing and write nothing,
also in a design tree that is read-only. They read the record of the storage. A flow that reads
generated sources is ``unknown`` in a dry run, with the reason "the generator has not run", until
the storage is current. With ``--rebuild-all`` or ``--clean`` it is ``stale``.

If the generator fails, the launch fails with the error code ``generator_failed``, and every flow
of the launch is ``skipped``. Xeda leaves no storage behind.

Concurrency
-----------

A launch holds a lock of a storage while it generates, and a shared lease of every storage until
it has delivered its outputs. A second launch of the same configuration waits, finds the record
and generates nothing. A launch that regenerates (``--rebuild-all``) waits until no other launch
reads the storage. On POSIX the locks are file locks beside the storage. Windows has no
interprocess lock.

A generation holds one exclusive lock and waits for no other lock while it holds it. The leases
come after all the generations, shared, in the order of the paths of the storages. Two launches
therefore never wait for each other, also when they merge the same dependencies in another order
and rebuild them. A launch generates before it takes the lock of any run directory.

Xeda keeps the storage until it is removed. No command removes it yet: ``xeda scrub`` leaves it
alone, and a later design-level scrub of the Python API will remove it. The directory
``<run root>/.generated/`` holds nothing that a launch needs after it has ended, and may be
deleted while no launch runs.

``--remote``
------------

The host runs the whole launch, so the host runs the generator, into the storage under its own
run root. Xeda sends the recipe of the generator, and the files and directories that it declares
in ``sources``, and nothing else of the design tree. A generator that declares no ``sources`` is
refused for ``--remote`` at planning, with an error that names the generator and asks you to
declare its ``sources``. So is a generator with a declared input outside the root of its design
(also for a dependency): place the input below the design root. A local launch has no such limit.
A path inside a word of ``args``, or in an executable, that names a file
on your machine names nothing on the host: name files through ``$DESIGN_ROOT``, and list them in
``sources``. ``--rebuild-all`` and ``--clean`` regenerate on the host.

A generator written as a command line or as a list declares nothing that it reads, so it runs in
every launch. Write it as a table with ``sources`` to have it judged. A table with ``shell`` may
list ``sources`` too.

``--rebuild-all`` (and ``--clean``, which implies it) runs the generator whatever its record
says, as it runs every flow of the launch, and records what that generation leaves. A ``touch``
does not force a regeneration, and ``--rebuild-all`` does. Use it when something that Xeda cannot
see has changed, such as anything that the ``sources`` of the generator do not name.

A generated source is a source of the converted design like any other: if the generator writes
Bluespec, ``bsc`` converts the generated files as it converts hand-written ones (see
:ref:`converted-designs`). A change to what the generator reads changes the Bluespec, and ``bsc``
runs again. Each flow after ``bsc`` runs again only if one of its own inputs changed, such as the
Verilog that ``bsc`` writes or a source that ``bsc`` passes through.

Dependencies with generators
----------------------------

A dependency design (``dependencies``) may have a generator of its own. The launch runs it too,
into a storage of its own, ``<run root>/.generated/<dependency name>/<key>/``, by the same rules:
the key holds the recipe of the dependency with its ``env``, the configuration of the dependency
without content, its declared ``sources`` and its executable, and the inherited environment is
evidence. A dependency of a dependency generates as well. The generators run in this order: a
dependency before the design that depends on it, and the design itself last. The sources that the
generator of a dependency writes keep that dependency as their owner when the design merges them,
so the flows read them from its storage. A pattern or a name that matches nothing fails the node
that reads it with ``missing_generated``, and the message names the generator of the dependency.
``--remote`` refuses a dependency generator that declares no ``sources``, as it refuses the
generator of the design. A design and its dependencies may not declare the same generated name.

.. _git-dependencies:

Git dependencies
----------------

A dependency supplies sources and metadata to the design: its ``rtl.sources`` and ``tb.sources``
join those of the design, and its ``rtl.top``, ``rtl.parameters``, ``rtl.clocks`` and ``tb.top``
fill the ones that the design leaves out. Loading a design reads no dependency and starts no Git
command. Planning reads them: ``xeda run`` (also with ``--dry-run``), ``xeda dse`` and the Python
calls that plan fetch a Git dependency into the run root first, and then merge it. With ``--remote``
the host plans, so the host fetches the dependency into its own run root. This is the one case in
which planning writes to the run root.

.. code-block:: yaml

    dependencies:
      - uri: git+https://github.com/example/lib.git?commit=0123456789abcdef0123456789abcdef01234567#lib.yaml
        rtl:
          pos: -1

A reference can say ``branch`` (a branch or a tag) or ``commit``. Without one, it names the
``HEAD`` of the repository. A ``commit`` is a full hash (40 hexadecimal digits, or 64 in a SHA-256
repository). An abbreviated hash is refused when the design loads.

Xeda keeps the files of a commit in ``<run root>/.dependencies/<key>/``, with the record
``dependency.yaml``. The key names the repository and the commit, so a storage never changes after
Xeda has written it. A fetch writes into a temporary directory and renames it into place, so a
storage is complete or absent. Two launches that need one commit fetch it once.

- A pinned ``commit`` whose storage is there needs no network. To work offline, pin the commit.
- A ``branch``, a tag or the ``HEAD`` is resolved with ``git ls-remote`` at every planning, so a
  branch that has moved is seen. Offline, that fails with ``fetch_failed``, and the message says to
  pin a commit.
- A failed resolution or fetch (an unreachable host, an unknown branch or commit, a design file
  that the repository lacks) stops the request before any tool runs. The error has the code
  ``fetch_failed`` and names the dependency, for example ``dependencies[0]``. Neither the message
  nor any log line or path shows a user name or a password of the URL.
- The commit is no part of the identity of a run. The identity holds the sources of a dependency by
  their path in its checkout. Another commit makes the runs that used the dependency stale: the
  reason is ``dependency <repository> now at <commit>``. The record of a run lists the Git
  dependencies (``repository``, ``reference``, ``commit`` and ``design_file``), and a source from a
  dependency names the dependency it came from.

Xeda never removes dependency storage. It is a cache of fixed content. You can delete
``<run root>/.dependencies/`` by hand while no launch runs. The keys ``clone_dir`` and
``local_cache`` were removed: to use a checkout that you manage, name its design file as a local
dependency.

A local dependency, the path of a design file, is relative to the design that lists it. The file
must exist when planning starts. A generator runs after planning, so it cannot write the design
file of a dependency: list the files that the generator writes as sources of the design instead.

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
        clock.period: 5 ns
      yosys:
        platform: nangate45
        clock.period: 10 ns
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
      clock.port: CLK
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
* A short form means the table it stands for: ``clock: CLK`` is ``clock.port: CLK`` and
  ``fpga: <part>`` is ``fpga.part: <part>``. When the design and the target write one key in
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
