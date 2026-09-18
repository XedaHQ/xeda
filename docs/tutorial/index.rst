********
Tutorial
********

This walks a small VHDL design through simulation, FPGA synthesis and a frequency search, using
the ``sqrt`` example that ships with Xeda (``examples/vhdl/sqrt``).

The design is an iterative integer square root with a single clock, a parameterized input width,
and a cocotb testbench.

1. Write the design file
========================

``sqrt.toml`` describes the design and nothing about how to build it, apart from the per-flow
sections at the end:

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

Three things worth noticing:

* ``clock`` names the design's clock *port*. The clock's *period* is a flow setting, because it
  constrains a particular build rather than describing the design. ``clock_port`` remains accepted
  as a compatibility shorthand.
* ``sources`` is in compilation order, and paths resolve against the design file's directory.
* The ``.py`` testbench source is recognized as cocotb automatically; ``cocotb = true`` is
  redundant here but harmless.

2. Simulate
===========

.. code-block:: bash

    xeda run ghdl_sim sqrt.toml

Xeda analyzes the VHDL with GHDL, elaborates it, and runs the cocotb regression. The results table
reports the cocotb test counts, and the exit status is non-zero if any test failed.

To capture a waveform, and to stop early:

.. code-block:: bash

    xeda run ghdl_sim sqrt.toml -s waveform=dump.vcd stop_time=50us

Any simulator will do - the design file does not change:

.. code-block:: bash

    xeda run nvc sqrt.toml           # NVC
    xeda run verilator fifo.toml     # Verilator, for a Verilog/SystemVerilog design

3. Synthesize
=============

.. code-block:: bash

    xeda run vivado_synth sqrt.toml

The ``[flows.vivado_synth]`` section supplies the part and the clock period. Tighten the period
from the command line without editing the file:

.. code-block:: bash

    xeda run vivado_synth sqrt.toml -s clock.period=4.0

To find out what else that flow accepts:

.. code-block:: bash

    xeda list-settings vivado_synth

For an open-source toolchain instead, ``yosys_fpga`` synthesizes and ``nextpnr`` places and
routes. ``nextpnr`` depends on ``yosys_fpga``, so running it runs both:

.. code-block:: bash

    xeda run nextpnr sqrt.toml -s fpga.part=LFE5U-85F-6BG381C clock.period=10

4. Read the results
===================

Each run leaves a directory under ``./xeda_run/`` containing the generated scripts, the tool logs,
``reports/``, ``outputs/``, and two JSON files: ``settings.json`` (both the re-runnable input and
the final effective settings) and ``results.json`` (what was parsed back out).

From a script, skip the file-hunting:

.. code-block:: bash

    xeda run vivado_synth sqrt.toml --json | jq '{fmax: .results.Fmax, luts: .results.lut}'

``xeda list-results vivado_synth`` explains every key.

5. Search for the maximum frequency
===================================

Instead of guessing at the clock period, let Xeda search. ``xeda dse`` runs many instances of a
flow in parallel under an optimizer; the default one binary-searches for the highest frequency
that still meets timing:

.. code-block:: bash

    xeda dse vivado_synth --design sqrt.toml --max-workers 4

The best result is written to a JSON file in the working directory as the search improves, so a
long search is never lost. ``xeda list-optimizers`` shows the optimizers and their settings.

Where to go next
================

* :doc:`../design-file` - the full design-file reference
* :doc:`../flows` - how flows, settings and dependencies fit together
* :doc:`../run-directories` - the run directory and ``results.json``
* :doc:`../machine-readable` - JSON output, for scripts and coding agents
