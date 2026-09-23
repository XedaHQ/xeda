*********************
The design file
*********************

A design description says *what* your design is. It is a single TOML, YAML or JSON file, and it
holds no tool invocations - only, optionally, per-flow settings at the end.

The authoritative, machine-readable definition is::

    xeda design-schema            # JSON Schema, for validation or generation

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
     - Compatibility shorthand for a single-clock design. Prefer ``clock = { port = "..." }``.
   * - ``clock``
     - no
     - A single clock as ``{ port = "...", name = "..." }``.
   * - ``clocks``
     - no
     - A list of clocks, for multi-clock designs.
   * - ``attributes``
     - no
     - HDL attributes to attach to objects, as ``attribute -> (object -> value)``.
   * - ``generator``
     - no
     - Command or generator class that produces the sources (e.g. Chisel elaboration) before the
       flow runs.

Clocks here are *logical*: they name the design's clock ports. The *physical* period or frequency
is a flow setting, because it is a constraint on a particular build rather than a property of the
design. A single-clock design usually needs only::

    [rtl]
    clock = { port = "clk" }

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
       when a ``.py`` source is present, so ``cocotb = true`` is usually redundant.

.. _sources:

Source files
============

The simplest form is a path string; the type is inferred from the extension:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Extension
     - Type
   * - ``.vhd``, ``.vhdl``
     - ``Vhdl``
   * - ``.v``
     - ``Verilog``
   * - ``.sv``
     - ``SystemVerilog``
   * - ``.vh`` / ``.svh``
     - ``VerilogHeader`` / ``SVHeader``
   * - ``.bsv``, ``.bs``, ``.bh``
     - ``Bluespec``
   * - ``.py``
     - ``Cocotb``
   * - ``.cc``, ``.cpp``, ``.cxx``
     - ``Cpp``
   * - ``.sc``
     - ``Chisel``
   * - ``.xdc`` / ``.sdc``
     - ``Xdc`` / ``Sdc``
   * - ``.tcl``
     - ``Tcl``
   * - ``.mem``
     - ``MemoryFile``

When inference is not enough, give a table instead of a string:

.. code-block:: toml

    [rtl]
    sources = [
      "pkg.vhdl",
      { file = "legacy.v", type = "SystemVerilog" },
      { file = "old.vhdl", standard = "93" },
      { path = "generated/top.v" },              # not checked for existence
    ]

``file`` must exist, and be a file rather than a directory, when the design is loaded; ``path``
is not checked, for sources a generator will produce. Every source carries a content hash, which
is what lets Xeda tell runs apart and reuse cached dependency runs. A source that other files find
by its name or place -- Verilog and its headers (an ``include`` searches the including file's
directory first), Bluespec, C++, cocotb modules, memory files -- also counts by its path relative
to the design root, so re-arranging those files is a different design; moving the whole design
never is.

A source may be a glob (``"src/*.vhd"``). Its matches are inserted in sorted order, so the
compilation order -- and the design's hash -- do not depend on the filesystem. Only files match:
a directory whose name fits the pattern is passed over. A variable in a pattern
(``$DESIGN_ROOT``, or any environment variable) stands for the place it names, so a character
such as ``[`` in it is not pattern syntax. A pattern that matches no file is an error, so that a
mistyped one cannot quietly contribute nothing.

.. _language:

``language`` - standards
========================

.. code-block:: toml

    language.vhdl.standard = "2008"      # or "93", "2019", ...
    language.verilog.standard = "2005"

``version`` is accepted as an alias of ``standard``. Two-digit forms (``08``) and four-digit forms
(``2008``) both work.

.. _flows:

``flows`` - per-flow settings
=============================

Settings for a specific flow live under ``[flows.<flow_name>]``. They apply only when that flow
runs, so one design file can carry constraints for several targets:

.. code-block:: toml

    [flows.vivado_synth]
    fpga.part = "xc7a100tftg256-2L"
    clock.period = 5.0

    [flows.openroad]
    platform = "sky130hd"
    clock.period = 10.0

    [flows.ghdl_sim]
    stop_time = "100us"

``xeda list-settings <flow>`` lists what a given flow accepts. Unknown keys are rejected, so a
typo fails loudly rather than being ignored.

Environment variables in paths
==============================

Settings typed as paths expand ``$PWD``, ``$DESIGN_ROOT`` and ``$DESIGN_DIR``, which keeps a
design file portable across machines:

.. code-block:: toml

    [flows.dc]
    target_libraries = ["$DESIGN_ROOT/lib/SAED90/saed90nm_typ_ht.db"]

Design sources expand environment variables too, except ``$PWD``. There ``$DESIGN_ROOT`` and
``$DESIGN_DIR`` name the design root, the directory a relative source is resolved against, so
``"$DESIGN_ROOT/src/*.vhd"`` and ``"src/*.vhd"`` are the same sources.

Multiple designs in one project
===============================

A ``xedaproject.toml`` can hold several designs, plus top-level ``flows`` settings that are merged
into each. Select one with ``--design-name``, or let Xeda prompt you interactively.
