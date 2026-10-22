*****
Flows
*****

A flow is a named sequence of steps that one or more tools perform. ``xeda list-flows`` lists the
flows of your installed version. This page explains how flows are organized and how to find your
way around one.

Discovering flows and their settings
====================================

.. code-block:: bash

    xeda list-flows                     # name, aliases, category, description, dependencies,
                                        # declared inputs/outputs, what can follow it in a chain
    xeda list-settings <flow>           # every setting: name, type, default, meaning
    xeda list-results <flow>            # every key the flow writes to results.json

``list-flows`` reports a ``category`` that comes from the base class of the flow:

``simulation``
    Runs a testbench. Adds the settings ``timeout``, ``fail_severity``, ``stop_time``, the
    waveform settings and ``cocotb``.

``fpga_synthesis``
    Builds a design for an FPGA. Adds the ``fpga`` setting to the clock settings. ``fpga`` is a part
    identifier, or a ``board`` name that fills it in. A ``board`` is looked up in the bundled
    database (``xeda list-boards``) or, with ``custom_boards_file``, in your own database: a TOML or
    YAML file, chosen by its suffix (``.toml``, ``.yaml`` or ``.yml``; any other suffix is an
    error). The database maps each board name to its ``fpga``, its name in openFPGALoader
    (``openfpgaloader_board``, optional) and its local pin file. The pin file has the key of the pin
    format of the family: ``lpf``, ``pcf``, ``pdc`` or ``xdc``. A relative ``custom_boards_file``
    resolves against the design directory, and the local pin file of a board resolves against the
    directory of the database file, in either format. Xeda reads a YAML database as YAML 1.2, like
    every other YAML file it reads. A dotted key in an entry is a path, as in a design file:
    ``fpga.part`` and a table ``fpga`` with the key ``part`` mean the same. Giving ``fpga`` as a
    value and also ``fpga.part`` in one entry is an error. A custom board database uses the same
    key rules as a design file (see :ref:`the rules for dotted keys <dotted-keys>`). An entry has
    only the keys ``fpga``, ``openfpgaloader_board``, ``lpf``, ``pcf``, ``pdc`` and ``xdc``. The
    pin file keys and ``openfpgaloader_board`` hold text that is not empty: a file name or a board
    name. Xeda refuses any other key, and a value of these keys that is empty or not text. The
    error names the board, the key and the database file. When a key looks like a misspelling, the
    error suggests the key that it resembles::

        # boards.yaml
        MY_BOARD:
          openfpgaloader_board: ulx3s
          fpga.part: LFE5U-85F-6BG381C
          lpf: my_board.lpf

    The bundled boards have lower-case names, and Xeda finds them by their name in any letter
    case: ``ARTY_A7_100T``, ``arty_a7_100t`` and ``Arty_A7_100T`` select the same board, which
    Xeda stores as ``arty_a7_100t``. The names in your own database are case-sensitive, like file
    names: ``MY_BOARD`` is not found as ``my_board``. An entry with a ``name`` key is an error that
    says to use ``openfpgaloader_board``.

``asic_synthesis``
    Builds a design for a standard-cell process (``dc``). ``dc`` takes the libraries of the
    process in ``target_libraries``.

``synthesis``
    Synthesis that the base class does not tie to an FPGA or an ASIC (``yosys`` and
    ``ghdl_synth``).

``other``
    A flow that is none of the above (``bsc``).

``list-flows`` folds an alias into its flow. ``ghdl`` appears as an alias of ``ghdl_sim`` and has no
row of its own. Either name works everywhere.

Flow names are forgiving: ``vivado_synth``, ``vivado-synth`` and ``VivadoSynth`` all resolve to
the same flow. An unrecognized name gets a list of close matches.

Settings
========

Every flow declares its own settings. A setting can come from seven places. This is the order from
the lowest to the highest precedence:

1. the defaults of the flow
2. with ``--remote``, the ``flows`` section of the host profile (see :doc:`remote`)
3. the ``flows.<flow_name>`` section of a project file (``xedaproject.yaml``)
4. the ``flows.<flow_name>`` section of the design file
5. the ``flows.<flow_name>`` section of the selected target, if you chose a target with
   ``--target`` (see :ref:`targets`)
6. the command line, with ``-s`` or ``--settings``. The ``flow_settings`` argument of ``run()`` and
   ``plan()`` is the same layer, so messages call it "the command line (``-s``) or
   ``flow_settings``".
7. the API, with ``flow_overrides``

The layers merge key by key. ``-s clock.freq=100MHz`` changes that one setting of a ``clock``
section from the design file and does not replace the whole section. A higher layer replaces only
the keys that it writes. This holds for a target as well: its value wins over the value of the
design for the same key, and only for the keys that the target writes.

A short form means exactly the table that it stands for, in every merge. ``fpga: xc7a35tcpg236-1``
is ``fpga: {part: xc7a35tcpg236-1}``, so ``-s fpga.speed=-2`` refines a part that the design wrote
as text. ``-s fpga=<part>`` over an ``fpga`` table of the design changes the part and keeps the
other keys that the design wrote (a speed grade, say), as ``-s fpga.part=<part>`` does. Xeda reads
the aliases of a setting (``nthreads`` and ``ncpus``) and the clock spellings of a synthesis flow
(``clock`` and ``clocks``) in each layer on its own, so a higher layer changes what a lower layer
wrote, whichever spelling each used. A value that is no table where a table is expected
(``synth: 3``) is an error, whatever a higher layer writes over it.

A flow's settings are written in one place, its own ``flows.<flow_name>`` section, whichever
flow's run needs them. For example, ``-s flows.yosys_fpga.flatten=true`` sets the synthesis that
``nextpnr`` places. ``nextpnr`` has no ``yosys`` section, and using one is an error that names
``flows.yosys_fpga.<key>``. The Vivado simulation and power flows follow the same rule:

* ``vivado_postsynth_sim`` has no ``synth`` section. Using one is an error that names
  ``flows.vivado_synth.<key>``.
* ``vivado_power`` has no ``postsynthsim`` section and none of the simulation settings
  (``elab_debug``, ``saif``, ``stop_time``, ``prerun_time``, ``timeout`` and ``fail_severity``).
  The errors name ``flows.vivado_postsynth_sim.<key>``.
* ``vivado_power`` takes timing activity from ``vivado_postsynth_sim`` by default, which switches
  on the ``timing_sim`` setting of that flow. ``vivado_power`` itself has no ``timing_sim`` setting
  (see :ref:`the functional alternative <power-activity>`).

OpenROAD place and route is a chain of four flows (see `OpenROAD place and route`_). ``openroad``
refuses its removed settings. Each error names the setting to use, or says why there is none.

``-s flows.<flow>.<key>=<value>`` sets a setting of any flow in the run: the requested flow, or one
of its producers. When ``nextpnr`` is the requested flow, ``-s flows.nextpnr.seed=2`` and
``-s seed=2`` are the same setting, and giving both different values is an error. A flow that is
not part of the run is an error too, with a list of close matches.

A ``flows`` table maps flow names to mappings of settings, wherever it is written: in a design
file, a target, a project file, on the command line or through the API. ``flows`` is a mapping,
and so is each ``flows.<flow>``. Text, a number or a list in either place is an error that names
the key and where it was written, even when the value is empty. ``-s flows=3`` and
``-s flows.nextpnr=3`` are errors, and so is ``flows: []`` in a file. Only the API also accepts
the settings of a flow as a list of ``KEY=VALUE`` text, as ``-s`` does.

``-s`` takes space-separated ``KEY=VALUE`` items. It ends at the next option, or at the first token
that is not ``KEY=VALUE``, so it never takes the design file for a setting. ``--`` ends the
options.

The command line takes dotted keys for nested values, and you can give several at once::

    xeda run vivado_synth examples/vhdl/sqrt/sqrt.yaml -s clock.period=4.5ns synth.strategy=Flow_PerfOptimized_high

A key is a value or a table, never both. ``-s timing=true timing.x=1`` is an error that names
``timing`` and ``timing.x``, and so is the same pair in the other order. The same holds in a
design file, a target, ``--design-overrides`` and a ``flows`` table.

.. _opaque-keys:

Some settings hold a dictionary whose keys are names that a tool defines or that a netlist gives.
The run properties of Vivado are an example, and their names contain dots. So are the names of
instances and nets after a flattening, such as ``u_core.reg_1``. A dotted key of such a setting is
one key and not a path into nested tables::

    xeda run vivado_synth examples/vhdl/sqrt/sqrt.yaml -s set_synth_properties.STEPS.SYNTH_DESIGN.ARGS.MAX_BRAM=0

This sets the one property ``STEPS.SYNTH_DESIGN.ARGS.MAX_BRAM`` of the synthesis run. A design
file, a target, a project file and the API give it in the same way. In a design file, the key
``STEPS.SYNTH_DESIGN.ARGS.MAX_BRAM`` under ``set_synth_properties`` is one property, and the same
name written as nested tables (``STEPS``, then ``SYNTH_DESIGN``, and so on) is the same property.
Each origin gives its names, and the origins merge name by name: a higher origin replaces only the
names that it gives. So ``A`` from a design file and ``A.B`` from ``-s`` are two properties, and
both stay. One origin cannot give both ``A`` and ``A.B``, because it writes a key either as a
value or as a table, as for any setting. ``-s set_synth_properties.A=1`` and
``-s flows.vivado_synth.set_synth_properties.A.B=2`` are one origin, so Xeda refuses them too. A
name with a backslash before a dot (``A\.B``) is refused in every origin, because the names of the
tool have no backslash.
A remote run (``--remote``) gets the table as the local origins merged it, ``A`` and ``A.B``
included.

A name with a space is a quoted shell item::

    xeda run vivado_synth examples/vhdl/sqrt/sqrt.yaml -s "set_synth_properties.STEPS.SYNTH_DESIGN.ARGS.MORE OPTIONS=-flatten_hierarchy none"

The same holds for a dictionary inside a settings group. The path goes on to the dictionary, and
the rest of the key is one name::

    xeda run openroad design.yaml -s ir_drop.instance_power.u_core.reg_1=1mW ndr.assignments.u_core.clk_div=wide

``xeda list-settings --json`` marks these settings with ``"opaque_keys": true``. They are
``set_synth_properties`` and ``set_impl_properties`` of ``vivado_synth`` and the flows that share
its settings, the option tables of ``ise_synth``, and these dictionaries of ``openroad``, which
are keyed by net or instance name: ``ir_drop.nets``, ``ir_drop.vsrc_files``,
``ir_drop.instance_power`` and ``ndr.assignments``. In ``synth.steps`` and ``impl.steps``, and in
a dictionary of tables such as ``ndr.rules``, a dotted key is a path into nested tables. A path
that goes on past the value of a key is an error, as for any setting.

A list setting takes comma-separated text: ``-s xdc_files=a.xdc,b.xdc``. An empty value is the
empty list (``-s xdc_files=``). Text written as a list, such as ``[]`` or ``[a,b]``, is an error
that names these two spellings, because it would be one item and not a list. A setting that also
takes plain text keeps the text as it is. The lists of the ``cocotb`` section (``testcase`` and
``gpi_extra``) take text in the same way.

An unknown setting is an error. A mistyped setting that Xeda ignored silently
would give a result that looks fine and is not what you asked for. For the same reason, a flow has
no setting of its own that it does not use. A flow that cannot honor a setting of a related flow
refuses it and names the flow that has it: ``vivado_project`` runs no simulation, so it refuses
the settings of ``vivado_sim``. The error for a removed setting says "``<name>`` was removed: use
..." and names the replacement, or says why nothing replaces it and tells you to delete the
setting.

Settings shared by every flow
-----------------------------

Every flow accepts the settings below, in addition to its own. The tools of some flows have no
matching option. Such a flow accepts ``ncpus``, ``redirect_stdout`` or ``lib_paths`` and does
nothing with it.

``ncpus`` (alias of ``nthreads``)
    The maximum number of threads that the tools may use.

``dockerized`` / ``docker``
    Run the tools of the flow from a container image instead of the local installation.

``quiet`` / ``verbose`` / ``debug``
    How much the flow and its tools print.

``redirect_stdout``
    Send the output of the tools to files instead of the console. Only the Vivado flows do this.

``lib_paths``
    Additional HDL libraries, as ``(name, path)`` pairs. GHDL, ModelSim, NVC and the Vivado
    simulation flows use them. GHDL and NVC take a relative path as relative to the design root.
    GHDL passes ``-P<path>``, where the path is a directory that holds the library. NVC maps a
    named entry to its directory (``--map=<name>:<path>``) and adds an unnamed path to its library
    search path (``-L <path>``), a directory that holds library directories. NVC refuses an entry
    that has a name and no path.

``xeda list-settings <flow>`` lists these settings under *Common settings*. Pass ``--no-common``
to see only the settings of the flow itself.

.. _quantities:

Physical quantities and units
-----------------------------

A setting that holds a physical measurement takes a number and a unit, such as ``5 ns``. A number
without a unit is an error. It means nothing: ``5`` can be 5 ns, 5 ps or 5 MHz. Xeda never guesses
the unit. A few settings do not follow this rule yet. The last part of this section lists them.

Each such setting has a *kind*. The kind is what the setting measures: a time, a frequency, a
capacitance, a length, a voltage, a resistance or a power. The kind decides which units the setting takes.
``clock.period`` is a time, ``clock.freq`` is a frequency, and ``abc.load`` of ``yosys`` is a
capacitance. ``xeda list-settings <flow>`` shows the kind of a setting in its type column: ``time``
and not ``string``. The JSON output shows it too (see :ref:`machine-readable-settings`).

Writing a quantity
~~~~~~~~~~~~~~~~~~

A quantity is one number, optional spaces and one unit. Spaces around the whole value do not
matter. The number has an optional sign, digits, an optional decimal point and an optional
exponent. ``10``, ``-2.5``, ``.5`` and ``1e3`` are numbers. Nothing else is a quantity. An
expression such as ``5 ns * 2`` is an error, and so is a digit separator, as in ``1_000 ns``.

In a design file, a quantity is plain text:

.. code-block:: yaml

    # clock-units.yaml
    name: clock_units
    rtl:
      top: top
      sources:
        - top.v
      clock.port: clk
    flows:
      yosys:
        clock.period: 2.5 ns
        liberty:
          - cells.lib
        abc:
          delay_target: 500 ps
          load: 3.9 fF
      nextpnr:
        clock.freq: 400 MHz

In a TOML or a JSON file, write a quantity as a string: ``period = "2.5 ns"``. On the command line,
write the number and the unit without a space. The shell then passes one word, and the item needs
no quotes: ``-s clock.period=2.5ns``. With a space, quote the whole item:
``-s "clock.period=2.5 ns"``. Without the quotes, the list of ``-s`` items ends at the unit, and
Xeda reports the unit as an unexpected extra argument, with a hint.

Kinds and units
~~~~~~~~~~~~~~~

Each kind takes the units of its row in this table. Xeda writes a quantity into ``settings.json``
in the unit of the last column, whatever unit you wrote: ``10000 ps`` becomes ``10 ns``.

.. list-table::
   :header-rows: 1
   :widths: 18 52 30

   * - Kind
     - Units
     - Unit in ``settings.json``
   * - time
     - ``fs``, ``ps``, ``ns``, ``us``, ``µs``, ``ms``, ``s``, ``min``, ``h``
     - ``ns``
   * - frequency
     - ``Hz``, ``kHz``, ``MHz``, ``GHz``
     - ``MHz``
   * - capacitance
     - ``fF``, ``pF``, ``nF``
     - ``fF``
   * - length
     - ``nm``, ``um``, ``mm``, and ``µm`` with the micro sign (U+00B5)
     - ``um``
   * - voltage
     - ``mV``, ``V``
     - ``V``
   * - resistance
     - ``ohm``, ``kohm``, ``Mohm``
     - ``ohm``
   * - power
     - ``nW``, ``uW``, ``mW``, ``W``, and ``µW`` with the micro sign (U+00B5)
     - ``W``

Units are case-sensitive, as in SI: ``MHz`` is megahertz, and ``mHz`` is millihertz. A unit in the
wrong case is an error that names the right spelling. Xeda takes only the units in the table. A
spelled-out name such as ``nanoseconds`` is an error that lists the units of the kind. A unit
with the prefix ``u`` (micro) can also be written with the micro sign (U+00B5): ``µs`` is ``us``.

Two spellings of one value are the same value. ``10 ns``, ``10000 ps`` and ``0.01 us`` are the
same time. Runs that differ only in such a spelling have the same identity, so Xeda reuses the
results of one for the other. A clock written as a frequency is the same clock as one written as a
period: ``clock.freq: 100 MHz`` is ``clock.period: 10 ns``.

Errors
~~~~~~

Xeda reports a quantity that it cannot read when it plans the run, before any tool starts. The
report names the setting and gives a message. These are the common messages. A number without a
unit, as in ``clock.period: 10``, gives this message:

.. code-block:: text

    `10` needs a unit, such as `10 ns`: a bare number does not mean anything

A unit of another kind, as in ``clock.period: 5 MHz``:

.. code-block:: text

    `5 MHz` is a frequency, not a time

A unit in the wrong case, as in ``abc.load: 3.9 ff``:

.. code-block:: text

    unit `ff` in `3.9 ff`: units are case-sensitive, did you mean `fF`?

Text that is no quantity at all, as in ``clock.period: 5 ns * 2`` or ``clock.period: fast``:

.. code-block:: text

    cannot read `5 ns * 2` as a time: write one number and one unit, such as `10 ns`

A unit that is not in the table, as in ``clock.period: 10 nanoseconds``:

