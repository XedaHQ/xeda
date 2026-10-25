********
Tutorial
********

This tutorial takes a small VHDL design through simulation, FPGA synthesis and a frequency
search. The design is the ``sqrt`` example, in ``examples/vhdl/sqrt`` of the Xeda repository. It
is an iterative integer square root with one clock, a parameterized input width and a cocotb
testbench. Run the commands from the root directory of the repository.

1. Write the design file
========================

``sqrt.yaml`` describes the design and nothing about how to build it, apart from the sections of
the flows at the end:

.. literalinclude:: ../../examples/vhdl/sqrt/sqrt.yaml
   :language: yaml

Notes on the file:

* ``clock.port`` names the clock port of the design. The period of the clock is a setting of each
  flow, because it constrains one build. A period has a unit, such as ``5 ns``. A number without a
  unit is an error (see :ref:`quantities`).
* ``sources`` is in compilation order. Paths are relative to the directory of the design file.
* A dotted key such as ``clock.port`` stands for a nested table: ``clock`` with the key ``port``.
  The command line uses the same keys.
* A ``.py`` testbench source makes the testbench a cocotb one. No other key is needed.
* Each flow has its own section under ``flows``. The device is a setting of each flow, so the
  sections repeat ``fpga.part``.

2. Simulate
===========

.. code-block:: bash

    xeda run ghdl_sim examples/vhdl/sqrt/sqrt.yaml

Xeda analyzes the VHDL with GHDL, elaborates it and runs the cocotb regression. The results table
shows the cocotb test counts. The exit status is non-zero if any test failed.

To write a waveform:

.. code-block:: bash

    xeda run ghdl_sim examples/vhdl/sqrt/sqrt.yaml -s vcd=dump.vcd

A simulation passes only if it ends on purpose. The testbench calls ``$finish`` (``std.env.finish``
in VHDL), the cocotb tests complete, or the run reaches the ``stop_time`` that you set. Use
``stop_time`` for a testbench that never finishes by itself, such as one with a free-running clock.
Do not use it with this design. cocotb ends the simulation itself, and a ``stop_time`` earlier than
the end of the tests makes cocotb report that the simulation ended too early.

Another simulator runs the same design file and testbench:

.. code-block:: bash

    xeda run nvc examples/vhdl/sqrt/sqrt.yaml

Verilator simulates Verilog and SystemVerilog. This command runs the ``fifo`` example, a
SystemVerilog design with its own testbench:

.. code-block:: bash

    xeda run verilator examples/sv/fifo/fifo.xeda.yaml

3. Synthesize
=============

.. code-block:: bash

    xeda run vivado_synth examples/vhdl/sqrt/sqrt.yaml

The ``flows.vivado_synth`` section supplies the part and the clock period. To tighten the period,
use the command line and leave the file as it is:

.. code-block:: bash

    xeda run vivado_synth examples/vhdl/sqrt/sqrt.yaml -s clock.period=4ns

To see all the settings of the flow:

.. code-block:: bash

    xeda list-settings vivado_synth

To see what a command would run, add ``--dry-run``. Xeda prints the plan and starts no tool:

.. code-block:: bash

    xeda run vivado_synth examples/vhdl/sqrt/sqrt.yaml --dry-run

An open-source toolchain uses ``yosys_fpga`` to synthesize and ``nextpnr`` to place and route.
``nextpnr`` needs the netlist of ``yosys_fpga``, so running ``nextpnr`` runs both:

.. code-block:: bash

    xeda run nextpnr examples/vhdl/sqrt/sqrt.yaml -s fpga.part=LFE5U-85F-6BG381C clock.period=10ns

4. Read the results
===================

Each run leaves a directory under ``./xeda_run/``. It holds the generated scripts, the tool logs,
``reports/`` and ``outputs/``. It also holds three JSON files: ``settings.json`` (the input
settings and the effective settings), ``results.json`` (what Xeda parsed from the reports) and
``trace.json`` (what Xeda needs to decide later whether the run is up to date).

A script can read the results from the ``--json`` document that the command writes to stdout:

.. code-block:: bash

    xeda run vivado_synth examples/vhdl/sqrt/sqrt.yaml --json | jq '{fmax: .results.fmax, luts: .results.lut}'

``xeda list-results vivado_synth`` lists every metric with its type and unit.

5. Search for the maximum frequency
===================================

To find the clock period, let Xeda search for it. ``xeda dse`` runs many instances of a flow in
parallel under an optimizer. The default optimizer searches for the highest frequency that still
meets timing:

.. code-block:: bash

    xeda dse vivado_synth --design examples/vhdl/sqrt/sqrt.yaml --max-workers 4

Each time the search improves, Xeda writes the best result to a JSON file in the run root,
``xeda_run/fmax_<design>_<flow>_<timestamp>.json``. A search that stops early keeps its best
result. ``xeda list-optimizers`` shows the optimizers and their settings.

Where to go next
================

* :doc:`../design-file`: the reference of the design file
* :doc:`../flows`: how flows, settings and producers fit together
* :doc:`../run-directories`: the run directory and ``results.json``
* :doc:`../machine-readable`: JSON output, for scripts and coding agents
