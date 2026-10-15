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

Xeda ships a Yosys techmap file (`*.v`) in a platform directory only when the source of the
platform lacks it. Such a file is a copy from its source and keeps its license header.
`nangate45` has none: its techmap files stay in the PDK store.

`sky130hd/platform.yaml` takes one value from the directory `flow/platforms/sky130hd` of
OpenROAD-flow-scripts: the target density of global placement (`PLACE_DENSITY` of `config.mk`),
which `open_pdks` does not give.

OpenROAD-flow-scripts publishes its build and run scripts under the BSD 3-Clause License. The
license text follows. It applies to the build and run scripts of OpenROAD-flow-scripts, which are
the source of these values and file names. It does not apply to the tools or to the platform
library files.

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

`sky130hd/platform.yaml` takes its file names and its values from the build of `open_pdks` that
`ciel` installs, at the commit named in the file:

* the paths of the platform files, from the `libs.ref` and `libs.tech` directories of the build;
* the cell names, the layers, the tap cell distance and the power grid, which xeda reads at run
  time from `libs.tech/openlane/config.tcl` and `libs.tech/openlane/sky130_fd_sc_hd/config.tcl`
  and does not copy;
* the cells that no flow may use, from `libs.tech/openlane/sky130_fd_sc_hd/drc_exclude.cells`,
  which `DRC_EXCLUDE_CELL_LIST` names. Xeda reads the list at run time and does not copy it. The
  list `no_synth.cells` of the same build is for synthesis only, and xeda ignores it;
* the layer adjustment of global routing: `config.tcl` gives `GRT_LAYER_ADJUSTMENTS` as a list
  with one number for each routing layer of the technology. The residue maps the list to layer
  names with the order of the layers in the technology LEF (`li1`, `met1` to `met5`). It writes
  only the entry of `li1`, because a zero asks for no adjustment, which is the default;
* the power and ground nets and the global connections of the cell pins: the nets have the names
  of the power and ground pins (`VDD_PIN`, `GND_PIN`) and the pins are `STD_CELL_POWER_PINS` and
  `STD_CELL_GROUND_PINS`.

The layer pairs that the power grid connects (the rails to the vertical straps, and the vertical
to the horizontal straps) and the layers of the power pins (both strap layers) follow the power
grid script of LibreLane (`scripts/openroad/common/pdn_cfg.tcl`). LibreLane is licensed under the
Apache License, Version 2.0 (https://github.com/librelane/librelane).

The Yosys techmap files for latches and full adders stay in the build of `open_pdks`
(`libs.tech/openlane/sky130_fd_sc_hd`). The build has no map for clock gates, and xeda ships none.

Xeda reads the configuration files of an installed `open_pdks` build when a platform asks for
them, and it copies no file of it. `open_pdks` is licensed under the Apache License, Version 2.0
(https://github.com/RTimothyEdwards/open_pdks).