.. code-block:: text

    unknown unit `nanoseconds` in `10 nanoseconds`: a time takes one of fs, ps, ns, us, µs, ms, s, min, h

A value outside the limits of the setting, as in ``clock.period: -1 ns``:

.. code-block:: text

    must be greater than 0 ns, got -1 ns

Settings that are plain numbers
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A setting that holds a plain number takes no unit. These settings hold plain numbers:

* A count: a number of items, bits or steps, or a level, an index or a seed. Examples are an
  iteration limit, a fanout, a verbosity level and a random seed. A count is a whole number.
* A ratio or a fraction: a number with no unit that is not a count. Examples are a utilization of
  ``0.5``, a duty cycle and the weight of a term in a placer.
* A percentage. The name of such a setting ends in ``_percent``, and ``5`` means 5 percent.
* A size of data. The description of the setting names the unit. For example, ``fsdb_size_limit``
  of ``vcs`` is in megabytes, and ``offset`` of ``openfpgaloader`` is in bytes.
* A value in the units of the library. The description of such a setting says "in the units of the
  library". Only the Liberty file of the library knows the unit, for example ``ns`` or ``ps`` for a
  time. Xeda passes the number to the tool as it is.

Settings that do not take units yet
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The settings in this table hold a time, a frequency or another measurement, but they do not take
a quantity. Each keeps its current form. A setting that takes a number refuses text with a unit.

.. list-table::
   :header-rows: 1
   :widths: 40 24 36

   * - Setting
     - Flows
     - Current form
   * - ``stop_time``
     - simulation flows
     - a number of nanoseconds, or text with a unit, such as ``100us``
   * - ``timeout``
     - simulation flows
     - a number of seconds
   * - ``prerun_time``
     - Vivado simulation flows
     - text with a unit, such as ``10ns``
   * - ``time_unit``, ``time_resolution``
     - ``vcs``
     - text such as ``1ns`` and ``1ps``
   * - ``lic_wait``
     - ``vcs``
     - a whole number
   * - ``default_max_input_delay``, ``default_min_input_delay``, ``default_max_output_delay``,
       ``default_min_output_delay``
     - Vivado synthesis and implementation flows
     - a number of nanoseconds
   * - ``hold_detour_max``
     - ``nextpnr``
     - a number of nanoseconds
   * - ``freq``
     - ``openfpgaloader``
     - a whole number of hertz

The ``dc`` flow keeps the form of its own numbers, such as ``default_max_input_delay``. Its clock
settings need a unit, as in every other flow. ``xeda dse`` keeps the form of its options and
optimizer settings: the frequency bounds (``--init-freq-low`` and ``--init-freq-high``),
``resolution``, ``delta``, ``max_runtime_minutes`` and ``timeout`` are plain numbers.

.. _settings-groups:

Settings groups and tiers
-------------------------

A flow with many tool options groups its settings by the command or pass of the tool that they
control. A group is a table of settings, and a group can hold another group. ``xeda list-settings
<flow>`` lists every setting of a group under its dotted name, for example
``<group>.<subgroup>.<setting>``. That name is the key that you give on the command line and in a
file. The ``yosys`` flow groups its standard-cell settings, such as ``abc.strategy``,
``arithmetic.adder_map`` and ``finish.tie_cells`` (see :ref:`yosys-standard-cell-mapping`).

On the command line, give a dotted key to ``-s``. In a design file, a target or a project file, give
the same setting as nested tables or as a dotted key. The two spellings mean the same setting, and
the usual rules for layers and dotted keys hold (see :ref:`the rules for dotted keys
<dotted-keys>`)::

    xeda run yosys design.yaml -s abc.strategy=speed -s sequential.clock_gating.enabled=true

.. code-block:: yaml

    flows:
      yosys:
        sequential:
          clock_gating:
            enabled: true
        # or, with the same meaning:
        sequential.clock_gating.enabled: true

A group takes the same values and gives the same errors as a setting that is not in a group. An
unknown name is an error. When only the case of a name is wrong, the error names the spelling that
exists.

Tiers
~~~~~

Every setting in a group has a tier that says how often a user needs it:

``common``
    The settings that most users set. They come first in a listing.

``advanced``
    Settings for a user who tunes a result.

``expert``
    Settings that follow an option of the tool closely. Most users never set them.

A tier is documentation. It decides where a setting appears in a listing and never what Xeda
accepts: a setting of any tier takes the same values, and a setting of any tier can be set. A
setting outside a group has no tier of its own, and a listing treats it as ``common``.

The tier ``common`` is not the key ``common`` of a listing, which marks the settings that every flow
accepts. The tier says how often a user needs a setting. The key says which flows accept it.

``xeda list-settings <flow> --tier advanced`` shows the settings up to that tier: ``common`` and
``advanced``. ``--tier expert`` shows all of them, which is also the default. The table lists the
``common`` settings first, including the settings outside a group, then a table for each other
tier, and then the settings that every flow accepts. ``--json`` and ``--format`` show the same
settings, and each entry has ``tier`` and ``group`` keys (see :ref:`machine-readable-settings`).

Switchable groups
~~~~~~~~~~~~~~~~~

A group that the tool can skip has the setting ``enabled`` as its first setting. When ``enabled``
is true and you set nothing else in the group, the tool runs the step with its own defaults. When
``enabled`` is false, the tool does not use that option or step. Xeda renders only the flag of
``enabled``, if the group has one, so no other value of the group reaches the tool. Xeda refuses
such a value when it plans the run. The error names each setting that has a value and
``<group>.enabled``. To switch the option or step on again, set ``enabled`` to true or take the
other values out. Xeda compares a setting with the default of the group, so a value that equals the
default is not an error.

Defaults from the platform
~~~~~~~~~~~~~~~~~~~~~~~~~~

A setting whose default depends on the platform has the default ``null``. ``null`` means that the
value of the platform applies. The listing shows ``null``, and the flow takes the value from the
platform when it runs. A value that you set replaces the value of the platform. Some settings name
the platform's value with a word instead of ``null``. In ``yosys``, ``arithmetic.adder_map``,
``sequential.latch_map`` and ``sequential.clockgate_map`` take ``platform``. The
:ref:`yosys-standard-cell-mapping` section lists these settings.

Extra arguments
~~~~~~~~~~~~~~~

A group that wraps a command of a tool can have the setting ``extra_args``. It is a list of
arguments for flags that have no setting of their own. The list is not free text:

* Each flag in the list must be on the list of flags that the group allows. A flag that is not on
  the list is an error, and the error shows the flags that the group allows.
* A flag that a setting of the group controls is an error. The error names that setting, and you
  set it there.
* A value follows its flag in the list. The first item must be a flag.
* An item is a flag when it starts with ``-`` and a letter or an underscore. Any other item is a
  value. ``-0.5`` is a negative number, not a flag.

Xeda checks all of this when it plans the run, before any tool starts. Each item reaches the tool
as one word, with the quoting that the tool needs. ``-s <group>.extra_args=-flag,value`` gives the
list ``-flag``, ``value``, because a list takes comma-separated text.

Producers and consumers
=======================

A flow can declare that it needs the output of another flow. The flow that makes the output is the
producer, and the flow that reads it is the consumer. ``xeda list-flows`` shows the producers of a
flow under *Depends on*, and Xeda runs them first. You ask only for the flow that you want. To
name the stages yourself, see :ref:`flow-chains`.

.. code-block:: text

    yosys_fpga  ->  nextpnr  ->  fpga_pack  ->  openfpgaloader
    yosys_fpga  ->  vivado_impl
    vivado_synth  ->  vivado_postsynth_sim  ->  vivado_power

A producer runs in its own run directory, a sibling of the run directory of its consumer. Its
settings are those of its own section (``flows.yosys_fpga``), and you set them on the command line
with ``-s flows.<flow>.<setting>``. For example, to change how the synthesis stage behaves while
you run ``openfpgaloader``::

    xeda run openfpgaloader blinky.yaml -s flows.yosys_fpga.flatten=true

Some flows declare inputs and outputs (``xeda list-flows --json`` shows them). ``nextpnr`` reads a
``netlist``, by default the one that ``yosys_fpga`` writes. A source of type ``JsonNetlist`` in
``rtl.sources`` supplies that input instead and skips the synthesis:

.. code-block:: yaml

    rtl:
      sources:
        - {file: "top.json", type: JsonNetlist}
      top: top

A listed netlist is your statement of what it is. Xeda does not parse it or compare it with
``platform``, ``liberty`` or ``fpga`` (see :ref:`the rule for a listed netlist <listed-netlist>`).

Xeda resolves the declared producers and their settings before anything runs. A scalar input
needs exactly one matching source. A source or a binding (a chain, or ``flows.<flow>.inputs``)
can displace a default producer. A displaced producer is not part of the run, its settings are
unused, and planning logs each such section. The open FPGA flows declare ``yosys_fpga.netlist``
(enabled by ``netlist_json``) and ``nextpnr.config`` (the selected ECP5 ``textcfg``, iCE40 ``asc``
or Nexus and Xilinx ``fasm``). ``yosys_fpga`` also declares ``netlist_edif``, which ``vivado_impl``
reads (see :ref:`vivado-impl`). ``fpga_pack`` packs that configuration, or a typed ``EcpConfig``,
``IceAsc`` or ``Fasm`` source, into its ``bitstream`` output. ``openfpgaloader`` programs that
bitstream, or a typed ``Bitstream`` source, and declares no output. A missing or stale enabled
configuration fails the run, and disabled outputs are omitted. The flow sets the output paths, and
Xeda verifies them and records them with content digests in ``results.json``. Consumers get only
those checked records, for producers that ran now and producers that Xeda reused.

Along a declared edge, shared settings (``fpga``, ``board``, ``custom_boards_file``, ``clocks``,
``prjxray_db``, ``platform``, ``corner``, ``dont_use_cells``, ``platform_file``, ``liberty``, ``pdk_provider``,
``pdk_version``, ``pdk_root``, ``hierarchical`` and ``wire_rc``) must agree where both endpoints
declare them. A ``platform`` is
the name of a bundled platform. ``platform_file`` and ``pdk_root`` are paths: Xeda compares them by
the resolved path, and a relative path is relative to the design root. Xeda compares a ``corner``
by its name. Xeda compares ``liberty`` as a list of resolved paths in order, so the same files in
another order disagree. When ``pdk_provider`` or ``pdk_version`` is unset, Xeda fills it in from the platform
before it hashes any node, so the version of the PDK is part of the identity of every node in the
group. ``pdk_root`` is not. It tells where the files are, and the same files in another place are
the same run. ``hierarchical`` and ``wire_rc`` belong to the OpenROAD flows only: an OpenROAD
database stores neither the way it was read nor the wire resistance and capacitance, so every flow
of a chain must use the same values. Xeda compares ``wire_rc`` leaf by leaf (``wire_rc.signal_layer``,
``wire_rc.layers.metal2``). A leaf given on one node applies to both, and disjoint leaves
combine. Different
values for the same leaf fail before anything runs, and the error names both files and sections.
An example is ``flows.nextpnr.fpga.part`` in a project and ``flows.yosys_fpga.fpga.part`` in a
design. A command-line leaf (``-s fpga.part=...`` or ``-s flows.yosys_fpga.fpga.part=...``) wins
for the connected group and keeps the unrelated leaves. API overrides keep their own origin with
the highest precedence. Within each node, the normal precedence of the layers still applies.

A flow that reads the outputs of a completed producer holds a verified shared lease on the run
directory of that producer until its own launch ends. Another Xeda process that cleans, rebuilds
or scrubs the producer waits (POSIX only). Missing or changed completion evidence refuses the
hand-over.

Planning without running
------------------------

``xeda run nextpnr blinky.yaml --dry-run`` prints the immutable plan that the launcher would
execute: the producers first, the directories, the hashes, the source or producer of each declared
input, and the optional outputs that are switched on for consumers. Add ``--json`` for a document
(see :doc:`machine-readable`). A dry run starts no tool and changes no run root, marker, lock or
delivery. Planning fails for invalid settings, for a device or family that a flow does not
support, for shared-setting conflicts, and for any configuration that the settings of a flow and
the design rule out.

Xeda first refuses a request that the declarations of the design already rule out, before a
generator runs (see :ref:`generators`). Loading that needs a generator or a Git dependency fetch
is refused before those side effects. Materialize the sources first, or pass an already
materialized ``Design`` to ``DefaultRunner.plan`` of the library (``plan_with_freshness`` adds
what each node would do). Planning does not call ``init()`` of a flow. With ``--remote``, the
host makes the plan (see :doc:`remote`).

The plan also says what a run would do with each flow. The state is ``fresh`` (Xeda reuses the
result), ``stale: <reason>`` (the flow runs, for the reason that the log would give),
``unknown: <reason>``, ``rejected again: <message>`` (the checks rejected the last run, and the
launch reports that rejection and does not run the flow) or ``blocked: <reason>`` (a producer is
``rejected again`` or blocked, so the flow does not run). For example, the state is
``unknown: depends on yosys_fpga`` when a producer would run first, because the answer then depends
on what that run hands over. The dry run reads the run directories and writes nothing, so after an
edit it shows what will run and why. See :doc:`run-directories`.

Runs are make-like by default. Xeda skips a producer whose trace still matches what it would
consume now and reuses its recorded results. ``--rebuild-all`` runs every flow. See
:doc:`run-directories`.

Xeda judges a flow on the files that its producers handed over, and not on their runs. If a
producer ran again and the outputs that a flow takes from it have the same content as the ones the
flow consumed, the flow is up to date, and the log says so. ``nextpnr`` also accepts a netlist
that differs from the one it placed only in the ``src`` attributes of the JSON netlist of Yosys,
which hold the file and line each object came from. When you add a comment above the logic,
``yosys_fpga`` runs again, and ``nextpnr`` and ``fpga_pack`` stay up to date. The reports of such
a ``nextpnr`` run cite the source locations of the netlist that it placed. A change to any other
part of the netlist, the Yosys version included, runs ``nextpnr`` again. So does any change when a
Python hook (``pre_pack``, ``pre_place``, ``pre_route``, ``post_route`` or ``py_script``) is set,
because a hook can read the source locations.

.. _flow-chains:

Flow chains and input bindings
==============================

``xeda run`` also takes a chain of flows joined by ``+``. You ask for the last flow, and each flow
before it supplies the inputs of the next one:

.. code-block:: bash

    xeda run yosys_fpga+nextpnr+fpga_pack design.yaml

This is the build that ``xeda run fpga_pack design.yaml`` already gives by default, so a chain is
for naming the stages yourself: to pick an alternative producer, to override a saved binding, or
to see in one line what a request will run. ``xeda run fpga_pack+openfpgaloader design.yaml``
builds the bitstream and then programs it.

A link ``a+b`` may pass through the flows between them: when the inputs of ``b`` come, through their
default producers, from ``a``, Xeda plans those flows as it would for ``b`` alone.
``yosys_fpga+fpga_pack`` runs ``nextpnr`` in between:

.. code-block:: bash

    xeda run yosys_fpga+fpga_pack design.yaml

The link binds the inputs that ``a`` feeds: here the ``netlist`` of ``nextpnr``. With ``a.output``,
each of those inputs must take that output. A flow that a link passes through cannot appear
elsewhere in the chain, so ``yosys_fpga+fpga_pack+nextpnr`` is refused. A source or a binding that
takes a flow that a link passes through out of the plan is refused too, because the plan would no
longer be that of ``b`` alone.

Rules for chains
----------------

* ``+`` is the only separator, and each flow may appear once. Name a flow as anywhere else
  (canonical name, alias or dashes). ``FLOW.OUTPUT`` names one output of a producer that has
  several: ``nextpnr.config+fpga_pack``.
* A flow supplies every compatible required input of the next one, or the inputs that it feeds in
  the plan of the next one when the link passes through flows. Optional inputs are not bound. An
  output that fits no such input, or two outputs that fit, is an error that names the choices. When
  another output of the producer fixes the pair, the error suggests it:
  ``yosys_fpga.netlist_edif+fpga_pack`` suggests ``yosys_fpga+fpga_pack`` and
  ``yosys_fpga.netlist+fpga_pack``. Nothing searches all flows, so a refusal with no such output has
  no suggestion.
* A flow that programs a device (``openfpgaloader``) can only end a chain.
* Only flows that declare their outputs can start a link: those with a nonempty ``outputs`` in
  ``xeda list-flows --json``. Today these are ``bsc``, ``yosys_fpga``, ``nextpnr``, ``fpga_pack``,
  ``yosys``, the OpenROAD stages, ``vivado_synth``, ``vivado_alt_synth``, ``vivado_impl`` and
  ``vivado_postsynth_sim``. A flow that declares none (``bsc_sim``, ``vivado_project``,
  ``vivado_sim``, ``openfpgaloader``, ``vivado_power`` and others) ends a chain. A chain through
  it is refused: no output fits an input, or the flow after it takes no required input. No stage
  is ever fed by reading the ``artifacts`` of another flow.
* ``xeda list-flows`` shows, for each declared flow, what it takes and makes and which flows can
  come after it (in JSON: ``can_precede`` and ``can_follow``). A flow that follows through other
  flows is shown with them, for example ``fpga_pack (through nextpnr)``. Shell completion offers
  only the flows that can follow the chain typed so far: first the flows that follow directly, then
  the flows that follow through other flows.

Completion returns the whole token, so ``<TAB>`` after a prefix extends the chain:

.. code-block:: text

    xeda run yosys_fpga+ne<TAB>      ->  yosys_fpga+nextpnr
    xeda run fpga_pack+<TAB>         ->  fpga_pack+openfpgaloader
    xeda run openfpgaloader+<TAB>    ->  (nothing: a programmer can only end a chain)

