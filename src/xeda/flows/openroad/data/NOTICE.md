# Notices for third-party material

This directory holds one file of OpenROAD-flow-scripts. The flow `openroad` runs it in KLayout to
merge the routed design with the GDSII files of the cells into one layout.

## OpenROAD-flow-scripts

`def2stream.py` comes from the directory `flow/util` of OpenROAD-flow-scripts at commit
`6b305a712966be72d8908695c04398acdbe273ca`. Its first line names the exact URL of the file.

Xeda changed the script in these ways. It takes the regular expression of the cells that may have
no GDSII cell (`allow_empty`) from a name that KLayout defines on its command line, as it takes the
other inputs, and not from an environment variable. It reads the GDSII files from a list with one
path on each line, so that a path can hold a space. It leaves out the support of the memory
generator of that flow, which Xeda does not have. It adds three kinds of input, all names that KLayout
defines on its command line: the policy for a cell that is in the layout already
(`conflict_resolution`), the options of the LEF and DEF reader that remove the properties of the
shapes (`net_properties`, `instance_properties`, `pin_properties`) or change the reading of the
LEF files (`read_lef_with_def`), and the switch for the time stamps of the output file
(`write_timestamps`). The code formatter of Xeda also changed the line breaks.

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
