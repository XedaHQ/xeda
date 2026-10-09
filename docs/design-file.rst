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
     - Other designs this one depends on: the path of a design file, or a Git repository written
       ``git+<url>#<design file in the repository>``. A URL without ``git+`` is refused.
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
       needed (e.g. a VHDL configuration alongside an entity). A simulation flow needs it when
       ``tb.sources`` holds a source in a hardware description language (Verilog, SystemVerilog,
       VHDL, Bluespec or Chisel) and the testbench is not cocotb: without it the simulator would
       run ``rtl.top``, which has no stimulus, so the flow refuses the design and names
       ``tb.top``. A simulator that knows what to run without it is not refused: ``ghdl_sim`` finds
       the top of a VHDL testbench itself, and ``verilator`` and ``yosys_sim`` run a C++ driver
       of the design's own, whatever HDL the testbench also holds.
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
``{file: net.v, type: VerilogNetlist}``.

A source of a later stage's type stands in for the flows that would build it: a ``JsonNetlist``
skips synthesis for ``nextpnr``, an ``Edif`` netlist skips it for ``vivado_impl``, a ``Fasm``,
``EcpConfig`` or ``IceAsc`` configuration is packed by ``fpga_pack`` without placing, and a
``Bitstream`` is what ``openfpgaloader`` programs, with nothing built:

.. code-block:: yaml

    rtl:
      sources:
        - top.v
        - {file: build/top.bit, type: Bitstream}   # or just build/top.bit: typed by its suffix
      top: top

Every flow that reads the design's sources declares the types it reads, and reads only those:
``vivado_synth`` passes over an ``Lpf`` source, for example. A source in a language that the flow
cannot read is an error naming it before the flow runs: a Bluespec source for ``vivado_synth``, a
VHDL source for ``verilator``, a SystemVerilog source for ``ghdl_sim`` or ``nvc``. So is a design
with no source that the flow reads: a design whose only netlist is an ``.edf`` file, for example,
gives ``yosys_fpga``, ``vivado_synth`` and every simulator nothing to read. The error
names the sources the design lists, with their types, and the types the flow reads. It also
stops ``nextpnr``, because the ``yosys_fpga`` synthesis that ``nextpnr`` would plan has no source
to read. When nothing else in the plan needs the synthesis, the error adds that a ``JsonNetlist``
source would replace it. The rule has a limit: one source of a type the flow reads is enough, a
constraint file included. An ``.edf`` file together with an ``.xdc`` file therefore still plans
for ``vivado_synth``, which reads only the constraints. A flow that reads the testbench (every
simulator, ``bsc`` and ``vivado_project``) checks ``tb.sources`` too; the others check
``rtl.sources``. A C or C++ file is never refused: it is a driver or a helper that another flow
may read. ``nextpnr``, ``openroad``, ``fpga_pack``, ``openfpgaloader``, ``vivado_impl`` and
``vivado_power`` read no design source themselves: each takes what it reads through its declared
inputs, from a producer or from a typed source (``openroad`` and ``nextpnr`` take the design's
``Sdc`` files that way, and ``vivado_impl`` its ``Xdc`` and ``Sdc`` files). Headers reach
include/search paths; no template turns a source type's name into a tool command.

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
        sources: ["soc.py", "../litex/litex", "../migen/migen"]

The generator runs while the design is loaded, and runs **again only when something it reads or
produced changed**, judged by content, never by a modification time:

``sources``
    the files it reads, as a design's own ``sources`` name them (patterns included). Each one has
    to exist, since its content is read. A ``touch``, a ``chmod`` or a branch round-trip of one
    is not a change; an edit given back its old timestamp is. A source naming a *directory* (a
    Chisel ``src/main/scala``, a directory of templates) counts as every file in it, so a file
    edited inside one, or added to one, runs the generator again. A path may lie outside the
    design root: the example names the library trees of editable ``litex`` and ``migen`` clones
    next to the design.