``-s`` sets the last flow, as for any run, and ``-s flows.<flow>.<key>=<value>`` sets any other
flow of the chain. A flow that is not in the run is an error with a list of close matches. A
flow's settings are written in one place, its ``flows.<flow>`` section, whether the flow runs
because you named it or because it is the default producer of another flow:

.. code-block:: bash

    xeda run yosys_fpga+nextpnr+fpga_pack design.yaml -s flows.nextpnr.seed=2 flows.yosys_fpga.flatten=true

Shared settings (``fpga``, ``board``, ``clocks`` and others) that are given at any one node apply
to every node along the edges of the chain. Two different values in files are a conflict that
names both nodes and their files, and a command-line value wins for the whole group. A flow that is
not part of the run gives no shared setting. For example, ``vivado_synth+openfpgaloader`` replaces
the default producers of the programmer, so a device written only in ``flows.yosys_fpga`` reaches
neither node. Write the device for a flow of the chain (``flows.vivado_synth.fpga.part``, or a
``board`` for ``openfpgaloader``), or give ``-s fpga.part=<part>``. The error that a flow needs
``fpga`` names each section that gives it without reaching the flow.

``flow``, ``results``, ``--help-settings`` and the exit status are those of the last flow.
``--json`` adds ``request`` (the chain, element by element, each with the flows its link passes
through in ``through``) and, for every flow of the plan, its ``node`` name and its resolved
``inputs``. A flow that Xeda planned but never started after a failure is listed as ``"state": "not
run"`` (see :doc:`machine-readable`). ``--outputs-to`` delivers the outputs of the last flow, and
only after the whole chain succeeded. A chain that ends at ``openfpgaloader`` has no outputs to
deliver, so ``--outputs-to`` is refused before anything runs. The error names the setting that
delivers the bitstream (``-s flows.vivado_synth.bitstream=$PWD/<file>``). ``--dry-run`` shows the
graph and runs no tool.

Input sources and saved bindings
--------------------------------

Every declared input has one origin, chosen in this order:

1. an explicit binding: a chain link or a saved ``inputs`` entry (below)
2. otherwise a typed source in ``rtl.sources``, in source order. A ``JsonNetlist`` supplies the
   ``netlist`` of ``nextpnr``, and no synthesis runs. A netlist that you list is your statement of
   what it is: Xeda does not parse it or compare it with the platform (see
   :ref:`the rule for a listed netlist <listed-netlist>`).
3. otherwise the default producer of the input (``yosys_fpga`` for the ``netlist`` of
   ``nextpnr``)

A typed source names its kind (``{file: "top.json", type: JsonNetlist}``; ``xeda design-schema``
lists the kinds), and a source never mixes with a producer for one input. A binding is saved in a
design file or a project file under ``flows.<consumer>.inputs``. It names a producer by flow name,
optionally with its output (``yosys_fpga.netlist``). It is a reference to the output of a flow and
never a file path. To give an external file, use a typed source. A list input takes an ordered
list of references.

.. code-block:: yaml

    # routed_demo.yaml
    name: routed_demo
    rtl:
      sources:
        - "top.v"
      top: top
    flows:
      nextpnr:
        fpga.part: LFE5U-85F-6BG381C
        inputs:
          netlist: yosys_fpga.netlist
      fpga_pack:
        inputs:
          config: nextpnr.config

These bindings say what the defaults say, so the plan and every hash are those of the file without
them. They matter when they differ from what the sources or the defaults would choose:

.. code-block:: yaml

    # prebuilt_demo.yaml
    name: prebuilt_demo
    rtl:
      top: top
      sources:
        - top.v
        - file: top.json
          type: JsonNetlist
    flows:
      nextpnr:
        fpga:
          part: LFE5U-85F-6BG381C

With this file, Xeda uses the source unless something binds the input. A saved
``netlist: yosys_fpga`` binds it, and so does a chain:

.. code-block:: bash

    xeda run nextpnr prebuilt_demo.yaml                 # reads top.json, runs no synthesis
    xeda run yosys_fpga+nextpnr prebuilt_demo.yaml      # synthesizes anyway

``--dry-run`` shows which origin each input has and, for a saved binding, the file and section it
came from.

``inputs`` is wiring that the resolver reads. It is not a setting of the flow: it does not appear
in ``xeda list-settings`` and is no part of the ``settings.json`` of a flow or of the design hash.
Xeda splits it out of each origin before it validates the settings, so ``flows.nextpnr.inputs``
never collides with a ``nextpnr`` setting. Where the inputs of a node come from does change its
identity: the same flow with the same settings has another hashed run directory when another
producer feeds it (see :doc:`run-directories`).

Related inputs
--------------

Some inputs of one flow belong together. ``vivado_postsynth_sim`` reads a netlist and the SDF file
that annotates it, so its ``netlist``, ``netlist_timing`` and ``sdf`` have to come from one
producer. ``vivado_power`` reports power on a routed checkpoint with the activity of a simulation,
so its ``checkpoint`` has to come from the synthesis whose netlist that simulation read. A flow
declares such a relation on the input. ``xeda list-flows --json`` shows it as ``same_producer_as``,
with ``via`` when the relation goes through the flow that makes the other input. Xeda refuses a
plan that takes related inputs from different producers, before anything runs. The error names the
inputs, where each comes from and the ``-s flows.<flow>.inputs.<input>=<producer>.<output>``
bindings that make them agree. The rule compares what Xeda generates. A file that you list in
``rtl.sources`` is your own choice and is not compared.

The chain check looks at declarations only. It lists ``vivado_alt_synth`` before ``vivado_power``,
and ``vivado_alt_synth.checkpoint_route+vivado_power`` passes it, but planning then refuses that
chain until the inputs of the simulation are bound too. To report power on the alternative
synthesis, bind the inputs of both flows to it:

.. code-block:: yaml

    # alt_power.yaml
    name: alt_power
    rtl:
      sources:
        - "top.v"
      top: top
      clock.port: clk
    tb:
      sources:
        - "tb.sv"
      top: tb
      uut: dut
    flows:
      vivado_alt_synth:
        fpga.part: xc7a35tcpg236-1
        clock.period: 5 ns
      vivado_postsynth_sim:
        inputs:
          netlist: vivado_alt_synth.netlist
          netlist_timing: vivado_alt_synth.netlist_timing
          sdf: vivado_alt_synth.sdf
      vivado_power:
        inputs:
          checkpoint: vivado_alt_synth.checkpoint_route

.. _power-activity:

Power from a functional simulation
----------------------------------

``vivado_power`` takes the activity of a timing simulation by default
(``vivado_postsynth_sim.timing_saif``). Vivado can also estimate power from the activity of a
functional simulation of the netlist. That simulation is faster and has none of the problems of a
timing simulation, but the estimate is less accurate. Bind the ``activity`` input to the ``saif``
output of the simulation to ask for it. ``vivado_power`` has no ``timing_sim`` setting, so the
binding is the only switch:

.. code-block:: yaml

    # power_functional.yaml
    name: power_functional
    rtl:
      sources:
        - "top.v"
      top: top
      clock.port: clk
    tb:
      sources:
        - "tb.sv"
      top: tb
      uut: dut
    flows:
      vivado_synth:
        fpga.part: xc7a35tcpg236-1
        clock.period: 5 ns
      vivado_power:
        inputs:
          activity: vivado_postsynth_sim.saif

The same binding on the command line is
``-s flows.vivado_power.inputs.activity=vivado_postsynth_sim.saif``.

Where a binding can be given
----------------------------

From the lowest rank to the highest, you can give a binding in a project file, in the design file,
on the command line and through the API. On the command line, use a chain,
``-s flows.<consumer>.inputs.<input>=<reference>``, or ``-s inputs.<input>=<reference>`` for the
requested flow. A higher rank replaces a lower one input by input. A chain is command-line input,
so it replaces a binding saved in a design or project for the same input, and the plan says so
(``overridden``). A chain and an explicit command-line or API binding of the same input are an
error, even when they are equal. Xeda reports the error before it checks either binding:

.. code-block:: text

    The chain position 1 (yosys_fpga) -> 2 (nextpnr) binds input 'netlist'; the command line (`-s`)
    or `flow_settings`: flows.nextpnr.inputs.netlist also binds that input. Give the binding in one
    place, even when equal.

A project file given with ``--xedaproject project.yaml`` holds the same ``flows.<flow>.inputs``
sections, below those of the design:

.. code-block:: yaml

    # project.yaml
    flows:
      nextpnr:
        inputs:
          netlist: yosys_fpga

``xeda dse`` refuses a chain and any binding that the request reaches, before it starts a worker.
It ignores a binding saved for a flow that the request does not reach. A remote run takes chains
and bindings (see :doc:`remote`).

What is supported today
-----------------------

Chains cover the open FPGA flows (``yosys_fpga``, ``nextpnr``, ``fpga_pack`` and
``openfpgaloader``). ``yosys_fpga`` and ``nextpnr`` work for ECP5, iCE40, Nexus and Xilinx 7-series.
``fpga_pack`` packs ECP5, iCE40 and Xilinx 7-series, and it has no packer for Nexus. The build-only
chain ``yosys_fpga+nextpnr+fpga_pack`` runs no programmer and touches no hardware. Only a chain that
ends at ``openfpgaloader`` programs a device. The test suite runs these chains against fake tools
and never starts a real programmer.

Chains that go through ``vivado_project`` or ``vivado_sim`` are not available yet. Xeda refuses the
command below today. The error starts with ``Flow `yosys_fpga` has no compatible output for a
required input of `vivado_project```. These chains need the flows to declare their outputs, and
example designs and targets that use them:

.. code-block:: bash

    # not available yet
    xeda run yosys_fpga+vivado_project knight.yaml

``vivado_synth``, ``vivado_alt_synth`` and ``vivado_impl`` declare their outputs, each switched on
by its own setting (``write_netlist``, ``write_timing_netlist``, ``write_checkpoint`` and
``bitstream``). So ``vivado_synth+openfpgaloader`` is a chain: the loader switches ``bitstream``
on. It needs the ``fpga`` of the design and a clock, and it programs a device, so plan it with
``--dry-run``.

Every flow that reads design sources has the input ``design``. It takes the design that ``bsc``
derives from a Bluespec design (see `Converted designs`_), and no other flow supplies it. A flow
that reads no design sources (``nextpnr``, for example) has no such input, and a binding for it
is refused.

Open-source FPGA flows: devices and tuning
==========================================

``yosys_fpga`` runs the Yosys synthesis pass of the selected family (``synth_<family>``). It
supports ECP5, iCE40, Nexus, Xilinx and Gowin. ``nextpnr`` adds verified device selection,
constraints and output formats for ECP5 (Trellis ``textcfg``), iCE40 (``asc``), Nexus (``fasm``)
and Xilinx 7-series (``fasm``, with the ``nextpnr-himbaechel`` of openXC7). ``fpga_pack`` packs
ECP5 with ``ecppack``, iCE40 with ``icepack`` and 7-series with ``fpga-as``, and ``openfpgaloader``
programs the bitstream. Each flow rejects a family for which it has no tested mapping or packer,
before any synthesis runs. ``open_xc7`` is a removed flow name: build with ``fpga_pack`` and
program with ``openfpgaloader``.

The options of the Yosys synthesis passes changed between releases, so ``yosys_fpga`` chooses them
by the installed Yosys version. Yosys 0.63 is the minimum, and Xeda checked the options through
0.69. ABC9 is the default mapper everywhere. The pass for Xilinx uses it only with ``-abc9``
before Yosys 0.69, and Xeda passes that option. The other passes use it unless given ``-noabc9``.
Since Yosys 0.69, every pass uses ABC9 and cannot turn it off. ``abc9: false`` maps with classic
ABC, and ``retime`` enables ``-retime``. Yosys 0.69 removed both choices, so with that release
they are errors. On iCE40, ``noabc`` maps LUTs without ABC at all.

``flatten`` left unset on a Xilinx device is ``true``. ``synth_xilinx`` alone keeps the hierarchy.
Flattening measured better in the designs that were tried. As with ``flatten: true``, Xeda flattens before it writes ``rtl_verilog`` and ``rtl_json``,
so those files are flat too. ``flatten: false`` keeps the hierarchy. The Lattice, iCE40 and Gowin
passes flatten on their own, so for them an unset ``flatten`` adds nothing, and the RTL outputs
keep the hierarchy. Under ``synth_pass_only``, an unset ``flatten`` is the choice of the pass for
every family. A setting for which the selected pass has no option is an error and is not ignored.
``nowidelut`` is off. Turning it on disables the wide LUT implementations that use hard MUX
resources, and its effect on area and timing differs from design to design. iCE40 UltraPlus DSP
and SPRAM inference are available as the ``ice40_dsp`` and ``ice40_spram`` settings of
``yosys_fpga``.

``stop_after: rtl`` (``yosys`` has it too) elaborates the design, writes the RTL outputs
(``rtl_json``, ``rtl_verilog`` and ``rtl_graph``) and stops before synthesis. The run succeeds,
with no netlist and no report. Xeda refuses, before anything runs, a setting that asks for what
synthesis writes: ``netlist_json`` and ``netlist_verilog`` (both on by default), ``netlist_graph``,
``write_blif``, ``sta`` and ``ltp``. So write ``-s stop_after=rtl netlist_json= netlist_verilog=``.
A flow that takes the netlist, such as ``nextpnr``, cannot follow a stopped flow. Xeda refuses the
plan, and the message names that flow.

A Lattice ordering code as ``fpga.part`` selects the Yosys timing model and the nextpnr device and
package. Examples are ``iCE40UP5K-SG48I`` and ``iCE40HX8K-CT256`` (temperature grades and
tape-and-reel suffixes are accepted), and ``LFE5U-85F-8BG381C``. For Nexus, give the full device
name that nextpnr-nexus uses, such as ``LIFCL-40-9BG400C`` (``nextpnr-nexus --list-devices``).

``nextpnr`` uses the timing-driven placer and router that each backend has as its default. Specify
the actual part, package, speed grade and timing constraints before you compare results.
``clock.period`` sets the frequency constraint.

Put pin files in ``rtl.sources`` with the types ``Lpf`` (ECP5), ``Pcf`` (iCE40), ``Pdc`` (Nexus) or
``Xdc`` (Xilinx). Xeda concatenates the files of the selected family in source order. When there
are none, it uses the pin file of the selected board. Xeda concatenates typed ``Sdc`` sources
separately, appends the file of the optional ``sdc`` setting, and passes the result once through
``--sdc``. A clock may have only one timing constraint across pin files, SDC files and flow
settings. An error for a duplicate names both original paths and lines, or the clock setting and
its period. A clock that only a file constrains gets its timing from the file, with no generated
frequency. Xeda refuses the removed ``lpf_cfg``, ``pcf_cfg`` and ``pdc_cfg`` settings. The error
names the typed pin file to list in ``rtl.sources`` instead
(``{file: "pins.lpf", type: Lpf}``).

The ``sdc`` setting, the hooks and ``py_script`` name files relative to the design root (or with
``$DESIGN_ROOT``). A setting of another architecture is an error. A fixed ``seed`` makes
comparisons reproducible. Try several fixed seeds when you optimize a particular design. The common
settings ``placer``, ``router``, the HeAP weights and the hooks allow measured experiments. The
experimental settings ``tmg_ripup``, ``parallel_refine`` and (for iCE40) ``opt_timing`` stay
opt-in. A faster or smaller Yosys netlist alone does not show an improvement in routed Fmax.

For example, list ``{file: "pins.pcf", type: Pcf}`` in ``rtl.sources``, then run::

    xeda run nextpnr blinky.yaml -s fpga.part=iCE40HX1K-TQ144 \
      -s clock.period=20ns -s seed=2
    xeda run openfpgaloader blinky.yaml -s write_flash=true -s verify=true

Use ``xeda list-settings <flow> --json`` with ``yosys_fpga``, ``nextpnr``, ``fpga_pack`` or
``openfpgaloader`` to inspect all named settings. ``synth_flags``, ``extra_args`` and the
``packer_args`` of ``fpga_pack`` expose switches that depend on the device family or the tool
version and have no setting of their own. The installed tool must support those switches.
Placement and programming are different operations: ``openfpgaloader`` is the only flow here that
writes hardware.

Running only the Yosys synthesis pass
-------------------------------------

By default, ``yosys_fpga`` adds the preparation and cleanup stages of Xeda around
``synth_<family>``. ``synth_pass_only: true`` omits those stages and reads the sources of the
design as a bare ``yosys <files>`` does: a Verilog source with plain ``read_verilog`` and a
SystemVerilog source with ``read_verilog -sv``. The reader follows the ``type`` of each source,
which its suffix gives unless the design states another type. So a ``.v`` file typed
``SystemVerilog`` (or a ``.sv`` file typed ``Verilog``) is read as that type, and not by its suffix
as Yosys would read it. The parameters of the design, ``synth_flags`` and an explicitly selected
ABC9 script still apply in either mode. In pass-only mode, Xeda refuses the settings that request
extra Xeda stages, or that make the mode read differently from ``yosys <files>``. It does not
ignore them silently:

``prep``, ``pre_synth_opt``, ``post_synth_opt``, ``splitnets``, ``post_synth_rename``,
``black_box``, ``keep_hierarchy``, ``set_attribute``, ``set_mod_attribute``, ``clockgate_map``,
``rmports``, ``stop_after``, ``rtl_json``, ``rtl_verilog``, ``rtl_graph``, ``sta``, ``ltp``, a
nonempty ``read_verilog_flags``, a ``systemverilog`` front end other than ``default`` and a
nonempty ``read_systemverilog_flags``.

