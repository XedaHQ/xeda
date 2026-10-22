**********
Quickstart
**********

Install
=======

Xeda supports every Python release series that has not reached end of life. Currently, that means
Python 3.11 through 3.15.

For command-line use, install Xeda in an isolated ``uv`` tool environment::

    uv tool install --upgrade xeda

`pipx <https://pipx.pypa.io/stable/>`_ does the same::

    pipx install --force xeda

To use Xeda as a library as well, install it in a virtual environment::

    python3 -m pip install -U xeda

Check the installation::

    xeda --version

Shell completion
================

Xeda supports completion for Bash 4.4 or newer, zsh and fish. Generate a completion script once, so
that a new shell can load it without starting Xeda.

For Bash::

    xeda completion bash --stdout > ~/.xeda-complete.bash

Add ``. ~/.xeda-complete.bash`` to ``~/.bashrc``.

For zsh::

    xeda completion zsh --stdout > ~/.xeda-complete.zsh

Add ``. ~/.xeda-complete.zsh`` to ``~/.zshrc`` after initializing completion with ``compinit``.

For fish::

    mkdir -p ~/.config/fish/completions
    xeda completion fish --stdout > ~/.config/fish/completions/xeda.fish

Completion of command names, options, built-in flow chains, and settings reads a generated catalog.
It does not load the flow implementations or read the design file. Generate the script again
after you upgrade Xeda, then load the new script in your shell.

After selecting a flow, press Tab after ``-s``, ``--settings``, or ``--flow-settings`` to complete
a setting. Nested names use dots::

    xeda run yosys_fpga+nextpnr+fpga_pack design.yaml -s fpga.p<Tab>

This offers ``fpga.package=``, ``fpga.part=``, and ``fpga.pins=``. A leaf ends in ``=`` so that
you can type its value immediately. A group ends in ``.`` so that you can complete its fields.
Some settings support both forms: ``fpga=`` takes a part-number string, while ``fpga.`` leads to
the device fields. The shell keeps the cursor after ``=`` or ``.`` without adding a space.
Boolean and enum values are suggested when their choices are declared by the settings model.

An unqualified setting belongs to the last flow in a chain. To select another flow, complete
``flows.<flow>.`` first::

    xeda run yosys_fpga+nextpnr+fpga_pack design.yaml -s flows.yosys_fpga.fl<Tab>

Flow suggestions include the chain and its potential default producers. The design can change
which producers the run needs; normal settings validation checks the selected flow when you run
the command.

You can complete several settings after one ``-s`` when the design path is already supplied,
or repeat ``-s`` for each setting. If the design path is omitted, a word after a complete
assignment can also be a design path. Completion preserves file suggestions in that position;
use another ``-s`` to request a setting explicitly. For mappings with user-defined keys, such as
``clocks.<name>.``, type the key before completing its declared fields. Arbitrary tool property
names are not listed.

The design file
===============

A design file says what the design is: its sources, top module, clock port and, optionally, a
testbench. It names no tool, apart from the optional settings of each flow at the end. It is a
YAML, TOML or JSON file, and YAML is recommended. This is ``examples/vhdl/sqrt/sqrt.yaml``:

.. literalinclude:: ../examples/vhdl/sqrt/sqrt.yaml
   :language: yaml

Paths are relative to the directory of the design file, so the file moves together with its
sources. A dotted key such as ``clock.period`` stands for a nested table. The command line takes
the same keys. See :doc:`design-file` for the full reference.

Run a flow
==========

``xeda run <FLOW_NAME> <DESIGN_FILE>``. To simulate the design with GHDL::

    xeda run ghdl_sim examples/vhdl/sqrt/sqrt.yaml

To synthesize it for an FPGA with Vivado::

    xeda run vivado_synth examples/vhdl/sqrt/sqrt.yaml

The ``flows.vivado_synth`` section of the design file holds the settings of that flow. Override
any of them on the command line with ``-s``/``--settings``. Nested settings use dotted keys::

    xeda run vivado_synth examples/vhdl/sqrt/sqrt.yaml -s clock.period=4.5ns impl.strategy=Performance_ExplorePostRoutePhysOpt

Xeda rejects an unknown setting, so a typo is an error. A clock setting needs a unit: ``4.5ns`` is
a time, and ``4.5`` is an error (see :ref:`quantities`).

A flow that declares its file inputs and outputs can follow another flow. Join the flows with
``+`` to make a chain. You ask for the last flow, and each flow before it feeds the next one.
A link can pass through the flows between its two flows: ``yosys_fpga+fpga_pack`` runs ``nextpnr``
in between. ``xeda list-flows`` shows which flows can follow which, and tab completion offers only
those. This chain ends at the bitstream. It builds the ULX3S example from
``examples/boards/ulx3s/blinky``, named by its path from the root directory of the repository::

    xeda run yosys_fpga+nextpnr+fpga_pack examples/boards/ulx3s/blinky/blinky.xeda.yaml --dry-run

``--dry-run`` shows the plan and runs nothing. The chain above programs no hardware. See
:ref:`flow-chains`.

Find out what is available
==========================

The command line describes itself:

.. code-block:: bash

    xeda list-flows                     # every flow: category, what it takes and makes, and what
                                        # can follow it in a chain
    xeda list-settings vivado_synth     # every setting of a flow, with type, default and meaning
    xeda list-results vivado_synth      # the metrics that flow writes, with types and units
    xeda list-boards                    # bundled FPGA board definitions
    xeda list-platforms                 # bundled ASIC platforms (PDKs) and whether they are installed
    xeda pdk list                       # the PDK files of each platform, by provider and version
    xeda pdk install <platform>         # download the PDK files that a platform pins
    xeda design-schema                  # JSON Schema of a design file

Add ``--json`` to any of them for machine-readable output. See :doc:`machine-readable`.

Where the output goes
=====================

Each run has its own directory under ``./xeda_run/``, the run root. The run directory holds the
generated scripts, the tool logs, ``reports/`` and ``outputs/``. It also holds three JSON files:
``settings.json`` (the settings as given and as used), ``results.json`` (what Xeda parsed from the
reports) and ``trace.json`` (what Xeda needs to decide later whether the run is up to date). See
:doc:`run-directories`.

Explore the design space
========================

``xeda dse`` runs many instances of a flow in parallel under an optimizer. The default optimizer
searches for the highest clock frequency::

    xeda dse vivado_synth --design examples/vhdl/sqrt/sqrt.yaml

``xeda list-optimizers`` shows the available optimizers and their settings.
