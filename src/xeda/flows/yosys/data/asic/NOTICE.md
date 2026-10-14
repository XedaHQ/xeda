# Notices for the operator-wrapping assets

This directory holds two files of OpenROAD-flow-scripts. Xeda uses them in the yosys ASIC
synthesis flow to wrap arithmetic operators in modules with alternative architectures.

## OpenROAD-flow-scripts

`synth_wrap_operators.tcl` and `synth_wrap_operators-booth.v` come from the directory
`flow/scripts` of OpenROAD-flow-scripts at commit `6b305a712966be72d8908695c04398acdbe273ca`.
Their first line names the exact URL of each file.

Xeda changed only the Tcl script. It takes the Booth map path, the adder order and the multiplier
order as arguments, not from environment variables. Sourcing the script defines procedures only.
The Verilog file is unchanged except for its first line, which names the source.

Three more parts of OpenROAD-flow-scripts at the same commit are not files in this directory:

- The Tcl template `yosys_liberty_synth.tcl` carries the procedure `convert_liberty_areas` of
  `flow/scripts/synth_preamble.tcl`. Xeda left out the last part of the procedure, which sets the
  cost of the macros that the memory generator of that flow makes.
- The same template carries the procedure `check_memory_sizes`. It follows the block of
  `flow/scripts/synth.tcl` that shrinks the memories above a limit to one row, and the size test
  of `flow/scripts/mem_dump.py`. Xeda lists the memories in the log of yosys and writes no report
  file.
- The module `yosys.py` carries the text of `flow/scripts/abc_speed_gia_only.script`. The flow
  writes it to the run directory as the script of `abc_new`.

OpenROAD-flow-scripts publishes its build and run scripts under the BSD 3-Clause License. The
license text follows.

The OpenROAD-flow-scripts repository (build and run scripts) has a BSD 3-Clause License with its text below.
The flow relies on several tools, platforms and designs that each have their own licenses.

The license below applies ***only*** to the build and run scripts, not the OpenROAD tools.
See the README.md file LICENSE section for details on the location of each components' individual license.

License for Build and Run scripts in the OpenROAD-flow-scripts repository only :

BSD 3-Clause License

Copyright (c) 2018-2023, The Regents of the University of California
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

* Redistributions of source code must retain the above copyright notice, this
  list of conditions and the following disclaimer.

* Redistributions in binary form must reproduce the above copyright notice,
  this list of conditions and the following disclaimer in the documentation
  and/or other materials provided with the distribution.

* Neither the name of the copyright holder nor the names of its
  contributors may be used to endorse or promote products derived from
  this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