Three of these are reader choices of Xeda, which the full recipe keeps. The mode needs their native
values written out: ``read_verilog_flags: []``, ``systemverilog: default`` and no
``read_systemverilog_flags``. ``read_verilog_flags`` defaults to ``-sv``, which Yosys does not pass
for a ``.v`` file. The option changes what such a file accepts: ``logic`` is a keyword with
``-sv`` and an identifier without it. ``systemverilog`` defaults to ``slang``, a plugin whose
``read_slang`` is not what ``yosys file.sv`` runs. ``default`` is the built-in ``read_verilog -sv``
of Yosys, which reads a ``.sv`` source. Xeda refuses these three settings by value, so it refuses
their defaults too. On the command line, the empty list is ``-s read_verilog_flags=`` (an empty
value). The text ``[]`` is refused, as it is for every list setting.

The default ABC9 behavior depends on the mode. In the full recipe of Xeda, an unset ABC9 script
selects ``flow3``, and a constrained clock supplies a clock-derived ABC9 delay (described below).
In pass-only mode, an unset script leaves the choice to the Yosys synthesis pass, and Xeda adds no
clock-derived delay. ``abc9_script`` can choose one of the scripts that Yosys includes, in either
mode: ``default``, ``default.area``, ``default.fast``, ``flow``, ``flow2``, ``flow3`` or
``flow3mfs``. The script names come from the installed Yosys build. The ``flow3`` setting is
also accepted: ``true`` selects ``flow3``, and ``false`` leaves the script to Yosys. Do not set
both ``abc9_script`` and ``flow3``. The script selection matters only when ABC9 mapping is enabled.

.. code-block:: bash

    xeda run yosys_fpga blinky.yaml -s synth_pass_only=true read_verilog_flags= \
      systemverilog=default
    xeda run yosys_fpga blinky.yaml -s synth_pass_only=true read_verilog_flags= \
      systemverilog=default abc9_script=flow2
    xeda run fpga_pack blinky.yaml -s flows.yosys_fpga.synth_pass_only=true \
      flows.yosys_fpga.read_verilog_flags= flows.yosys_fpga.systemverilog=default

The clock-derived delay is two thirds of ``clock.period``, in picoseconds. Yosys gives it to ABC9
as the required time of the LUT mapping (the ``-D`` option of the ``&if`` command of ABC). The Yosys
log shows it, for example ``ABC: + &if -W 300 -D 6666.666666666668``. ABC9 can use a required time
only when the mapped logic can meet it. If the required time is lower than the least delay that
ABC9 can reach, ABC9 maps for that least delay, as it does with no required time, and the netlist
does not change. ABC then prints ``ABC: Warning: Cannot meet the target required times (4000.00).
Mapping continues anyway.`` in the Yosys log. If the required time is higher, ABC9 spends the slack
on area. The mapping can change, and it can get smaller. It can also keep its LUT count and change
only the mix of LUT sizes. So the netlist changes with ``clock.period`` only when the clock is slow
for the design. A design with little logic to trade can map to the same netlist at every period. A
netlist that does not change with the clock period is therefore expected.

Pass-only mode does not by itself guarantee the same result as a native Yosys command. To compare
them, match the Yosys version and the FPGA family, the source paths and their order, the
parameters, the flags of the synthesis pass and the ABC9 script. The mode itself matches the
reader. For plain Verilog and for SystemVerilog that the built-in front end of Yosys reads, the
mode reproduces the native netlist cell for cell and name for name (checked against the installed
Yosys). Yosys also embeds source paths in generated names, so use the same path spellings on both
sides.

The synthesis pass of the family reads its own primitive libraries. If ``verilog_lib`` also names
one of those files, Xeda skips that duplicate read. It compares the file itself (including paths
through links) with the data directory of the installed Yosys. Every ``read_verilog`` advances the
shared counter of generated names in Yosys, and ABC9 maps by generated names, so an extra library
read can change the netlist. Xeda gets the data directory from ``yosys-config`` beside the selected
Yosys executable (or in the selected container image). A difference in cell counts alone does not
show a difference in quality.

Xilinx 7-series with openXC7
----------------------------

Artix-7, Kintex-7, Spartan-7, Virtex-7 and Zynq-7000 parts build with the openXC7 1.0 toolchain:
its ``yosys``, its ``nextpnr-himbaechel`` (the Xilinx backend) and ``fpga-as``. Put its ``bin``
directory on ``PATH``. Xeda finds the tool data from the resolved ``nextpnr-himbaechel``
executable, in ``<prefix>/share/nextpnr/himbaechel`` and ``<prefix>/share/nextpnr/prjxray-db``, and
reads no environment variable of the ``CHIPDB_DIR`` kind. This is a design for the Arty A7-100T:

.. code-block:: yaml

    name: blinky
    rtl:
      sources:
        - "blinky.v"
        - "blinky.xdc"   # pins: an Xdc source, typed by its suffix
      top: blinky
      clock.port: clk
    flows:
      yosys_fpga:
        fpga.part: xc7a100tcsg324-1
      nextpnr:
        clock.freq: 100 MHz

``xeda run fpga_pack blinky.yaml`` synthesizes, places and routes the design, and packs
``outputs/blinky.bit`` in the run directory of ``fpga_pack``. Nothing is programmed. ``xeda run
openfpgaloader blinky.yaml`` builds the same bitstream if it is not up to date, and loads it. You
give the part once, because ``fpga`` is shared along the declared edges.

.. warning::

   Some builds of the openXC7 ``nextpnr-himbaechel`` write wrong bits and print no error. The build
   succeeds, the timing is met, and the bitstream can behave differently from the design. The
   openXC7 1.0 release and ``1.0.0-41-g3e5c2cdd`` (2026-10-05), the commit that the installer of
   the openXC7 toolchain pinned, have none of the fixes below. At least these defects are known.
   Each item says what the design contains and what goes wrong:

   * A multiplication that Yosys maps to a ``DSP48E1``: the DSP ignores its A operand (issue 39,
     pull request 66).
   * A memory with initial contents that Yosys maps to ``RAM32M`` or ``RAM64M``: every LUT of the
     memory is 0 (pull request 70).
   * A shift register on the falling clock edge: it shifts on the rising edge (pull request 70).
   * A block RAM with an initial value or a reset value on its output register: the register
     starts at 0, and a reset loads 0. A simple dual-port memory that is 64 bits wide, in one
     ``RAMB36E1``, gets the wrong write width (pull request 69).
   * An ``ODDR`` that drives the T input of an ``OBUFT`` or ``IOBUF``: the tri-state is not
     registered. An ``ODDR`` with no ``INIT`` starts high instead of low (pull request 72).
   * An ``MMCME2`` or ``PLLE2``: the loop filter, power and fractional-divide registers, the duty
     cycle and a negative phase differ from Vivado's (pull request 68).
   * The differential and drive settings of a pad: a ``TMDS_33`` input is programmed as
     ``LVDS_25``, an ``LVDS_25`` or ``TMDS_33`` output on a high-range bank gets a bit that
     Vivado does not set, ``LVCMOS33`` with ``DRIVE 16`` gets 12 mA, and the internal stages of an
     ``IDDR`` start with the wrong values (pull request 71).
   * A memory of 64K words by 1 bit or deeper, which Yosys maps to cascaded ``RAMB36E1`` pairs:
     the router usually fails, and a pair that does route has the wrong address bit and is not
     marked as the lower block (pull request 67).
   * An output of a high-performance bank that an ``ODDR`` or an ``OSERDESE2`` drives: the pad is
     driven around the register (pull request 78).

   The chain ``yosys_fpga+nextpnr+fpga_pack`` is affected. A timing report or a utilization report
   cannot show these defects. Only a test of the bitstream itself can. Use a build of the ``main``
   branch of openXC7/nextpnr at commit ``26f5e17a5`` (``1.0.0-75-g26f5e17a``) or later. That
   commit has every fix in this list. ``nextpnr-himbaechel --version`` names the build. Xeda does
   not check the build. The authors of the fixes compared the bitstreams with Vivado's for the same
   netlist. None of the pull requests reports a test on a board.

``fpga.part`` must be the full ordering part (device, package, pin count and speed grade, for
example ``xc7a100tcsg324-1``) as the Project X-Ray database lists it. Xeda refuses a bare device
name before synthesis. ``board: arty_a7_100t`` or ``arty_a7_35t`` fills it in (``xeda
list-boards``). So do ``basys_3`` (``xc7a35tcpg236-1``) and ``stlv7325_v2`` (``xc7k325tffg676-2``).

Constraints
~~~~~~~~~~~

``Xdc`` sources carry the pins (``set_property`` with ``PACKAGE_PIN`` or ``LOC``, and
``IOSTANDARD``) and may carry ``create_clock``. With no ``Xdc`` source and a ``board``, Xeda uses
the bundled pin file of the board. The bundled Arty files are active, pin-only derivatives of the
master XDC of Digilent, and they keep its port names (``CLK100MHZ``, ``led[0]``, ``sw[0]``,
``btn[0]`` and so on). A design that relies on the fallback names the ports of its top module that
way. The ULX3S file likewise uses the names of the board (``clk_25mhz``, ``led[0]`` and so on), and
it sets a 25 MHz constraint on ``clk_25mhz``. The bundled Xilinx files contain no clock constraint.
Timing comes from the ``clock`` or ``clocks`` setting of the flow, or from the files of the design,
and each clock has one authority. With no clock constraint at all, nextpnr analyzes at its default
of 12 MHz, and Xeda says so. nextpnr reads ``set_property PULLTYPE PULLUP [get_ports rst]``. It
ignores ``set_property PULLUP true [get_ports rst]`` and prints no warning, so a port that uses the
second form gets no pull-up. The bundled pin files use neither form, and a pin file of your own may.

The bundled ``basys_3`` and ``stlv7325_v2`` files constrain the clock, every user LED, the user
buttons and the USB-UART, and nothing else. A design that relies on them names its ports as
follows. Each file gives the source of every group of pins. The ``stlv7325_v2`` pins are checked on
hardware, as described below. The ``basys_3`` pins are not.

* ``basys_3`` uses the names of Digilent, all LVCMOS33: ``clk``, ``led[15:0]``, the buttons
  ``btnC``, ``btnU``, ``btnL``, ``btnR`` and ``btnD``, and the UART's ``RsRx`` (an input of the
  FPGA) and ``RsTx`` (an output). The LEDs and buttons are active high, and ``btnC`` is the reset
  button.
* ``stlv7325_v2`` has no vendor names. Its ports are ``clk_p`` and ``clk_n`` (the differential
  clock, DIFF_SSTL15: the design needs an ``IBUFDS`` and a ``BUFG``), ``led[7:0]`` (LED0 to LED7),
  ``btn[1:0]`` (``btn[0]`` is K2, ``btn[1]`` is K3), and the UART's ``uart_rx`` (an input) and
  ``uart_tx`` (an output). The LEDs and buttons are active low. The jumper J4 sets the voltage of
  the bank of ``btn[1]`` and the UART. Their standard, LVCMOS33, is right with J4 at 3.3 V. The file
  is for the second (2023) revision of the board. The Vivado board files on GitHub (rriggs) describe
  the first revision, which has other UART pins and a 100 MHz clock on F17 (the second has a 50 MHz
  clock on F17, as listed in the `STLV7325 v2 platform source
  <https://github.com/litex-hub/litex-boards/blob/10debf146d433ce8ac8aedcac84e97d252ff4d5b/litex_boards/platforms/sitlinv_stlv7325_v2.py#L19-L25>`_).
  This target uses the separate 200 MHz differential input described below.

  The clock, LED, button and UART pins are checked on one board. A counter behind an ``IBUFDS`` and
  a ``BUFG`` on the clock pins ran at 200 MHz, read over JTAG. A static pattern and a walk lit LED0
  to LED7 in order, and holding K2 or K3 lit ``led[0]`` or ``led[1]``. Text sent on ``uart_tx``
  arrived through the board's USB-UART bridge (a WCH CH9102), and an echo design on ``uart_rx`` and
  ``uart_tx`` answered at 115200 and 3 Mbaud. The FT4232H that programs the board is not that UART.
  A plain echo test can pass through a wire loopback without testing the FPGA: a loopback plug, a
  wrong port, or a programmer that ties TXD to RXD on a spare channel. Build with
  ``ECHO_TRANSFORM=1`` and run with ``--transform`` to check that the FPGA is in the data path.
  The four LEDs beside the SFP cages (LED8 to LED11) are not ports: they follow
  ``led[2]``, ``led[3]``, ``led[1]`` and ``led[0]`` through a level shifter.

The chip database
~~~~~~~~~~~~~~~~~

nextpnr needs a database for the die, which openXC7 generates from the Project X-Ray data. Xeda
generates it on first use and keeps it under the run root, in
``.cache/xilinx-chipdb/<identity>/<die>.bin``. Every design that uses that run root shares it. The
first build of a die runs a generator that takes noticeable time and memory, and larger dies need
more. Later launches start no generator. Xeda identifies an entry by content: the nextpnr and
``bbasm`` executables, the generator and the files of the database family. Another installation, or
an updated one, therefore gets its own entry. Xeda never modifies an entry, and ``xeda scrub`` and
``--clean`` leave the entries alone. Delete the ``.cache`` directory to reclaim the space.
``chipdb`` names an existing database file to use instead, and then Xeda generates nothing.
``prjxray_db`` names another Project X-Ray database root, and ``nextpnr`` and ``fpga_pack`` must
agree on it. Generation and the Python hooks of nextpnr run with bytecode writing off, so Xeda
never writes to the installation.

Tuning
~~~~~~

``delay_matrix`` (``build``, the default of the backend, or ``off``), ``hold_fix`` (``true``, or a
pass limit) and ``hold_detour_max`` are options of the Xilinx backend itself. ``seed``, ``placer``,
``router`` and the hooks are common to every nextpnr backend. ``placement`` names a location that
Xeda delivers the placement dump to.

Result keys
~~~~~~~~~~~

``ff`` counts ``SLICE_FFX`` cells, ``bram`` counts ``RAMB18E1`` plus ``RAMB36E1``, ``dsp`` counts
``DSP48E1`` and ``io`` counts pads. Xeda reports ``CARRY4`` and the other cell types under their
own names. ``device`` is the part, and ``fabric`` is the die that was routed. Every ``available``
total in the utilization describes the fabric, so an ``xc7a35t`` (routed as an ``xc7a50t``) shows
the totals of the larger die. ``clock_port`` is given only when the one reported clock domain is
itself a port of the top module. A domain that is named for the output net of a buffer or a clock
generator keeps its raw name alone. A run that misses its timing constraint fails with the
"Max frequency ... (FAIL at ...)" line that nextpnr prints, unless ``timing_allow_fail`` is set.

``lut`` is reported per toolchain and per stage, and ``LUT:STAGE`` and ``LUT:METHOD`` say which.
After ``nextpnr`` it is the number of distinct LUT locations (tile, site and one of A-D) that the
placement occupies. A location whose two outputs are both used counts once, and LUTs used as
distributed RAM or shift registers are included. ``yosys_fpga`` estimates it from the mapped
primitives (``LUT:LOGIC``, ``LUT:RAM``, ``LUT:SRL``) before packing. The two numbers differ for one
design, and neither is certified comparable with the utilization report of Vivado. Compare a
design against itself across settings or seeds with one toolchain, and not across toolchains. The
same holds for ``Fmax``: the timing model of nextpnr is not the model of Vivado (see
:ref:`flow-results`).

Packing and programming
~~~~~~~~~~~~~~~~~~~~~~~

``fpga_pack`` runs ``fpga-as`` with the Project X-Ray family database and the part. It writes into
a scratch file in its run directory, and it publishes the bitstream only when the packer succeeded
with a nonempty file, so a failed run leaves no partial bitstream. A Project X-Ray database often
has the part data of one speed grade only. A device and package are one die with one pinout, so the
speed-grade directories of one device and package are expected to agree. ``fpga_pack`` uses the
directory of the exact part when the database has one. If not, it uses the directory of the same
device and package at the lowest other speed grade (the smallest grade number; a plain grade comes
before its ``L`` variant), and it logs which part it asked for and which directory it used. It
warns if the ``part.json`` or ``package_pins.csv`` of the other grades differ from the one it uses.
``nextpnr`` keeps the exact grade for timing. The header of the bitstream, which ``fpga-as``
writes, names the part whose data was used. A build for ``xc7k325tffg676-2`` that is packed with
the ``-1`` data says ``xc7k325tffg676-1``. ``fpga_pack`` never uses another device or package. If
the database has no directory for the device and package at any speed grade, the flow fails before
any tool runs. The message names the part, the directory it searched, and the packages of the same
device that the database has, with their speed grades.

