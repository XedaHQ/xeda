# written for xeda's tests
# A small configuration in the style of the open_pdks and IHP files.

# Keys that the reader does not map are ignored, whatever they hold.
set ::env(PROCESS) 130
set ::env(TECH_LEF) "$::env(PDK_ROOT)/$::env(PDK)/libs.ref/tiny.tlef"
set ::env(CELLS_LEF) [glob "$::env(PDK_ROOT)/lef/*.lef"]
set ::env(GRT_LAYER_ADJUSTMENTS) "0.9,0,0,0"
set ::env(FP_PDN_VSPACING) 1.7
set ::env(FP_PDN_CORE_RING_VWIDTH) 1.6
if { ![info exist ::env(STD_CELL_LIBRARY)] } {
	set ::env(STD_CELL_LIBRARY) tiny_lib
}
set ::env(SYNTH_DRIVING_CELL_PIN) "X"

# Placement and cells
set ::env(PLACE_SITE) "core"
set ::env(FP_WELLTAP_CELL) "tiny_$::env(PLACE_SITE)_tap"
set ::env(FP_ENDCAP_CELL) {tiny_endcap}
set ::env(FP_TAPCELL_DIST) 12.5

# Synthesis
set ::env(SYNTH_TIEHI_PORT) "tiny_tiehi HI"
set ::env(SYNTH_TIELO_PORT) "tiny_tielo LO"
set ::env(SYNTH_MIN_BUF_PORT) "tiny_buf_1 A X"
set ::env(SYNTH_DRIVING_CELL) "tiny_buf_4/X"
set ::env(OUTPUT_CAP_LOAD) "6.0" ; # femtofarad

# Filling and antenna
set ::env(FILL_CELL) "tiny_fill_2 tiny_fill_1"
set ::env(DECAP_CELL) "tiny_decap_3"
set ::env(DIODE_CELL) "tiny_diode"
set ::env(DIODE_CELL_PIN) "DIODE"

# Layers
set ::env(RT_MIN_LAYER) "m1" ; # lowest layer
set ::env(RT_MAX_LAYER) m4
set ::env(RT_CLOCK_MIN_LAYER) "m3"
set ::env(FP_IO_HLAYER) "m3"
set ::env(FP_IO_VLAYER) "${::env(RT_MAX_LAYER)}"
set ::env(DATA_WIRE_RC_LAYER) "m2"
set ::env(CLOCK_WIRE_RC_LAYER) "m4"

# Clock tree
set ::env(CTS_ROOT_BUFFER) tiny_clkbuf_8
set ::env(CTS_CLK_BUFFER_LIST) "tiny_clkbuf_4 tiny_clkbuf_2 \
    tiny_clkbuf_8"

# Wire resistance and capacitance, and via resistance
set ::env(LAYERS_RC) [dict create]
dict set ::env(LAYERS_RC) "*" m1 res 8.5E-03
dict set ::env(LAYERS_RC) "*" m1 cap 1e-10
dict set ::env(LAYERS_RC) "*" m2 res 2.5E-03
dict set ::env(LAYERS_RC) "*" m2 cap 1.5E-04
set ::env(VIAS_R) [dict create]
dict set ::env(VIAS_R) "*" via1 res 2.0E-3
dict set ::env(VIAS_R) "*" via2 res 0.4E-3

# Power grid
set ::env(FP_PDN_RAIL_LAYER) m1
set ::env(FP_PDN_RAIL_WIDTH) 0.48
set ::env(FP_PDN_RAIL_OFFSET) 0
set ::env(FP_PDN_VERTICAL_LAYER) m3
set ::env(FP_PDN_HORIZONTAL_LAYER) m4
set ::env(FP_PDN_VWIDTH) 1.6
set ::env(FP_PDN_VPITCH) 150
set ::env(FP_PDN_VOFFSET) 16.5
set ::env(FP_PDN_HWIDTH) 1.8
set ::env(FP_PDN_HPITCH) 160
set ::env(FP_PDN_HOFFSET) 17