A generator is an external tool: xeda assumes nothing about its language or its environment.
Name what it reads as ``sources``, including directories outside the design such as an editable
LiteX clone; anything xeda cannot see (a package upgrade in a virtual environment) needs
``--rebuild-all`` or ``always_runs``. A directory is digested as it is on disk, so whatever the
generator writes there while it runs (Python's ``__pycache__``, for one) changes what it read:
the first generation then keeps no record, and the next one, which finds the directory as that
run left it, does.

The selected direct executable is also identified by its content, including the selected
``mill`` or ``bloop`` command for a Chisel generator. Xeda resolves it without running it, and the
same command-selection helper builds the argv used to launch it. The generator script itself still
belongs in ``sources``; indirect tools and dependencies are outside this identity.

After a successful run, Xeda keeps a record only if the declared file inputs and direct executable
still have the identity used to start it. The output digests say what the generator left; they do
not prove that an untracked environment, indirect tool or external input would produce the same
bytes on another run. Declare those inputs where possible or use ``always_runs``/``--rebuild-all``.

``generated_sources``
    which of ``rtl.sources`` the generator writes, when it does not write them all (or a
    directory holding some of them). Left out, every declared source is judged, so editing a
    hand-written one runs the generator too. An entry that is none of ``rtl.sources`` is an
    error: the source the generator really writes would otherwise go unjudged.

``always_runs``
    run it on every load. For a generator whose inputs cannot be judged at all -- they are not
    files, or they cannot be listed. A generator that declares no ``sources`` runs on every
    load anyway, since nothing says when it is out of date.

What xeda keeps about a generation -- the digest of every source it left -- is a record under
``<run root>/.cache/generators/``, beside the chip databases and everything else of xeda's: the
design's own tree holds nothing of xeda's. So a design loaded with no run root in sight (a
``Design`` built by hand, outside ``xeda run``) has nowhere to keep that record, and its
generator runs on every load -- the direction xeda takes wherever it cannot prove something is up
to date. The run root is made to *write* that record, after a generation that succeeded, never to
look one up, so a generator that fails leaves no run root behind; ``xeda run --dry-run`` creates
nothing at all, reads an existing record, and refuses to plan a design that would have to
generate -- one with a stale record, and, with ``--rebuild-all`` or ``--clean``, any generated
design, since that launch would run its generator. On POSIX, a read-only lock on the resolved design-root directory serializes generators
for that same directory, including the first run and different input identities; it creates no
sidecar and does not create the run root early. The per-identity record lock still protects its
record. This does not coordinate different design roots writing to the same external output, and
Windows follows the existing no-interprocess-lock policy. Planning takes no lock.

Before a generator runs, xeda judges the request on the design as the design declares it. The
sources that the generator writes are in ``rtl.sources`` with their types, whether they exist yet
or not, so what these declarations already rule out is refused first, and the generator does not
run: a flow that reads none of the design's sources, a source in a language that the flow cannot
read, a testbench without its top, a setting that the flow does not have, a run directory that the
flow cannot work in. This holds for ``xeda run``, ``--dry-run``, ``--remote`` and ``xeda dse``.
A setting that names a file that xeda reads to check the setting (``custom_boards_file``, a
platform file) is checked at this point too, so that file must exist before the generator runs,
and a generator cannot write it.
Some designs say too little before their generator has run, and xeda then judges the request
after the generator, as it did before: a design with a pattern in ``rtl.sources`` whose suffix
gives no type (``gen/*``), a design with a Git dependency (the dependency can bring sources, a top
and a testbench), and a design that does not load before its generator runs (it lists another
file that the generator writes, such as a testbench, as a file that must exist).

A generator given as a shell command (``generator: "python soc.py"``) or as a list of arguments
declares nothing it reads, so it runs on every load. Write it as a table with ``sources`` to have
it judged.

``--rebuild-all`` (and ``--clean``, which implies it) runs the generator whatever its record says,
as it runs every flow of the launch, and records what that generation leaves: that -- not a
``touch`` -- is what forces a regeneration when something xeda cannot see has changed. An
environment variable the generator reads is such a thing; so is anything its ``sources`` do not
name.

With ``--remote``, the remote flow still runs fresh. ``--rebuild-all`` forces local generator
loading before Xeda ships the generated design; the remote runner's default ``clean`` does not
force local generation on ordinary invocations.

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

.. _targets:

Targets
========

One design file can describe the design for several boards. Each entry of ``targets`` is an
*overlay* on the design: it takes the design file's own keys, merged over them, and
``--target NAME`` selects one.

.. code-block:: yaml

    name: knight
    rtl:
      top: mkKnight
      sources: [Knight.bsv, por_sync.v]
      clock: {port: CLK}
    targets:
      arty:
        sources: [arty.xdc]
        defines: {CLK_HZ: 100000000}
        flows:
          nextpnr: {board: arty_a7_100t}
      ulx3s:
        sources: [ulx3s.lpf]
        defines: {CLK_HZ: 25000000}
        flows:
          nextpnr: {board: ulx3s_85f}

.. code-block:: bash

    xeda run nextpnr knight.yaml --target ulx3s

- **Keys.** A target takes ``rtl``, ``tb``, ``flows`` and every other key of a design file,
  including the flat forms (``sources``, ``defines``, ``top``, ``clock``, ...), which mean in a
  target exactly what they mean at the top of the file. ``name`` and ``targets`` are the
  design's alone. An unknown key is an error, as it is in the design.
- **Merging.** Mappings merge key by key, at every depth (``defines``, ``parameters``, each
  ``flows.<flow>`` section). ``sources`` (``rtl`` and ``tb``) are appended after the design's, in
  order. Any other list replaces the design's. Each ``flows.<flow>`` section is merged by the
  rules of that flow's settings layers (see :doc:`flows`): the aliases of a setting and the
  clock spellings of the flow are read as they are there. A null or empty ``flows`` table, or
  flow section, adds nothing: the design's section stays as it is. Set a key to change it.
- **A short form means its table.** Where the design and the target write one key in different
  forms, each is read as the table it stands for before they merge. ``clock: CLK`` is
  ``clock: {port: CLK}``; ``parameters`` as a list of ``{name, value}`` objects is the mapping
  they give; ``language.vhdl: "08"`` is ``language.vhdl: {version: "08"}``; ``tb.cocotb: true``
  is ``tb.cocotb: {}`` (so it keeps the design's ``module``, and ``false`` means no cocotb, which
  replaces the design's table); a flow's ``fpga: <part>`` is ``fpga: {part: <part>}``. Aliases
  (``generics`` for ``parameters``, ``version`` for ``standard``) are read the same way, at every
  depth.
- **The design's clock.** The design's clock is ``clock``, ``clock_port`` or ``clocks``: three
  spellings of one list of clocks. A target's ``clocks``, in any form, replaces the design's
  list. A target's ``clock`` or ``clock_port`` names the design's first clock and changes it key
  by key (``clock: {name: sys}`` keeps the design's port). ``clock: null``, ``clock_port: ""`` and
  an empty table (``clock: {}``) mean no clock, in the design and in a target. A clock that is
  neither text nor a table (``clock: 0``, ``clock: false``) is an error, reported at the key as
  written, in a target as well (at ``targets.<name>.rtl.clock``). ``clock_port`` takes a port name
  only: a table is an error. Two spellings in the design, or in the selected target, are an error.
  A target that is not selected is checked only for the form of its values.
- **A table where a table is expected.** A value that is no table, and no short form of one,
  where a table is expected (``tb: 3``, or ``keep: 3`` in ``rtl.attributes``, whose entries are
  tables) is an error, whichever target you select: a target's table written over it does not
  hide it. The design's own ``flows`` table and every target's ``flows`` table are checked as
  well, a target's at ``targets.<name>.flows``. So is every target's overlay, selected or not, at
  ``targets.<name>.<key>``.
- **A target overrides the design.** A target is the design author saying "for this board, these
  values", so where the design and the target write the same key, the target's value wins, key by
  key: a target's ``flows.nextpnr.board`` replaces the design's ``flows.nextpnr.board`` without an
  error, and a ``seed`` the target does not write stays the design's. The full order of the
  places a setting can come from, lowest first, is: the flow's defaults, the project file, the
  design file, the target, the command line (``-s``), the API. So ``-s seed=9`` and an API
  override still win over a target for design settings. The selected target name is recorded by
  the loader and cannot be set or changed by a design override. This is a different matter from two *flows* of one run that
  disagree about a setting they share (``yosys_fpga`` and ``nextpnr`` naming different boards),
  which is an error: a target is one author's overlay on one design, not a second opinion.
- **Paths** in a target resolve against the design root, like the design's own.
- **Selection.** A design with one target needs no ``--target``. With several, ``--target`` is
  required, and the error lists them. ``--target`` on a design without ``targets`` is an error.
  From Python: ``Design.from_file(path, target="ulx3s")``. ``--design-overrides`` are an overlay
  on the selected design like a target, and merge by the same rules: a short form means its
  table, and a mistake in the design is not hidden by an override. One rule differs. Only a
  target adds its ``sources`` to the design's, because its job is to add the files of a board. A
  ``sources`` override (``rtl.sources`` or ``tb.sources``) replaces the whole list, the sources
  of the selected target included, as an override of any other list does. A project's designs
  take overrides the same way.
- **Names.** A target name starts with a letter and holds letters, digits, ``_`` and ``-``, and
  is not the name or alias of a flow. There is one spelling, ``targets.<name>``: a design file
  has no ``target`` key; the loader records the selected name, which overrides cannot change.
- **Identity.** Selecting a target yields an ordinary design: the same design, hash and results
  as the file written out flat with the target's keys applied by hand. The target's name is
  reported (``target`` in the ``--json`` documents, ``null`` without one) but is no part of the
  design's identity.

- **Run directories.** A selected target has run directories of its own,
  ``<run root>/<design>/<target>/<flow>`` (``<flow>_<hash>`` with ``--hashed-run-dirs``), also
  when it was selected because it is the design's only one; a design without targets keeps
  ``<design>/<flow>``. Two targets that yield the same design therefore build separately and each
  stays up to date, though they have the same hashes. A run made before targets existed is left
  alone by a target's launch, and ``xeda scrub FLOW DESIGN --target NAME`` removes only that
  target's runs (``xeda scrub FLOW DESIGN`` every target's and the older ones too).

``xeda run`` and ``xeda dse`` take ``--target``; designs in a project file take targets the same
way.

Limits:

- ``board``, ``fpga`` and ``custom_boards_file`` cannot be written once at a target's top level
  (they are refused, naming where to write them): they are settings of each flow, under the
  target's ``flows.<flow>``. A clock constraint goes there too, as ``flows.<flow>.clock``.
- A target cannot inherit from another target.

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
