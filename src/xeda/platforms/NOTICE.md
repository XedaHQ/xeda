# Notices for the platform files

This directory holds one residue file per platform (`<platform>/platform.yaml`). A residue file
holds file names and numbers that xeda needs about a platform and that the PDK files do not give.

## Xeda ships no PDK library files

Xeda does not ship the library files of any PDK: no Liberty, LEF, GDS or SPICE file, and no file
of the Nangate Open Cell Library. A user installs those files from their own source, or names a
directory that holds them. The residue files only say where each file is in such a directory.

## OpenROAD-flow-scripts

`nangate45/platform.yaml` takes its file names and its values from the directory
`flow/platforms/nangate45` of OpenROAD-flow-scripts at the commit named in the file:

* the paths of the platform files, from `config.mk`;
* the layer adjustments of global routing, from `fastroute.tcl`;
* the resistance and capacitance of the wire layers, from `setRC.tcl`;
* the tap cell distance and the end cap cell, from `tapcell.tcl`;
* the power grid, from `grid_strategy-M1-M4-M7.tcl`;
* the track pitches, from `make_tracks.tcl`.

The Yosys techmap files (`*.v`) in a platform directory are copies of files of
OpenROAD-flow-scripts. OpenROAD-flow-scripts publishes its build and run scripts under the
BSD 3-Clause License. The license text follows. It applies to the build and run scripts of
OpenROAD-flow-scripts, which are the source of these values and files. It does not apply to
the tools or to the platform library files.

BSD 3-Clause License with its text below.
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

## open_pdks

Xeda reads the configuration files of an installed `open_pdks` build (for example the
`config.tcl` of a PDK that `ciel` installs) when a platform asks for them. No residue file holds a
value from `open_pdks` yet, and xeda copies no file of it. `open_pdks` is licensed under the
Apache License, Version 2.0 (https://github.com/RTimothyEdwards/open_pdks).