``openfpgaloader`` loads into SRAM by default. ``write_flash`` programs the flash, and ``verify`` is
accepted only with it. A ``cable`` takes precedence over the board. With a ``cable``, the flow
passes ``--cable``, and ``--fpga-part`` when the part is known. It passes no board name, even when
``board`` is set. Without a ``cable``, when ``board`` is set, the flow passes the
``openfpgaloader_board`` of the board to openFPGALoader as ``--board``, and no ``--fpga-part``.
openFPGALoader knows the part of the board, and the option would replace it. A board that
openFPGALoader does not know has no ``openfpgaloader_board``. Then the flow gives the part of the
board as ``--fpga-part``. openFPGALoader uses ``--fpga-part`` as written to name the bridge
bitstream that it needs to write a flash (``spiOverJtag_<device><package>.bit.gz``), which has no
speed grade. So the flow gives a Xilinx part without its speed grade (``xc7a35tcpg236``), and any
other part as it is. Every bundled board has an ``openfpgaloader_board``, taken from the board list
of openFPGALoader itself (``basys_3`` is ``basys3``, ``ulx3s_85f`` is ``ulx3s``). When no part is
known, from ``fpga`` or from the board, the flow gives no ``--fpga-part``, and openFPGALoader
detects the device. Programming the flash needs the device, so Xeda refuses ``write_flash`` before
anything runs when neither ``fpga`` nor a board gives it. What the loader printed is in
``openfpgaloader.log`` in the run directory of the flow. In a terminal, Xeda runs the loader with a
terminal of its own, as you would run it by hand: the colors work, and the progress bar is redrawn
in place. The log has the same text without the escape codes, with one line for each redraw. Without
a terminal (a pipe, a file, a CI log), or with ``console_colors`` switched off, the loader writes a
line for each update of the bar.

openFPGALoader opens the first probe whose USB id fits the cable (``digilent`` is
``0x0403:0x6010``). It does this before it reads the ID code of any FPGA, and ``--fpga-part`` does
not change it. With two boards of one kind connected, for example an Arty A7-100T and a Basys 3, it
can program the wrong one. So the flow chooses the probe when the run names none. It does this when
all of these hold:

* The run names no probe: no ``usb_serial_num``, no ``cable_index``, and none of ``--ftdi-serial``,
  ``--usb-serial-num``, ``--cable-index``, ``--busdev-num``, ``--vid``, ``--pid``, ``--device`` or
  ``-d`` in ``extra_args``.
* ``extra_args`` gives no other cable or board (``-c``, ``--cable``, ``-b``, ``--board``). When it
  does, probe selection is left to openFPGALoader.
* The cable is known: the ``cable`` setting, else the cable that ``openFPGALoader --list-boards``
  gives the board. A board whose cable is ``Undefined``, or that the loader does not list, has none.
* The part is known: ``fpga.part``, which ``board`` fills in.

The flow asks the loader for the cables, the boards (when the cable comes from the board) and the
probes with the USB id of the cable (``--list-cables``, ``--scan-usb``). With none or one probe,
there is no choice to make. With two or more, it reads the FPGA of each probe with ``openFPGALoader
--board <board> --ftdi-serial <serial> --detect``. When the ``cable`` setting gives the cable, it
passes ``--cable <cable>`` instead of ``--board``. A board gives the loader its pins and clock,
which a bit-bang cable such as the ULX3S one (``ft231X`` or ``ft232RL``) needs. Probe selection
skips a bit-bang cable given directly with ``cable``; a board-supplied cable has pin assignments
that Xeda can use.

``--detect`` reads the JTAG ID code. It does not configure or reset the FPGA, but it resets the
JTAG state machine, sets the lines of the FTDI chip, and opens every probe with that USB id,
whether it is a programmer or not. For example, the cable of the ``stlv7325_v2`` board is
``ft4232`` (``0x0403:0x6011``), the id of the FT4232H in a Xilinx debugger, so Xeda opens a
connected debugger too. ``usb_serial_num`` names the probe and skips all of this.

A detected model matches the part when both, in lower case and without punctuation, start alike
and the part does not continue with a digit: ``xc7a35`` matches ``xc7a35tcpg236-1``, not
``xc7a350t``. With ``index_chain``, the device at that index of the chain is the one that counts.
Without it, any FPGA of the chain counts. The matching knows the model names of Xilinx 7 series,
UltraScale and UltraScale+, Lattice ECP5, MachXO2 and Nexus, Gowin (by series and capacity), and the
Intel MAX II, MAX 10, Cyclone II to V, and Agilex 3 and 5 families.

The flow programs a probe only when exactly one probe matches and every other probe was read and
shows another device. It then adds ``--ftdi-serial <serial>`` to the command line and logs the
probe it chose and the devices on the others. Every supported loader has ``--ftdi-serial``. Release
1.2.0 prefers ``--usb-serial-num`` and keeps ``--ftdi-serial`` as a deprecated spelling. The
``usb_serial_num`` setting passes ``--ftdi-serial`` too.

A probe whose device cannot be read (it is busy, a permission is missing, or the cable prints no
device) is unknown, not excluded. It may be a second board with the same FPGA, and the part cannot
tell two such boards apart. So the run fails before it programs anything when no probe matches,
when several match, and when one matches beside an unknown probe. The message lists every probe
with its serial number, its bus and device number, the device that was detected or why it could not
be read, and the ``usb_serial_num`` setting that names it. ``cable_index`` also works.

When two or more probes fit the cable, the flow does not choose and warns in these cases.
openFPGALoader then opens the first probe that fits:

* No part is known.
* Xeda cannot match the part to the loader's model names reliably: iCE40 (it has no JTAG ID code),
  Spartan-6, MachXO3, Arria 10 and others.
* The cable is a bit-bang cable given with ``cable`` (``ft231X``, ``ft232RL``). Its pins come only
  from a board.
* The probes are not FTDI chips. A USB-Blaster, CMSIS-DAP or J-Link cable opens the first device
  of its id.
* A probe has no serial number, or shares it with another.
* The scan prints no table that Xeda can read, or says that it could not open a probe of that id
  (for example a missing permission).

Xeda names a probe only by the serial number of an FTDI chip. It does not use ``--busdev-num``,
which the loader honors for an FTDI probe only when it was built with libftdi 1.4 or newer, and Xeda
cannot detect the build. In these cases, name the probe with ``cable_index``, or with
``usb_serial_num`` when its serial number is unique. To list the serial numbers, run
``openFPGALoader --scan-usb``.

The queries run in a real run only, never in a plan or ``--dry-run``. They run outside the run
directory, so they write nothing there, and their output is not in ``openfpgaloader.log``. Each has
a time limit. If Xeda cannot get the cable or probe list, openFPGALoader chooses the probe.
If Xeda cannot read a probe with ``--detect``, the flow stops before programming.

Xeda needs openFPGALoader 0.13.1 or newer. It asks the loader for its version (``-V``) before any
flow of the chain runs, so an older loader fails the launch at once, before Xeda builds anything.
The error has the version that Xeda found and the version it needs. The loader prints the DONE
state of a Xilinx FPGA only since release 0.13.0, so with an older loader, a load that left DONE
low would pass. Release 0.13.0 prints its version as 0.12.1, so Xeda cannot tell it from the older
releases and refuses it too.

The run fails when the loader exits with a nonzero status. It also fails when the log shows that
the device was not programmed, because openFPGALoader 1.1.1 exits with status 0 after several
failures. The error of the run (``reported_failure``) says what happened and quotes the lines of the
log that show it. Xeda looks for these signs:

* DONE stayed low after a load into a Xilinx FPGA. The loader prints a line such as ``ir: 1
  isc_done 0 isc_ena 0 init 1 done 0`` and then the status register of the FPGA. If the register
  reports an ID error, the bitstream is for another device than the one on the cable, so check the
  board. The loader uses the first cable that matches. With several boards of one kind connected,
  the flow chooses the probe whose FPGA matches the part (see above). It does not choose when the
  part is unknown or Xeda cannot match it, when the run names a probe (``cable_index``,
  ``usb_serial_num`` or ``extra_args``), or when the probes cannot be told apart. Then the loader
  can program the wrong board. The error tells whether Xeda chose the probe, and which settings
  name one.
  If the register reports a CRC error, the bitstream is damaged or incomplete.
* A step that printed ``FAIL`` or ``Fail``, such as a bitstream file that the loader cannot parse.
* An error message that the loader prints before it ends with status 0. A flash write that fails
  does this: the loader prints why and exits with status 0. Xeda knows the lines that start with
  ``Error:``, ``Can't program``, ``FAIL:`` or ``Verification failed at``, and lines that end with,
  for example, ``Read ID failed``, ``Failed to read flash``, ``flash overflow``, ``disable
  protection failed``, ``wait: Error`` or ``write en: Error``. They show that the flash does not
  answer, is write-protected, is too small, does not accept the data, or holds other data than the
  file.
* A flash write for which the loader knows no part. The loader names the bridge that writes a flash
  after the part of the FPGA. With ``--board``, it takes the part from its own list of boards, and
  that list gives some boards no part (``kc705`` is one). The loader then prints ``Can't program
  SPI flash: missing device-package information``, writes nothing and exits with status 0. Set
  ``cable`` as well (``digilent`` for ``kc705``; ``openFPGALoader --list-boards`` shows the cable
  of each board, and ``Undefined`` where the part is missing). The flow then gives the loader the
  part from ``fpga`` (``--fpga-part``) in place of the name of the board.

A flash write on a Xilinx FPGA must also say that it finished, and Xeda fails the run when it does
not. The loader can stop before it writes without any message, and exit with status 0. A flash of
the SST26VF family that stays locked after the loader unlocked it does this. The flash then holds
its old data, or only part of the file when a write stopped half way. A write that finished ends
with a progress bar and the word ``Done``:

.. code-block:: text

   Writing: [==================================================] 100.00%
   Done

With ``verbose_level: -1`` (or ``--quiet`` in ``extra_args``) the loader prints ``Writing: Done``
instead. Xeda looks for these lines once for each flash chip it writes (twice with
``target_flash: both``). It checked the source of the loader from 0.13.1 to 1.1.1 for them. A later
release that prints another bar fails every Xilinx flash write. The error names the line that this
run needs, how many finished writes it needs and how many the log shows, and what the log holds
after the last one, so read the log. ``verify: true`` makes the loader read the flash back after a
write that finished, and it prints ``Verification failed at`` and an address if the flash holds
other data than the file. The loader does not read the flash back when the write stopped, so the
lines above are the only check for that.

Xeda requires no other sign of success, because the other families and versions print other words
or nothing. A load that fails in another way, with status 0 and none of these signs, passes, so
read the log. A run without a log of its own fails, because it leaves no evidence that the device
was programmed. A Lattice load needs no sign in the log, because the loader exits with a nonzero
status when it fails. An SRAM load needs none either.

Xeda does not notice an in-place change of the installed Project X-Ray database alone, with
``fpga-as`` itself unchanged, when it packs a prebuilt ``Fasm`` source. The files that a tool reads
from its own installation are not inputs, and ``--rebuild-all`` runs everything. It does not notice
a new directory for the exact part either. A ``prjxray_db`` that you set is a setting that names a
directory, and Xeda tracks it as it tracks any such setting.

There is no ``open_xc7`` flow. Running it, or keeping a ``flows.open_xc7`` section in a design
file or project file, fails with "``open_xc7`` was removed: use fpga_pack to build,
openfpgaloader to program". Put the ``nextpnr`` settings in ``flows.nextpnr``, the synthesis
settings in ``flows.yosys_fpga``, and the pin files in ``rtl.sources``. The ``openfpgaloader``
flow refuses the ``nextpnr``, ``packer_args`` and ``bitstream_file`` settings in the same way: use
the ``nextpnr`` and ``fpga_pack`` sections, and a ``Bitstream`` source for a prebuilt file.
``xeda scrub open_xc7 <design>`` removes the run directories that an ``open_xc7`` flow left.

.. _vivado-impl:

Vivado place and route of a yosys netlist
-----------------------------------------

``vivado_impl`` implements a netlist in Vivado, in non-project mode. It reads the constraints and
an EDIF netlist, links the design for the part, and runs the implementation steps of
``vivado_alt_synth``. With the default strategy these are ``opt_design``, ``place_design``, a second
``opt_design``, ``phys_opt_design`` (twice) and ``route_design``. It does no synthesis. Its
``netlist`` input is an ``Edif`` design source or, by default, the EDIF netlist that ``yosys_fpga``
writes for a Xilinx device. So Vivado can place and route the open-source synthesis, and you can
compare it with ``vivado_synth`` and ``vivado_alt_synth`` on one design. The design needs
``rtl.top``, a Xilinx part, a clock, and pin constraints as ``Xdc`` sources (or ``xdc_files``):

.. code-block:: yaml

    # vivado_demo.yaml
    name: blinky
    rtl:
      sources:
        - "blinky.v"
        - "blinky.xdc"   # pins: an Xdc source, typed by its suffix
      top: blinky
      clock.port: clk
    flows:
      vivado_impl:
        fpga.part: xc7a35tcpg236-1
        clock.period: 10 ns

.. code-block:: bash

    xeda run yosys_fpga+vivado_impl blinky.yaml                              # reports and results
    xeda run yosys_fpga+vivado_impl blinky.yaml -s bitstream=$PWD/blinky.bit # and a bitstream
    xeda run yosys_fpga+vivado_impl+openfpgaloader blinky.yaml --dry-run     # plan the loader too

``fpga`` and ``clock`` are shared along the edge, so one section gives them to both flows, and one
clock period drives the ABC9 delay of Yosys and the constraint of Vivado. Xeda writes the bitstream
when ``bitstream`` is set, or when a flow after ``vivado_impl`` needs it, such as
``openfpgaloader``. A bitstream needs every port to be constrained. Without ``bitstream``, Vivado
stops after routing, and the flow reports timing and utilization only. The results are those of
``vivado_synth`` (``Fmax``, ``wns``, ``lut``, ``ff``, ``dsp`` and others), read from the reports of
Vivado for the routed design. ``impl.strategy`` and ``impl.steps`` choose the steps and their
options as for ``vivado_alt_synth``. Only 7-series parts were tested.

Files of the routed design
~~~~~~~~~~~~~~~~~~~~~~~~~~

``vivado_impl`` writes the same files as ``vivado_alt_synth``, switched on by the settings of the
same names: ``write_checkpoint``, ``write_netlist`` and ``write_timing_netlist``. Each file is a
declared output, with the same name and type as in the synthesis flows, so ``vivado_postsynth_sim``
and ``vivado_power`` can read them. The checkpoint after placement and the exported constraints are
artifacts and not declared outputs. There is no synthesis checkpoint, because there is no
synthesis.

.. list-table::
    :header-rows: 1

    * - Setting
      - Output
      - File
    * - ``write_checkpoint``
      - ``checkpoint_route``
      - ``checkpoints/post_route.dcp`` (and ``post_place.dcp`` beside it)
    * - ``write_netlist``
      - ``netlist``
      - ``outputs/impl_funcsim.v`` (and the constraints ``outputs/impl.xdc``)
    * - ``write_timing_netlist``
      - ``netlist_timing``, ``sdf``
      - ``outputs/impl_timesim.v``, ``outputs/impl_timesim.sdf`` (slow corner)
    * - ``bitstream``
      - ``bitstream``
      - the file the setting names

The consumers take ``vivado_synth`` by default, so bind them to ``vivado_impl`` as for
``vivado_alt_synth`` (see `Related inputs`_). The chain check sees declarations only, so
``yosys_fpga+vivado_impl+vivado_power`` passes it, but planning refuses it until the three inputs
of the simulation that makes the activity come from ``vivado_impl`` too. Add these items to ``-s``
(the chain binds the checkpoint itself; use the same items with ``vivado_postsynth_sim`` to
simulate the routed netlists alone):

.. code-block:: text

    -s flows.vivado_postsynth_sim.inputs.netlist=vivado_impl.netlist flows.vivado_postsynth_sim.inputs.netlist_timing=vivado_impl.netlist_timing flows.vivado_postsynth_sim.inputs.sdf=vivado_impl.sdf

The power report needs a testbench in the design (``tb``), as for the other Vivado flows.

The EDIF netlist
~~~~~~~~~~~~~~~~

``yosys_fpga`` writes ``netlist.edif`` beside its JSON netlist, with ``write_edif -pvector bra``,
when its settings make a Xilinx synthesis flat. That means ``flatten: true``, or ``flatten`` unset
and ``synth_pass_only`` off; no ``keep_hierarchy`` (the setting, or an attribute of that name in
``set_mod_attribute`` or ``set_attribute``); no ``black_box``; and no ``stop_after``. Nothing asks
for the file, so ``yosys_fpga+nextpnr`` and ``yosys_fpga+vivado_impl`` share one synthesis run.
``netlist_edif`` names the file, or a location that Xeda delivers it to. Vivado reads a flat
netlist only: for a hierarchical one it finds the modules undefined and treats them as black boxes.
So where the settings show a hierarchy, the synthesis writes no EDIF netlist, and Xeda refuses
``yosys_fpga+vivado_impl`` while planning and names the setting (``flatten`` for ``flatten:
false``). The synthesis itself is not refused, and ``netlist_edif`` is an error where none is
written. The settings cannot show a ``(* keep_hierarchy *)`` attribute in the HDL, or one that the
design gives in ``rtl.attributes``, but the netlist does show it. ``vivado_impl`` reads the netlist
before it starts Vivado, and stops with the modules named.

Pin constraints name the ports as the netlist does. Yosys writes a one-bit bus (``input [0:0] a``)
as the scalar port ``a``, so its constraint is ``get_ports a``, and ``get_ports {a[0]}`` matches
nothing. A port that no constraint matches stops ``write_bitstream``.

A netlist from elsewhere
~~~~~~~~~~~~~~~~~~~~~~~~

