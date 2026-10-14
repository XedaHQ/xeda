# written for xeda's tests
# A small configuration in the style of OpenROAD-flow-scripts.

export PROCESS = 45

# File names are ignored: the residue names the files.
export TECH_LEF = $(PLATFORM_DIR)/lef/tiny.tlef
export LIB_FILES = $(PLATFORM_DIR)/lib/tiny.lib \
                     $(ADDITIONAL_LIBS)
export GDS_FILES = $(sort $(wildcard $(PLATFORM_DIR)/gds/*.gds)) \
                     $(ADDITIONAL_GDS)
export LATCH_MAP_FILE = $(PLATFORM_DIR)/cells_latch.v
export KLAYOUT_TECH_FILE = $(PLATFORM_DIR)/tiny.lyt
export PDN_TCL ?= $(PLATFORM_DIR)/grid.tcl

# Cells
export PLACE_SITE = TinySite
export DONT_USE_CELLS = TINY_TAP_X1 \
                        TINY_FILL_X1 # not for synthesis
export DONT_USE_CELLS += TINY_NAND_X1
export FILL_CELLS ?= TINY_FILL_X1 TINY_FILL_X2
export FILL_CELLS ?= TINY_FILL_X8
export TAP_CELL_NAME = TINY_TAP_X1
export TIEHI_CELL_AND_PORT = TINY_TIEHI Z
export TIELO_CELL_AND_PORT = TINY_TIELO Z
export MIN_BUF_CELL_AND_PORTS = TINY_BUF_X1 A Z
export ABC_DRIVER_CELL = TINY_BUF_X2
export ABC_LOAD_IN_FF = 3.5
export SYNTH_MINIMUM_KEEP_SIZE ?= 10000

# Layers
IO_PLACER_H ?= m5
export IO_PLACER_V ?= m6
export MIN_ROUTING_LAYER = m2
export MIN_CLK_ROUTING_LAYER = m4
export MAX_ROUTING_LAYER := m10
export ROUTING_LAYER_ADJUSTMENT ?= 0.25

# Placement and optimization
export PLACE_DENSITY ?= 0.30
export MATCH_CELL_FOOTPRINT = 1
export OPT_POST_GRT_WNS ?= 0

# Supply
export PWR_NETS_VOLTAGES  ?= VDD 1.1 VDD2 0.9
export GND_NETS_VOLTAGES  ?= VSS 0.0
export IR_DROP_LAYER ?= m1
