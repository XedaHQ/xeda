Quickstart
==========

To get started with xeda, first create a ``design.json`` file for your design.
This file contains metadata, list of source files, and flow settings regarding
your design. Below is an example ``design.json`` which can be adapted to any design. 

.. Checkout the :ref: design.json page for the full list of settings availble. 
.. TODO add design.json breakdown

.. code-block:: json

    {
        "design": {
            "name": "mydesign",
            "description": "This really cool design",
            "author":["Dr. Seuss", "[Xena](https://en.wikipedia.org/wiki/Xena)", "[Kamyar Mohajerani](mailto:kammoh@gmail.com)"],
            "url": "https://github.com/KeccakTeam/Xoodoo.git",
            "sources": [
                "src_rtl/v1/s1.vhd",
                "src_rtl/v1/s2.vhd",
                {
                    "file": "src_tb/tb_s.vhd",
                    "sim_only": true
                }
            ],
            "vhdl_std": "02",
            "vhdl_synopsys": true,
            "top": "TOP",
            "clock_port": "clk",
            "tb_top": "TB_TOP",
            "tb_generics": {
                "STRING_GENERIC": "hello",
                "FILENAME_STRING_GENERIC": {
                    "file": "../data.txt"
                },
                "INT_GENERIC": 123
            },
            "generics": {}
        },
        "flows": {
            "diamond": {
                "fpga_part": "LFE5U-25F-6BG381C",
                "clock_period": 11.061946902654867,
                "synthesis_engine": "synplify",
                "strategy": "Timing"
            },
            "vivado": {
                "fpga_part": "xc7a12tcsg325-3",
                "clock_period": 4.369,
                "strategy": "Timing",
                "optimize_power": "False",
                "sim_run": "all"
            }
        }
    }

After the ``design.json`` file has been created for your design, you can now use xeda to generate simulation, synthesis and implementation results for your design using the supported tool of your choice.

For example, if we want to simulate the above design with GHDL, we would run

.. code-block:: bash

    $ xeda run ghdl_sim

If our ``design.json`` file is renamed or not in the directory that xeda is called in, we can explictly specify the path of the file with the ``--design-json`` argument

.. code-block:: bash

    $ xeda run --design-json xeda_configs/design_1.json ghdl_sim

If we are satisfied with the results of the simulation, we can have xeda synthesis and implement our design. For example, with Xilinx Vivado:

.. code-block:: bash

    $ xeda run vivado_synth

That's it! That's all xeda requires to simulate, synthesis, and implement an HDL design.

As always, you can run ``xeda --help`` or ``xeda run --help`` for the full list of arguments