List the netlist in ``rtl.sources``. The ``.edf`` and ``.edif`` suffixes are typed ``Edif``, and
``xeda run vivado_impl blinky.yaml`` then runs no synthesis. Whatever the file is called, the flow
copies it to ``<top>.edif`` in its run directory, because Vivado finds the top module of an EDIF
netlist by the name of its file. The netlist has to be flat, and written with the ranges of its
buses. In Yosys, run ``synth_xilinx -flatten -top <top>``, then ``write_edif -pvector bra
<top>.edif``. The flow stops before it starts Vivado for a netlist whose instances refer, as
library cells, to modules that the netlist itself defines, which is how Yosys writes a hierarchy.
It cannot tell a black box from a primitive, and it cannot see a bus written without its range:
Vivado reads such a bus with its bits reversed, and says nothing about it. A Verilog netlist is not
an input. When Vivado reads a Verilog netlist, it drops the contents of a block RAM that has
undefined bits, and it warns only with a critical warning (``[Netlist 29-72]``).

.. _yosys-standard-cell-mapping:

Yosys standard-cell mapping
===========================

``yosys`` maps a design to a standard-cell library when the flow has ``liberty`` files, a
``platform`` or a ``platform_file``. Mapping to library cells and mapping to the generic gates or
LUTs of Yosys contradict each other, so ``gates`` or ``lut`` together with one of these three is
refused. The platform supplies the Liberty files, the mapping files, the tie and buffer cells, and
the defaults that this section describes. Set ``platform`` to a bundled platform name, or
``platform_file`` to a platform of your own (see
:doc:`platforms`).

``yosys`` has two netlist outputs. A run that writes the Verilog netlist (``netlist_verilog``)
writes exactly one of them. ``netlist`` (type ``VerilogNetlist``) is written when the run maps to a
Liberty library: ``liberty``, ``platform`` or ``platform_file`` is set, and ``gates`` and ``lut``
are not. A Liberty mapping with ``nosynth`` is refused. ``netlist_generic`` (type
``GenericVerilogNetlist``) is written in all other cases: ``gates``, ``lut``, ``nosynth``, or no
platform and no ``liberty``. ``results.json`` records the
netlist of a run under ``outputs.netlist`` or ``outputs.netlist_generic``, whichever the run
wrote. The ``netlist_verilog`` setting and the delivered file are the same for both.
``openroad_place`` takes only ``netlist``, because a netlist of the cells of Yosys has no Liberty
library.

Reading the sources
-------------------

``yosys``, ``yosys_fpga`` and ``yosys_sim`` read Verilog sources with ``read_verilog``. ``yosys`` and
``yosys_fpga`` read the sources of ``rtl``. ``yosys_sim`` reads the sources of ``rtl`` and of ``tb``.
When these sources include a SystemVerilog source and ``systemverilog`` is ``slang``, one
``read_slang`` reads all their Verilog and SystemVerilog sources together, in design order, as
OpenROAD-flow-scripts does. The sources are then one compilation. A module or a package that one
file declares is known in all the others. ``read_systemverilog_flags`` apply to that command, and
``read_verilog_flags`` do not apply to the Verilog sources it reads. The ``yosys_sim`` flow reads
SystemVerilog with the built-in reader unless ``systemverilog`` says otherwise.

When ``read_slang`` reads the design and its top module is not VHDL, the command also sets the
parameters of the top module, with ``-G NAME=VALUE`` as OpenROAD-flow-scripts does, and the script
has no ``chparam``. GHDL sets the generics of a VHDL top module, and the command then has no
``-G``. slang elaborates a module while
it reads the sources, so ``chparam`` cannot change a parameter afterwards. An integer, a sized
literal such as ``7'b0101001``, a boolean (``1'b1`` or ``1'b0``), a string and a real number all
reach the top in the form that slang needs. Xeda writes a string value between double quotes
without an escape, so a double quote inside the value would end it, and Xeda refuses such a
parameter when slang reads the design. The tokenizer of a Yosys script (not of a Tcl script) also
ends a value at whitespace, so Xeda refuses whitespace when it writes a Yosys script. Change the
value, or set ``systemverilog: default``.

slang resolves every module instance when it reads the sources. So the script reads the files of
``verilog_lib``, and the Liberty files that the flow reads itself, before the ``read_slang``
command. The script reads VHDL sources after it. A top module in Verilog or
SystemVerilog cannot instantiate a VHDL entity.

A design with Verilog sources only still uses ``read_verilog`` and ``chparam``, and so does a design
that sets ``systemverilog: default``.

The mapping script
------------------

Xeda writes the mapping as a Yosys script, or as a Tcl script when an option needs Tcl (see `Options
that need Tcl`_). Xeda selects the format, and no setting chooses it. The list below is the default
mapping, in the order of OpenROAD-flow-scripts, and both formats hold these commands. The options
that need Tcl add or replace commands: operator wrapping replaces the fine part of ``synth`` with
``wrap_operators``, and hierarchical synthesis adds ``keep_hierarchy -min_cost``. In both formats,
``abc.mapper: abc_new`` replaces ``abc``. The script in the run directory, ``yosys_synth.ys`` or
``yosys_synth.tcl``, has every command. The main steps are these:

.. code-block:: text

    read_liberty -overwrite -setattr liberty_cell -lib <file>  # each Liberty file, as cells
    read the design; proc; flatten                    # flatten, unless flatten: false
    techmap -map <map>                                # techmap_files at pre_synth
    synth -run :fine                                  # the coarse part of synth
    memory_libmap; techmap -map <map>                 # memory.libmap, memory.techmap
    abc -dff -script <retiming script>                # sequential.retime_modules only
    techmap -map <map>                                # techmap_files at post_coarse
    synth -run fine: -noabc                           # the fine part of synth
    chformal -remove; rename -wire; opt -purge
    extract_fa; techmap -map <adder map>              # arithmetic.adder_map
    techmap; opt -fast -purge
    dfflegalize; techmap -map <latch map>             # sequential.latch_map
    techmap -map <map>                                # techmap_files at pre_dff
    clockgate                                         # sequential.clock_gating.enabled only
    dfflegalize; dfflibmap -liberty <file>            # or the flip-flop map, sequential.dff_map
    techmap -map <map>                                # techmap_files at post_dff
    opt; setundef -zero                               # finish.tie_undefined
    abc -liberty <file>                               # the logic mapping; -D <ps> with abc.delay_target
    splitnets; opt_clean -purge; hilomap; insbuf
    rmports; autoname; attrmap; check -assert -mapped
    write_verilog                                     # the netlist

Some steps run only when a setting asks for them. The comments name those settings.

With ``prep``, the script runs ``prep`` with its flags in place of ``proc`` and ``flatten``. Xeda
adds ``-flatten`` unless ``flatten`` is false, and ``-top`` with the top module. ``coarse.latches``
is an option of the ``proc`` that ``prep`` replaces, so Xeda refuses it with ``prep``.

Xeda reads each Liberty file as it is. It does not patch, merge or write a Liberty file. Each file
goes to ``dfflibmap`` and to ``abc``. ``sequential.dff_liberty`` names a flip-flop file that
replaces the Liberty files on ``dfflibmap``. ``abc`` still reads all of them.

The cells that the flow must not use reach ``dfflibmap`` and ``abc`` as ``-dont_use`` words, one
word for each cell. The cells of the platform come first, then the cells of ``dont_use_cells``, and
each name appears once. A name is a glob pattern: ``*`` matches any number of characters, ``?``
matches one character, and ``[...]`` matches one character of a set. Xeda passes each name as it
is.

Xeda refuses ``dont_use_cells`` together with ``gates`` or ``lut``. Those settings map to generic
cells, which have no library cells to exclude.

Settings groups
---------------

The other settings of the mapping are in seven groups, one for each stage. A group holds the
settings of one stage, and its common settings are these:

``hierarchy``
    The modules that stay separate after the flattening, the separator of flattened names, and
    the optimization across module boundaries. Common setting: ``hierarchy.keep_min_cost`` (Tcl
    only).

``coarse``
    The coarse passes of ``synth``: finite state machines, resource sharing, the report of
    latches, and the removal of flip-flops with a SAT solver. No common setting.

``arithmetic``
    The mapping of adders and other operators: the carry architecture, the full-adder map, the
    Booth multipliers and the operator wrapping. Common settings: ``arithmetic.adder_architecture``,
    ``arithmetic.adder_map`` and ``arithmetic.extract_fa``.

``memory``
    The mapping of memories to library cells, and the memory size limit. No common setting.

``sequential``
    The latches, clock gates, flip-flops and retiming, and the conversion of tri-state buffers.
    Common setting: ``sequential.clock_gating``.

