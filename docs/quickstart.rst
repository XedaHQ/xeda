**********
Quickstart
**********

Install
=======

Python 3.11 or newer is required.

For command-line use, the preferred installation is an isolated ``uv`` tool environment::

    uv tool install --upgrade xeda

`pipx <https://pipx.pypa.io/stable/>`_ is an equivalent alternative::

    pipx install --force xeda

To use Xeda as a library as well, install it into a virtual environment::

    python3 -m pip install -U xeda

Check the installation::

    xeda --version

Describe your design
====================

A design description is a single TOML, YAML or JSON file. It says what the design is, not how to
build it - tool choices stay out of it, except for the optional per-flow settings at the end.

``sqrt.toml``:

.. code-block:: toml

    name = "sqrt"
    description = "Iterative computation of square-root of an integer"
    language.vhdl.standard = "2008"

    [rtl]
    sources = ["sqrt.vhdl"]
    top = "sqrt"
    clock = { port = "clk" }
    parameters = { G_IN_WIDTH = 32 }

    [tb]
    sources = ["tb_sqrt.py"]
    cocotb = true

    [flows.vivado_synth]
    fpga.part = "xc7a100tftg256-2L"
    clock.period = 5.0

Paths are resolved relative to the directory holding the design file, so the file is portable
along with its sources. See :doc:`design-file` for the full reference.

Run a flow
==========

``xeda run <FLOW_NAME> <DESIGN_FILE>``. To simulate the design with GHDL::

    xeda run ghdl_sim sqrt.toml

To synthesize it for an FPGA with Vivado::

    xeda run vivado_synth sqrt.toml

The ``[flows.vivado_synth]`` section of the design file supplies that flow's settings. Override
any of them on the command line with ``-s``/``--settings``, using dotted keys for nested values::

    xeda run vivado_synth sqrt.toml -s clock.period=4.5 impl.strategy=Performance_ExplorePostRoutePhysOpt

Unknown settings are rejected rather than ignored, so a typo fails loudly instead of quietly
doing nothing.

Find out what is available
==========================

Nothing here needs to be memorized; the CLI is self-describing.

.. code-block:: bash

    xeda list-flows                     # every flow, with its aliases, category and dependencies
    xeda list-settings vivado_synth     # every setting of a flow, with type, default and meaning
    xeda list-results vivado_synth      # the keys that flow writes to results.json
    xeda list-boards                    # bundled FPGA board definitions
    xeda list-platforms                 # bundled ASIC platforms (PDKs)
    xeda design-schema                  # JSON Schema of a design file

Add ``--json`` to any of them for machine-readable output. See :doc:`machine-readable`.

Where the output goes
=====================

Each run gets its own directory under ``./xeda_run/``, holding the generated scripts, the tool
logs, ``reports/``, ``outputs/``, and two JSON files: ``settings.json`` (the settings, as given and
as used) and ``results.json`` (what was parsed back out). See :doc:`run-directories`.

Explore the design space
========================

``xeda dse`` runs many instances of a flow in parallel under an optimizer. The default optimizer
searches for the maximum clock frequency::

    xeda dse vivado_synth --design sqrt.toml

``xeda list-optimizers`` shows the available optimizers and their settings.
