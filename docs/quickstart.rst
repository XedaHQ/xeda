Quickstart
==========

.. OUTDATED FIXME

To get started with xeda, first create a ``xedaproject.toml`` file for your design.
This file contains metadata, list of source files, and flow settings regarding
your design. Below is an example ``xedaproject.toml`` which can be adapted to any design. 

Checkout the xedaproject_ section for the detailed structure of the file. 

.. TODO add xedaproject.toml breakdown


.. code-block:: toml

    [project]
    name = "Project1"
    description = "My Project with 2 designs"

    [[design]]
    name = 'Design1'
    [design.rtl]
    sources = [
        'src_rtl/module1.vhd',
        'src_rtl/top.v'
    ]
    top = 'Top'
    clock = 'clk'
    [design.tb]
    sources = [
        'top_tb.vhd',
    ]
    top = 'TopTB'



After the ``xedaproject.toml`` file has been created for your design, you can now use xeda to generate simulation, synthesis and implementation results for your design using the supported tool of your choice.

For example, if we want to simulate the above design with GHDL, we would run

.. code-block:: bash

    $ xeda ghdl_sim

If we are satisfied with the results of the simulation, we can have xeda synthesis and implement our design. For example, with Xilinx Vivado:

.. code-block:: bash

    $ xeda vivado_synth

If multiple ``[[design]]`` entries are present, the active design needs be specified using ``--design`` flag

That's it! That's all xeda requires to simulate, synthesis, and implement an HDL design.

As always, you can run ``xeda --help`` for the full list of arguments.


xedaproject
-----------
Settings:

- ``project``
- ``design`` can be a list of multiple TOML tables, must use ``[[design]]`` instead of ``[design]``
    - ``name``: a string that designates a name for this design. Recommended to avoid any whitespace.
    - ``rtl``
        - ``sources``: a list of HDL files used for synthesis and simulation
        - ``top``: syntehsis top entity/module
    - ``tb``
        - ``sources``: a list of testbench files used for simulation only
        - ``top``: simulation top entity/module/function
- ``flows``

The sub-tables may include optional entries referenced only by a particular plugin.
E.g., ``design.lwc`` is used by ``lwc`` plugins.


bash-completion
---------------