``abc``
    The logic mapping with ABC: the strategy, the driving cell and the load of the ports, and the
    delay target. Common settings: ``abc.strategy``, ``abc.driver_cell`` and ``abc.load``. The
    mapping passes no ``-D`` option to ``abc`` unless ``abc.delay_target`` is set, as
    OpenROAD-flow-scripts does. ``abc.delay_target: auto`` passes the clock period whenever the flow
    has a clock. ``abc.delay_target`` and ``abc.max_slew``
    are times and ``abc.load`` is a capacitance. Each needs a unit, such as ``500 ps`` or
    ``3.9 fF`` (see :ref:`quantities`). ABC reads picoseconds and femtofarads, and Xeda converts.
    ``abc.max_slew`` is at least 1 ps and a whole number of picoseconds. ``abc.dress`` passes
    ``-dress`` to ``abc``.
    It is off by default. It is experimental in ABC: with a Liberty library it maps to LUTs
    instead of library cells (YosysHQ/yosys#3644), and it fails when ``abc`` retimes
    (berkeley-abc/abc#445).
    ``abc.mapper: abc_new`` maps with the ``abc_new`` command of yosys instead, in a ``ys`` or a
    ``tcl`` script. The command is experimental in yosys. It maps with a script that works on a GIA
    and ends in a mapped one (``&nf``): ``abc_script``, or else the script
    ``abc_speed_gia_only.script`` of OpenROAD-flow-scripts that Xeda writes into the run directory.
    With ``arithmetic.wrap_operators``, each wrapped operator maps with its own script unless
    ``abc_script`` replaces it. Xeda refuses ``abc.strategy`` other than ``area``, because those
    scripts end in a network, and ``abc.liberty_args`` and ``abc.dress: true``, which ``abc_new`` has
    no option for.

``finish``
    The commands after the mapping: the undefined values, the tie and buffer cells, the formal
    cells, the names of the objects, and the checks. Common settings: ``finish.tie_undefined``,
    ``finish.tie_cells`` and ``finish.buffer_cell``.

The setting ``techmap_files`` is not a group. It lists Verilog maps that ``techmap`` applies at the
point that each entry names in ``when``: ``pre_synth``, ``post_coarse``, ``pre_dff`` or
``post_dff``. A bare path is a map at ``pre_dff``. Maps at one point run in the order of the list.

``xeda list-settings yosys --tier common`` lists the common settings. ``xeda list-settings yosys
--tier expert`` lists every setting, which is also the default.

Options that need Tcl
---------------------

Some options need a Tcl script. When one of them is set, Xeda writes the script in Tcl, and
otherwise in the script language of Yosys, which every build runs. A Tcl script needs a yosys
build with Tcl support: a build without it fails, and the log of Xeda names the option.

``hierarchy.keep_min_cost``
    Keeps each module whose cost is above this number of NAND2 gate equivalents as a module of its
    own. The other modules are flattened. It is refused with ``flatten: false``.

``arithmetic.wrap_operators``
    Wraps each wide ``$alu`` and ``$macc`` cell in a module for each architecture, so that the
    place and route tool can swap them. The first architecture in ``arithmetic.wrapped_adders``
    or ``arithmetic.wrapped_multipliers`` builds the module that the design instantiates. The
    netlist keeps the attributes that the swap reads (``arithmetic_operator``,
    ``implements_operator``, ``architecture`` and ``source_cell``). With ``abc.mapper: abc_new``,
    ABC maps each wrapped operator with the script that the operator carries, as
    OpenROAD-flow-scripts does. With ``abc``, ABC maps every module with the script of
    ``abc.strategy``.
    Xeda refuses ``netlist_attrs: false`` and a ``netlist_unset_attributes`` entry that names one
    of these attributes.

``memory.max_bits``
    Stops the run when a memory is larger than this many bits. The error suggests raising
    ``memory.max_bits`` or setting ``memory.mock_large``. ``memory.mock_large`` shrinks each such
    memory to one row, as OpenROAD-flow-scripts does. The netlist is then not a correct
    implementation of the design. ``memory.keep_mocked`` (true by default) gives the module of a
    shrunk memory the ``keep_hierarchy`` attribute. That attribute keeps the module separate only
    when nothing flattened it before.

Settings that a liberty mapping refuses
---------------------------------------

A liberty mapping runs its own elaboration and mapping, and it ends with the tail of
OpenROAD-flow-scripts. Xeda refuses these settings with such a mapping, and each message names the
setting:

* ``abc_dff: true``. The mapping maps the flip-flops with ``dfflibmap`` before ``abc`` runs, so
  ``-dff`` has no effect. Use ``sequential.retime_modules`` to retime. Set it to false or delete
  it.
* ``post_synth_opt: true``. The mapping runs no extra ``opt`` after ``synth``, so the setting has
  no effect. Use ``coarse.opt_dff_sat`` to add ``-sat`` to the ``opt`` that follows ``synth``. Set
  it to false or delete it.
* ``nosynth: true``. Mapping to library cells needs ``synth`` to reduce the design to gates
  first. Set it to false or delete it.
* ``noabc: true``. The mapping always maps with ``abc``. Set it to false or delete it.

A group setting that acts on a liberty mapping, such as ``abc.load``, is refused without a
``platform``, ``platform_file`` or ``liberty``.

Defaults from the platform
--------------------------

A leaf whose value is ``null`` takes the value of the platform when the platform has one. These are
``abc.driver_cell`` (the driving cell), ``abc.load`` (the load on the outputs), ``finish.tie_cells``
and ``finish.buffer_cell``. Three leaves take the word ``platform``, which uses the map of the
platform: ``arithmetic.adder_map``, ``sequential.latch_map`` and ``sequential.clockgate_map``. Each
of them uses its ``*_file`` setting first, when that is set, and ``none`` turns the map off.
The clock gate map of ``sky130hd`` is a file that Xeda ships. It defines the module
``OPENROAD_CLKGATE`` (ports ``CK``, ``E`` and ``GCK``), as the clock gate maps of
OpenROAD-flow-scripts do. The module is the cell ``sky130_fd_sc_hd__dlclkp_1`` when the macro
``OPENROAD_CLKGATE`` is defined (``defines`` of the flow), and a wire from ``CK`` to ``GCK``
otherwise. The flow reads the clock gate map with the same ``defines`` as the sources.
A value that you set replaces the value of the platform. The Liberty files you list in ``liberty``
replace the Liberty files of the platform. ``dont_use_cells`` adds to the cells of the platform.

Removed settings
----------------

``yosys`` refuses these settings. Each error names the replacement.

.. list-table::
   :header-rows: 1

   * - Removed setting
     - Use instead
   * - ``optimize``
     - ``abc.strategy``. Use ``speed_roundtrip`` for ``speed`` and ``yosys`` for ``null``.
   * - ``abc_driver_cell``
     - ``abc.driver_cell``
   * - ``abc_load_in_ff``
     - ``abc.load``
   * - ``adder_map``
     - ``arithmetic.adder_map_file`` for a file, or ``arithmetic.adder_map: none``
   * - ``clockgate_map``
     - ``sequential.clockgate_map_file`` for a file, or ``sequential.clockgate_map: none``
   * - ``dff_liberty`` and ``dff_library``
     - ``sequential.dff_liberty``
   * - ``other_maps``
     - ``sequential.latch_map_file`` for the latch map, ``sequential.latch_map: none`` for no
       latch map, and ``techmap_files`` for the other maps
   * - ``hilomap``
     - ``finish.tie_cells``
   * - ``insbuf``
     - ``finish.buffer_cell``
   * - ``merge_libs_to``
     - None. Xeda does not merge Liberty files. Delete the setting, since ``dont_use_cells``
       reaches ``dfflibmap`` and ``abc`` as ``-dont_use``.
   * - ``retime``
     - ``abc_dff``, which passes ``-dff`` to ``abc`` (not with a liberty mapping), and
       ``abc_flags`` for more options of ``abc``. To retime the modules of a liberty mapping, use
       ``sequential.retime_modules``.
   * - ``netlist_verilog_extmem``
     - ``netlist_verilog_flags: [-extmem]``

Example
-------

The file below sets several of these settings. It is a design file whose ``yosys`` section uses
nested tables and dotted keys.

.. code-block:: yaml

    # settings-groups.yaml
    name: settings_groups
    rtl:
      top: top
      sources:
        - top.v
    flows:
      yosys:
        clock.period: 2 ns
        liberty:
          - cells.lib
        abc:
          strategy: speed
          load: 0.5 fF
        arithmetic.adder_map: none
        finish.tie_undefined: none
        sequential:
          clock_gating:
            enabled: true
            min_width: 4
        techmap_files:
          - path: maps/cells.v
            when: pre_dff

OpenROAD place and route
========================

Four flows take a gate-level netlist through OpenROAD to a routed design. Each flow hands an
OpenROAD database (``.odb``) and an SDC file to the next one:

.. code-block:: text

    yosys -> openroad_place -> openroad_cts -> openroad_grt -> openroad

``openroad_place``, ``openroad_cts`` and ``openroad_grt`` each run one OpenROAD process.
``openroad`` runs two: the first routes the design in detail, and the second writes the final
outputs. With ``save_images``, a third process saves the images of the routed design.

``yosys`` is the default producer of the netlist, so ``xeda run openroad`` and
``xeda run yosys+openroad`` plan the same five flows. A changed setting of a late flow reruns only
that flow and the flows after it. A shared setting, such as ``wire_rc`` or ``platform``, belongs to
every flow of the chain, so its change reruns all of them. Every flow needs a ``platform`` (see
:doc:`platforms`), and the PDK files of the platform must be in the PDK store
(``xeda pdk install nangate45``).

The flows follow the order of the stages of OpenROAD-flow-scripts. Where a setting has the default
of OpenROAD-flow-scripts, of the tool or of the platform, its description says so. Run
``xeda list-settings <flow> --tier common`` for the settings that most designs change, and
``xeda list-settings <flow>`` for all of them.

The flows
---------

``openroad_place``
    Reads the netlist and the constraints. It makes the floorplan, places the tap cells, builds
    the power grid, places the pins and the cells, optimizes the design and legalizes the
    placement. It writes ``place.odb`` and ``place.sdc``. Its groups are ``design``,
    ``floorplan``, ``tapcell``, ``pdn``, ``io``, ``global_placement``, ``resize``, ``arith_swap``,
    ``detailed_placement``, ``routing`` and ``tie``. The constraints are the SDC files of the
    design, then the clocks of ``clocks`` (or ``clock.period``), then ``sdc_files``.
``openroad_cts``
    Builds the clock tree, legalizes the placement and repairs the timing with propagated clocks.
    It writes ``cts.odb`` and ``cts.sdc``. Its groups are ``buffers``, ``sink_clustering``,
    ``macro_clustering``, ``tree``, ``balance``, ``ndr``, ``characterization``,
    ``repair_clock_nets``, ``legalize`` and ``repair``.
``openroad_grt``
    Routes the design globally, repairs the design and the timing with the parasitics of the
    route, and repairs the antenna violations where the platform has a diode that the router can
    use. It writes ``grt.odb``, ``grt.sdc`` and the route guides ``grt.guide``. Its groups are
    ``pin_access``, ``congestion``, ``router``, ``legalize``, ``repair`` and ``antenna``.
``openroad``
    Routes the design in detail, repairs the antenna violations of the route, places the filler
    cells, and writes and reports the final design. Its groups are ``detailed_route``, ``ndr``,
    ``antenna``, ``fill``, ``extraction``, ``reports``, ``ir_drop`` and ``gds``.

Settings that travel in the database
------------------------------------

The database keeps the routing layers (``routing.signal_layers``, ``routing.clock_layers``), the
layer adjustments (``routing.layer_adjustments``), the limits of the sizing
(``resize.opt_config``), the options of the global sizing (``resize.global_sizing``) and the
cells that the resizer must not touch (``design.dont_touch``). These settings belong to
``openroad_place`` only. A change of one of them reruns ``openroad_place`` and every flow after
it.

The database keeps neither the wire resistance and capacitance nor the way it was read.
``wire_rc`` and ``hierarchical`` are therefore settings of every OpenROAD flow, and they must agree
along the chain (see `Producers and consumers`_). ``wire_rc`` replaces the wire RC of the platform
layer by layer. ``dont_use_cells`` adds to the cells that the platform excludes.

Each OpenROAD flow also has the shared setting ``liberty`` (alias ``library``). It names the
Liberty files that the flow times against, in place of the Liberty files of the corner of the
platform. The platform model still comes from the platform: the units, the tie cells, the wire RC
and the site. So the platform files must be installed. The flow refuses a ``liberty`` file whose
time unit differs from the time unit of the platform, because the stages scale all times by the
unit of the platform. OpenSTA reads a time unit only as 1, 10 or 100 and a unit (``1ns``, ``10ps``)
without spaces. It reads ``1000ps`` as ``1ps`` and ``1.0ns`` as one second, so the flow refuses any
other spelling. OpenROAD also reads the wire capacitance of the platform in the capacitance unit of
the first Liberty file, so the flow refuses a first file with another capacitance unit. A later
file can have its own. ``liberty`` must agree along the chain with the same files in the same order
(see `Producers and consumers`_).

The verdict of the last flow
----------------------------

``openroad`` judges the routed design with three checks, all on by default:

``fail_timing``
    A negative setup or hold slack rejects the run.
``fail_drc``
    A design rule violation that the detailed route leaves rejects the run.
``fail_antenna``
    An antenna violation that remains after the repair rejects the run.

One rejection names every check that failed. The run keeps its outputs and its trace, and the next
launch reports the same rejection again without running OpenROAD, until a setting, an input or
the outputs change. A check that you switch off does not reject the run, but its value must still
be in the metrics: a missing or malformed value fails the run.

``openroad_place`` stops on an illegal placement (``detailed_placement.check: fail``, as
OpenROAD-flow-scripts does). Set it to ``report`` to print the violations and go on.

Outputs and results
-------------------

``openroad`` writes ``final.odb``, ``final.sdc``, ``final.def``, the netlist ``final.v`` without
physical-only cells, ``final.sdf``, the abstract ``final.lef``, ``final.cdl`` when the platform
gives CDL files, and ``final.spef`` when OpenRCX extracts the parasitics
(``extraction.method``). ``write_timing_model`` adds the Liberty model ``final.lib``, and
``write_gds`` adds the layout ``final.gds``, which KLayout writes from the DEF file and the GDSII
files of the platform. ``save_images`` saves images of the routed design. The results hold the
slack, the area, the power and the counts of the violations. The IR drop of each supply net is in
a report in the directory ``reports`` of the run directory.

When a stage fails, its process writes the database of that moment as ``<flow>-failed.odb`` in
the run directory, for example ``openroad_cts-failed.odb``. Open it in OpenROAD to see where the
stage stopped. The flow deletes it before its next process starts.

Example
-------

The GCD in ``examples/bluespec/gcd`` has one design file for a simulation, for the ASIC chain, for
Vivado and for two FPGA targets. The section ``flows.yosys`` holds the settings that reach the four
OpenROAD flows. Every other setting keeps its default:

.. code-block:: yaml

    # openroad-gcd.yaml: the design file examples/bluespec/gcd/gcd.yaml.
    name: gcd
    description: >-
      GCD accelerator in BSV with a Get/Put (Server) interface. It deals requests round-robin, through
      bsc library FIFOs, to two binary-GCD engines. The engine is a module synthesized separately in
      another package. The testbench checks 200 directed and LFSR-driven requests against Euclid's
      algorithm.
    rtl:
      # The package that defines `top` comes last.
      sources:
        - "rtl/GcdEngine.bsv"
        - "rtl/GcdUnit.bsv"
      top: mkGcdUnit
      clock.port: CLK
    tb:
      sources:
        - "tb/TbGcdUnit.bsv"
      top: mkTbGcdUnit
      # Negative test: defining the macro XEDA_INJECT_BUG plants a bug in the design. The testbench
      # must catch it (non-zero exit).
      # defines.XEDA_INJECT_BUG: true
    flows:
      # ASIC place and route on nangate45: `xeda run openroad gcd.yaml`. The shared settings reach the
      # four OpenROAD flows along the chain.
      yosys:
        platform: nangate45
        clock.period: 1.5 ns
      # Vivado synthesis of the module on its own: `xeda run vivado_synth gcd.yaml`.
      vivado_synth:
        fpga.part: xc7a100tcsg324-1
        clock.period: 10 ns
        out_of_context: true
    # The FPGA configurations are resource and timing estimates without pins. A board build needs an
    # I/O wrapper and constraints.
    targets:
      # `xeda run yosys_fpga+nextpnr gcd.yaml --target ecp5`
      ecp5:
        flows:
          yosys_fpga:
            fpga.part: LFE5U-85F-6BG381C
            clock.period: 20 ns
          nextpnr:
            lpf_allow_unconstrained: true
      # `xeda run yosys_fpga gcd.yaml --target artix7`. Synthesis only: nextpnr-himbaechel of openXC7
      # refuses unconstrained I/O, and Xeda refuses a Xilinx nextpnr run without XDC files.
      artix7:
        flows:
          yosys_fpga:
            fpga.part: xc7a100tcsg324-1
            clock.period: 10 ns

The design has Bluespec sources, so ``xeda run openroad examples/bluespec/gcd/gcd.yaml`` runs
``bsc`` first, to convert them to Verilog (see `Converted designs`_). It plans ``bsc``, ``yosys``, ``openroad_place``,
``openroad_cts``, ``openroad_grt`` and ``openroad``. The FPGA settings are resource and timing
estimates without pins. A board build needs an I/O wrapper and constraints.

The physical settings take a quantity with its unit (see `Physical quantities and units`_). The
``openroad`` refuses its removed settings, and each message names the setting to use or says why
there is none.

Bluespec
========

``bsc`` and ``bsc_sim`` compile Bluespec with the Bluespec compiler, ``bsc``. Bluespec is BSV
(``.bsv``) or BH, Bluespec Classic (``.bs``). ``bsc`` generates Verilog for ``rtl.top``.
``bsc_sim`` compiles a Bluespec testbench and simulates it. Both need bsc 2026.07.1 or newer, which
Xeda checks when the flow starts.

Bluespec compiles packages in source order, and the package that defines the top module must come
last. For ``bsc``, the last Bluespec source in ``rtl.sources`` must define ``rtl.top``. For
``bsc_sim``, the last one in ``tb.sources`` must define ``tb.top``. For a design without a
testbench, the last one in ``rtl.sources`` must define ``rtl.top``. The name of a package is the
stem of its file, so a design with several packages lists the file of each package, the top last.

``rtl.defines`` and ``rtl.parameters`` (and, for ``bsc_sim``, those of the testbench on top of the
RTL's) are passed to bsc as preprocessor macros. The preprocessor of bsc reads BSV only, so a BH
(``.bs``) source sees the macros only through the C preprocessor. Set ``cpp: true``, which also
feeds the macros to it. Without it, Xeda warns about a design that defines macros and whose
Bluespec sources are all BH.

``bsc`` also makes the output ``design``: the design with the generated Verilog in place of the
Bluespec sources. It is what the flows that read Verilog take (see `Converted designs`_).

``artifacts.verilog`` of ``bsc`` is the set of files that a downstream synthesis or simulation flow
needs. It holds the generated modules and the Verilog sources of the design itself (listed where
they are). It also holds the modules of the bsc library and the modules imported with
``import "BVI"`` that the design instantiates and that are on the Verilog search path. Xeda copies
those into ``verilog_out_dir``. A module that the generated Verilog instantiates and that no
Verilog file defines (a vendor primitive, for instance) is left out of the list, with a warning,
and the downstream tool must provide it. With ``positive_reset`` (on by default), every file that
``bsc`` generated or copied begins with a Verilog macro definition for ``BSV_POSITIVE_RESET``, so
the generated and library modules reset active-high. The Verilog sources of the design itself stay
unchanged. A first file, ``bsv_defines.v``, defines the macro ahead of them, which sets their reset
polarity only where they read the macro themselves. Name the reset port with ``reset_prefix`` if a
downstream flow expects one. ``bsc`` and ``bsc_sim`` refuse a design that defines
``BSV_POSITIVE_RESET`` while ``positive_reset`` is on, because bsc defines the macro itself.

``bsc_sim`` runs the testbench with Bluesim (the default), the cycle-based simulator of bsc, or
with a Verilog simulator through the ``-vsim`` link step of bsc: Verilator, Icarus Verilog or
another simulator that bsc supports. Every accepted backend (listed below) requires an observed
``$finish`` or a confirmed requested limit, with no runtime event reaching ``fail_severity``
(default ``error``). The argument of ``$finish(n)`` is a verbosity level and not an exit status. A
requested Bluesim ``max_cycles`` passes only with the measured cycle count and the final simulated
time. An early explicit finish remains valid. A silent exit 0 and a drained event queue fail.
``bsc_sim`` refuses a run directory whose path has whitespace, with every simulator, before
anything is created and when planning, because the link step of bsc runs its tools through a shell
without quoting. Use a run root without whitespace (``--run-root``).

.. code-block:: bash

    xeda run bsc examples/bluespec/gcd/gcd.yaml
    xeda run bsc_sim examples/bluespec/gcd/gcd.yaml -s simulator=verilator

See ``examples/bluespec/`` for self-checking designs in BSV and BH. They include a design with
several packages, one that macros size, one that imports Verilog with ``import "BVI"``, and one
that mixes BSV and BH. ``examples/bluespec/uart_link`` shows a larger design: a reliable packet
link whose bench compares the exact bytes of frames that a Python codec makes, whose cocotb tests
(``tb/cosim.yaml``) run on the Verilog that ``bsc`` makes, and which has board targets for four
boards and a host library in Python.

.. _converted-designs:

Converted designs
=================

A Bluespec design runs on every flow that reads Verilog. The rule is general. When ``rtl.sources``
holds a language that a flow does not read, and one flow converts that language into languages the
first flow reads, the converter is the default producer of the ``design`` input of the first flow.
Today the converter is ``bsc``, which converts Bluespec to Verilog. From the repository root, run
``xeda run yosys examples/bluespec/gcd/gcd.yaml`` to run ``bsc`` first, then ``yosys`` on the
Verilog that ``bsc`` generated. The dry run shows the reason:

.. code-block:: text

    bsc  xeda_run/gcd/bsc
    yosys  xeda_run/gcd/yosys
        design <- bsc.design (the design has Bluespec sources, which yosys does not read)

A flow that reads Bluespec itself (``bsc_sim``) needs no conversion. These are the commands for the
configurations in ``examples/bluespec/gcd/gcd.yaml``:

.. code-block:: bash

    xeda run bsc_sim examples/bluespec/gcd/gcd.yaml  # Bluesim, no conversion
    xeda run openroad examples/bluespec/gcd/gcd.yaml  # ASIC place and route on nangate45
    xeda run vivado_synth examples/bluespec/gcd/gcd.yaml  # Vivado, out of context
    xeda run yosys_fpga+nextpnr examples/bluespec/gcd/gcd.yaml --target ecp5  # ECP5 place and route
    xeda run yosys_fpga examples/bluespec/gcd/gcd.yaml --target artix7  # Xilinx synthesis only

The FPGA configurations are resource and timing estimates without pins. A board build needs an I/O
wrapper and constraints. The Artix-7 target stops at ``yosys_fpga`` because ``nextpnr-himbaechel``
of openXC7 refuses unconstrained I/O, and Xeda refuses a Xilinx ``nextpnr`` run without XDC files.
``examples/bluespec/ascon`` is a larger design with the same configurations. Its ASIC clock period
is 2.0 ns.

The chain ``bsc+openroad`` and the saved binding ``flows.yosys.inputs.design: bsc`` say what the
default says, so they give the same plan as ``xeda run openroad examples/bluespec/gcd/gcd.yaml``.
A link ``bsc+X`` binds the ``design`` input of every flow in the default graph of ``X`` that
``bsc`` fits, ``X`` included.
When two flows fit, there is no default, and the plan is an error. It names the flows, and it
suggests the chain and the binding that choose one: ``yosys does not read the design's Bluespec
sources, and more than one flow converts them``.

* ``bsc`` passes every source of ``rtl`` that is not Bluespec through, once and in the order of the
  design (Verilog, VHDL and constraint files, for example), before the modules that it generates.
  The flow that reads the converted design judges these sources as it does for any other design.
* The rest of the design is carried unchanged, ``tb`` included. A Verilog testbench or a cocotb
  testbench works on a converted design. A Bluespec testbench needs ``bsc_sim``: a Verilog
  simulator refuses it, and the error names ``bsc_sim``.
* ``--dry-run`` judges the flow on the design that ``bsc`` would derive. While ``bsc`` is stale,
  it reports the later flow as ``unknown: depends on bsc``.
* A remote run converts the design on the remote machine, so ``bsc`` must be installed there (see
  :doc:`remote`).

What reruns after an edit follows from what each flow reads:

* A comment in a Bluespec source reruns ``bsc``. It writes the same Verilog, so the flows after it
  stay up to date.
* An edit that changes the Verilog reruns ``bsc`` and the flow that reads the Verilog. The flows
  after that flow rerun when its outputs change.
* A source that passes through ``bsc`` reruns ``bsc``, because ``bsc`` hashes all of ``rtl``. The
  flow after it reruns too, when it reads that source.
* A setting of the flow that reads the converted design reruns that flow and the flows after it.
  ``bsc`` stays up to date.

.. _flow-results:

Results
=======

After the tools run, a flow parses their reports into ``results``, which Xeda writes to
``results.json`` in the run directory. ``results.success`` decides the exit status of the flow and
of the process.

Every flow reports a common set of keys (``success``, ``runtime``, ``tools``, ``design``, ``flow``,
``run_path``, ``timestamp`` and the hashes). Each flow also reports keys of its own. Run ``xeda
list-results <flow>`` for the list.

Different flows report some quantities under different names. Xeda also records a canonical name
next to the name that the flow reported, so a script does not need to know which
flow produced the file:

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

The original keys stay in ``results.json``.

.. note::
   ``clock_frequency`` has no alias ``Fmax`` on purpose. It is the frequency that was constrained,
   and not the maximum that was achieved.

.. note::
   Every tool times a design with its own model, so compare ``Fmax`` only between runs of the same
   tool. ``nextpnr`` and Vivado, which place and route with different models, show it. For one
   Artix-7 design with a block RAM and a multiply-accumulate, ``nextpnr`` reported 453 MHz for the
   netlist from Yosys and 429 MHz for the netlist from Vivado. Vivado placed and routed the same two
   netlists and reported 241 MHz and 232 MHz. On a counter that drives LEDs, Vivado timed the
   placement and routing of ``nextpnr``, and its result differed from the report of ``nextpnr`` by
   0.14 ns.

Simulation results
==================

A simulation passes only on evidence that it ended, and not on the exit status of the simulator
alone. A cocotb run, on any simulator, needs at least one test that ran and none that failed. A run
in which every test was skipped fails. Each simulator that does not use cocotb must report a
recognized end:

* ``$finish`` (including VHDL ``std.env.finish`` and ``std.env.stop``),
* a requested and confirmed ``stop_time``,
* a measured requested ``max_cycles``, or
* the observed exit status 0 of a C++ driver that the user owns, where Xeda supports that driver
  contract.

A run fails when its record is missing or malformed, when its end is unknown, on a silent exit 0,
on an unrequested stop, and when the event queue drains without a finish. A nonzero exit status of
the tool or the driver always fails, even after a finish. Xeda saves the normalized record as
``sim.evidence`` next to ``sim.ended_by``, ``sim.time``, ``sim.time_unit``, ``sim.errors`` and
``sim.warnings``.

When a simulation fails these checks, ``error.message`` in ``results.json`` says what happened and
what to do. For a testbench that stops with no finish, it tells you to end the testbench with
``std.env.finish`` (VHDL) or ``$finish`` (Verilog), or to set ``stop_time``.

Every simulator flow (every ``SimFlow``) has the settings ``timeout`` and ``fail_severity``. The
default severity is ``error``, and the choices are ``warning``, ``error``, ``failure`` and
``fatal`` (``failure`` and ``fatal`` have the same rank). Any observed event at or above the
selected threshold fails the run. ``timeout`` is a number of seconds of wall-clock time. It stops
and fails each subprocess invocation that contains simulation. An invocation can combine analysis,
elaboration and simulation. The limit applies to each invocation and not to their total. An unset
``timeout`` adds no limit. A Verilog ``$stop`` is an event of the rank ``error`` and fails at the
default threshold. VHDL ``std.env.stop`` is accepted as completion.

GHDL and nvc keep the actual end time of the simulator. A stop whose observed time differs from the
requested ``stop_time`` fails, which matters for designs with sparse clock events. Xeda does not
rewrite the reported time to the request. NVC uses a passive VHPI helper for the end time, so a C++
compiler is needed when that helper must be built. NVC has no ``exit_severity`` setting: use
``fail_severity``. A Bluesim ``max_cycles`` passes only when the recorded cycle count
equals the request and the record includes a measured final time and unit. CXXRTL links an exit
monitor and an RTL assertion hook into a C++ driver that the user owns. An observed driver exit 0
is valid evidence even when the simulated time is unknown, but missing records or abnormal exits
fail. CXXRTL rejects ``stop_time`` because the driver controls scheduling.

ModelSim batch runs require matching native observations of the end reason, the time and TESTSTATUS
from the checkpoint of this run and from a bounded runtime logfile. Compilation and loading
messages do not affect the runtime verdict. VHDL ``std.env.stop`` is accepted with native VHDL
break evidence, and Verilog ``$stop`` fails. A VHDL failure and a SystemVerilog fatal share the
native fatal rank. This contract was verified with ModelSim-Intel Starter 2020.1. Vivado 2024.2
verified the xsim behavior for completion, severity, a source-qualified VHDL stop and an absolute
``stop_time``, across prerun and runtime. The requested bound is measured against the actual
current time, and Xeda skips further execution after a finish or after the bound. The
``elab_debug`` setting must preserve source information. Disabling it explicitly is rejected,
because the evidence of a VHDL stop would otherwise be ambiguous.

VCS and Questa adapters are documentation-only and have not been verified against licensed real
tools. They fail closed when native evidence is not recognizable. VCS quiet ``$finish(0)`` and VHDL
completion fail without a native finish diagnostic, and a UCLI time checkpoint alone does not prove
HDL completion. Questa requires the ModelSim-style owned runtime transcript and checkpoint. An
unrecognized native stop reason or missing source-qualified VHDL evidence fails. Do not treat their
coverage with synthetic tools as certification on real tools.

The accepted ``bsc_sim`` backends are Bluesim, Verilator, Icarus, ModelSim, Questa, VCS, vcsi and
xsim. The legacy engines ``cvc``, ``cver``, ``isim``, ``ncverilog`` and ``veriwell`` are rejected
before compilation, because they lack a certified evidence adapter. The real runtime evidence and
the builtin-task capability checks of Icarus remain mandatory gates of the Linux CI. They were not
run on macOS.

SDF annotation
--------------

``ghdl_sim``, ``modelsim``, ``vivado_sim`` and ``vivado_postsynth_sim`` annotate a netlist with
the SDF files of their ``sdf`` setting. The annotation needs the instance of the unit under test in
the testbench: ``sdf.root``, or else ``tb.uut``. These flows refuse an SDF file when neither is
set, naming both, before anything runs.

Waveforms
---------

Every simulator writes a waveform through its own mechanism: a run option, a script command or the
``$dumpfile`` of the HDL. Xeda never writes one itself. A simulator that cannot honor a waveform
setting refuses it when the run is planned, so no waveform setting is ignored.

All waveform settings read their value by one rule, on the command line and in a design file:

* ``true`` writes ``dump.<format>`` (``dump.vcd`` for ``vcd``, ``dump.vcd.gz`` for ``vcdgz``). This
  is also the name that the run writes when you give a location, which Xeda then delivers, so a
  location and ``true`` are the same run. For the ``saif`` of ``vivado_sim`` and
  ``vivado_postsynth_sim``, that name is ``outputs/<design>.saif``. ``verilator`` without cocotb
  accepts only ``true`` and no location, because the testbench names the file. On the command line,
  ``-s vcd=true`` and a YAML ``vcd: true`` are the same setting.
* ``false`` and an empty name write nothing.
* A name without an extension gets the extension of the format.
* The words that YAML 1.1 reads as a boolean (``yes``, ``no``, ``on``, ``off``, ``y``, ``n``,
  ``t`` and ``f``) are refused, as they are for a boolean setting. So is ``true`` or ``false`` with
  spaces around it. Write ``true`` or a file name.
* Other text is a name. The setting that receives a name decides the format, and the name never
  does: Xeda refuses a name that ends in an extension of another format. A format can have several
  extensions (``.vcd.gz``, ``.vcdgz`` and ``.gz`` are compressed VCD). When the simulator has a
  setting for the other format, the message names it. For example, ``wave: x.vcd`` on ``ghdl_sim``
  says to use ``vcd``. Otherwise the message says which extension to use. A gzip name given to
  ``vcd`` on a simulator that writes plain VCD says to drop the ``.gz``.

A waveform setting is named after its format, so the same name means the same thing on every
simulator. The one exception is ``wave`` of ``ghdl_sim``, which writes GHW, the waveform format of
GHDL itself.

The shared ``vcd`` setting writes a VCD file. No simulator has a ``waveform`` setting; the error
says to use ``vcd``.

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Simulator
     - Waveform settings
   * - ``ghdl_sim``
     - ``vcd`` (plain VCD, ``--vcd``), ``vcdgz`` (gzip-compressed VCD, ``--vcdgz``), ``fst``
       (``--fst``) and ``wave`` (GHW, ``--wave``). Only one of the four may be set, and
       ``read_wave_opt`` needs one of them. The name never selects the option: ``vcd`` always
       writes plain VCD.
   * - ``nvc``
     - ``vcd`` (``--wave=<file> --format=vcd``) and ``fst`` (``--wave=<file>``). Only one of
       the two may be set. ``wave_include_glob``, ``wave_exclude_glob`` and ``wave_arrays`` apply
       to the waveform, and need one of the two unless they are set to their default values.
       ``wave`` and ``wave_format`` are errors: use ``fst``, or ``vcd`` for a VCD file.
   * - ``modelsim``
     - ``vcd``. ModelSim dumps nothing unless it is set.
   * - ``vivado_sim``, ``vivado_postsynth_sim``
     - ``vcd`` (with ``vcd_scope`` and ``vcd_level``) and ``saif``. ``saif`` records the activity of
       the instance ``tb.uut``, so the design must set ``tb.uut``. Planning fails without it. That
       includes ``vivado_postsynth_sim`` with ``timing_sim`` and every ``vivado_power`` request,
       which switch the activity on.
       ``prerun_time`` runs the simulation before the SAIF file is opened, so the activity and
       the duration of the SAIF file exclude that time. The VCD records from time 0.
   * - ``bsc_sim``
     - ``vcd``, on every backend. With the Verilator backend, a run whose driver wrote no
       ``dump.vcd`` fails.
   * - ``verilator``
     - ``vcd``, ``fst`` and ``saif``. See the Verilator section below.
   * - ``vcs``
     - ``vpd``, ``fsdb`` and ``evcd``, only one at a time. ``vcd`` is refused: set ``vpd`` or
       ``fsdb`` with ``to_vcd`` to get a VCD file. ``vpd_size_limit`` needs ``vpd``, and
       ``fsdb_size_limit`` needs ``fsdb``.
   * - ``yosys_sim``
     - ``vcd`` is refused. The C++ driver of the design writes the waveform with
       ``cxxrtl::vcd_writer``.

Verilator
---------

The ``--main`` option of Verilator runs an HDL top, and hooks of Xeda in the Verilator runtime
record how the run ended. They set the ``sim.ended_by`` and ``sim.time`` result keys, among others.
The run passes when the testbench ends it with ``$finish`` and nothing reported reaches
``fail_severity`` (``warning``, ``error``, ``failure`` or ``fatal``; default ``error``). An event
queue that runs empty without a ``$finish`` fails (``sim.ended_by`` is ``drained``, and
``sim.time`` is empty), and ``timeout`` stops a run that does not end. Xeda also copies the output
of the model to ``sim.log`` in ``sim_dir``, except under cocotb, whose output goes straight to the
terminal.

Three drivers can run the model:

* Verilator's own main, by default. The ``main`` setting maps to ``--main``: it is true unless the
  testbench uses cocotb.
* The ``main`` or ``sc_main`` of the design itself, with ``main: false``. The hooks still record
  how it ended. The run passes when the main exits with status 0 and nothing reported reaches
  ``fail_severity``. A C++ source with ``main`` unset is a helper (for DPI, for example) and is
  linked into the model.
* The main of cocotb, when the testbench uses cocotb. The results of cocotb decide the run.
  ``main: true`` is an error with cocotb.

A report that ends the run is one event, and ``sim.ended_by`` names its cause: ``error`` for
``$error``, a failed assertion and ``$stop``, and ``fatal`` for ``$fatal`` and for the fatal errors
of Verilator itself.

The simulated top (``--top-module``) is ``tb.top`` of the testbench, or else ``rtl.top``. With
cocotb, it is the module that cocotb drives: ``tb.cocotb.toplevel``, or else ``rtl.top``. A design
with a Verilog or SystemVerilog testbench needs ``tb.top``. Without it, the flow refuses the
design, since it would simulate ``rtl.top``, which has no stimulus. With ``main: false``, the main
in C++ of the design runs the model and the flow needs no ``tb.top``, whatever HDL the testbench
also holds (a bound checker, for example). ``rtl.parameters`` apply only when the RTL top is the
simulated top, and ``tb.parameters`` always apply.

Waveforms follow the rules of Verilator itself. ``vcd``, ``fst`` and ``saif`` build the model with
that trace format (``--trace-vcd``, ``--trace-fst``, ``--trace-saif``), and only one may be set.
Without cocotb, Xeda writes no waveform. The testbench (or the main of the design) names and writes
the file with ``$dumpfile`` and ``$dumpvars``, in the run directory, which is the working directory
of the model. Set the format to ``true``, which is stored as ``dump.<format>`` (``dump.vcd`` for
``vcd``). That name is the only one accepted, and the testbench must call
``$dumpfile("dump.<format>")``. A run that wrote no ``dump.<format>`` fails and says so. Otherwise
it would pass with the file missing, or with data of the built format under another name. Every
other name and every location is an error before Verilator runs, because the testbench writes the
file and Xeda does not copy it. With cocotb, Xeda names the file (``--trace-file`` of cocotb):
``true`` writes ``dump.<format>`` (``dump.vcd`` for ``vcd``), and Xeda delivers a location. A
testbench that also calls ``$dumpvars`` under cocotb makes the file of cocotb nearly empty, because
the trace code of Verilator gives all changes to the first writer. The trace options
(``trace_underscore``, ``trace_structs``, ``trace_max_width`` and ``trace_max_array``) keep the
defaults of Verilator unless set.

Defaults and limits:

* ``optimize`` is ``true`` (``-O3``), ``false`` (no ``-O`` option, so Verilator uses its own
  default) or a level of ``0``, ``1``, ``2`` or ``3``. Write the level only: ``2``, not ``-O2``.
  Planning refuses any other value, an empty one included.
* ``random_init`` defaults to false, as in Verilator, and ``random_seed`` is used only with it.
  ``x_initial`` and ``x_assign`` default to ``"0"``.
* ``timing`` is on by default. It enables the ``--timing`` support of Verilator for delays and
  event controls, which needs a compiler with C++20 coroutine support. The C++ standard that
  Verilator requires takes precedence over a ``-std=`` flag in ``cflags``. Set ``timing: false`` to
  disable it. Verilator then ignores delays and warns with ``STMTDLY`` for a delay on a statement
  (``#10;``) and ``ASSIGNDLY`` for a delay on an assignment. Such a warning does not fail the run,
  unless ``warnings_fatal`` is set. A delay that Verilator ignores leaves no event to wait for, so a
  run without ``$finish`` drains at once and fails. It does not wait for ``timeout``.
* ``stop_time`` is an error in every mode, before anything runs. End the testbench with
  ``$finish``, or limit the run with ``timeout``.
* ``generate_systemc`` needs ``main: false`` and an ``sc_main`` of the design among its C++
  sources, because the ``--main`` option of Verilator generates a C++ main.
* Xeda refuses a run directory whose path has whitespace, before anything is created and when
  planning, because the GNU Make build of Verilator cannot work in such a directory. It refuses a
  ``sim_dir`` with whitespace for the same reason, since the model is built there. Use a run root
  and a ``sim_dir`` without whitespace (``--run-root``).
* Verilator 5.048 or newer is required. A ``$finish`` in a ``final`` block does not end a run that
  drained: the run fails, as it does without the block. Because the model runs under the main of
  Verilator, ``--coverage`` in ``compile_args`` writes ``coverage.dat``, and VPI start-up routines
  run with ``vpi: true``. ``bsc_sim`` with Verilator needs 5.044 or newer.
* Before the model runs, Xeda removes a file or link that is left at the name of the coverage file
  in the run directory (``coverage.dat``, or the name that ``+verilator+coverage+file+`` gives), so
  the model never writes through a link. The flow refuses a coverage name that is a directory, that
  has ``..`` in it, or that goes through a link leading out of the run directory. A full path
  outside the run directory is left as it is. In a directory that no launcher chose (a flow built
  directly), the flow deletes nothing and refuses a link at that name.

Writing a new flow
==================

A new simulator flow must inherit ``SimFlow`` and translate the records of the current run of that
simulator into ``SimEvidence`` for the shared ``judge_evidence`` verdict. Add its accepted backend
to the behavioral oracle, and test positive completion, silent and drained failure, severity,
requested limits, timeout, and stale or malformed evidence. Never add an exemption for an exit
status alone.

Concrete flows live in ``src/xeda/flows/<tool>/``. A flow subclasses one of ``Flow``, ``SimFlow``,
``SynthFlow``, ``FpgaSynthFlow`` or ``AsicSynthFlow`` and implements:

``init()`` (optional)
    Runs after construction, once the settings and the design are known. It is separate from
    ``__init__``, so a flow can prepare itself from its effective settings. It adds no producer and
    reads no input: a flow declares the inputs it reads (``Flow.Inputs``), and the launcher runs
    their producers before ``run()``.

``run()``
    Generates scripts (Jinja2 templates in a ``templates/`` directory next to the flow module) and
    invokes the tools.

``parse_reports()`` (optional)
    Fills ``self.results``. Helpers: ``parse_report_regex()``, ``parse_regex()`` and
    ``parse_xml()``.

A flow refuses what its settings and the design alone rule out in two class methods that planning,
``--remote`` and ``xeda dse`` call: ``check_settings_supported(cls, settings)`` and
``check_design_supported(cls, design, settings=None)``. Neither starts a tool or reads a file that
the design does not list. An override calls ``super()`` first. A refusal that needs the run itself
(a tool on ``PATH``, an installed database, the inputs that a producer handed over) stays in
``init()`` or ``prepare_inputs()``. A flow that is built directly, without a launcher, is checked
only if its own code calls the two methods, as ``ghdl_sim`` does in ``init()``. Call them at the
start of the ``init()`` of a new flow.

A simulator that needs no ``tb.top`` for some designs overrides
``runs_without_testbench_top(cls, design, settings=None)``. Both methods take the settings of the
flow as an optional second argument, which every launch passes. An override with the signature
that takes the design alone fails with a ``TypeError`` when the launch is planned: add the
argument.

Registration is automatic. ``Flow.__init_subclass__`` derives the flow name from the class name
(``VivadoSynth`` -> ``vivado_synth``) and registers it, with any extra names listed in
``aliases``. Add the class to both the import list and ``__all__`` in
``src/xeda/flows/__init__.py``, so that Xeda ships it and users can discover it. Then run
``python tools/generate_flow_catalog.py`` and commit the updated ``src/xeda/flow_catalog.json``.
Shell completion reads that file. Run the same command again after you change the aliases, the
inputs, the outputs, the settings models, or the ``action_reason`` of a flow. The catalog includes
nested setting paths, accepted aliases, and finite value choices derived from the settings
models. Do not add setting names to the catalog by hand.

The test suite enforces two things that a new flow owes its users:

* a docstring of its own, which ``xeda list-flows`` shows
* a ``description=`` on every settings field, and a ``results_description`` that describes the keys
  the flow reports (or ``results_description = {}`` if it reports none)
