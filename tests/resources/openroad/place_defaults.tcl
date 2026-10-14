# The restart header. Every OpenROAD process starts with it, because the database stores the
# design and the options that openroad_place set, and none of the following.
#
# The driver sets two variables before it includes this file:
#   restart    "netlist" to read a netlist, or "database" to read the database of the flow before
#   sdc_files  the SDC files to read, in order
read_liberty "@STORE@/tiny/tiny.lib"
read_lef "@STORE@/tiny/tiny.tlef"
read_lef "@STORE@/tiny/tiny.lef"
read_verilog "@DESIGN@/counter_netlist.v"
# The source file names stay out of the database, so it does not depend on where the files were.
link_design -omit_filename_prop "counter"
read_sdc "clocks.sdc"
# The wire RC is no part of the database. The choice of the layers sets the parasitics that the
# estimates use, so it sets what the repair commands decide.
set_layer_rc -layer "m1" -resistance 0.0085 -capacitance 0.00015
set_layer_rc -layer "m2" -resistance 0.0025 -capacitance 0.00014
set_layer_rc -layer "m3" -resistance 0.0015 -capacitance 0.00012
set_layer_rc -layer "m4" -resistance 0.0003 -capacitance 0.0001
set_layer_rc -via "v1" -resistance 0.002
set_layer_rc -via "v2" -resistance 0.0004
set_layer_rc -via "v3" -resistance 0.0002
set_wire_rc -signal -layer "m2"
set_wire_rc -clock -layer "m3"
# The command takes one argument that is a list. Call it before any command that resizes,
# places or builds a clock tree.
set_dont_use [list "INV_X4" "NAND2_X4"]
# The helper procedures that the stages of every driver call.

# Every stage sets its own metrics stage. A stage that does not would write its keys under the
# name of the stage before it, because the name stays set until the next call.
proc xeda_stage {name} {
    utl::set_metrics_stage "${name}__{}"
    puts "xeda: stage $name"
}

# The metrics of a report. `erc` writes the violation counts and the limits of the electrical
# rules, and `skew` writes the clock skew. A stage that does not have clock trees yet (before
# clock tree synthesis) asks for no skew. The worst slack is the setup slack: positive when
# timing is met. OpenROAD writes no worst negative slack, so a check must test the sign of the
# worst slack.
proc xeda_metrics {erc skew} {
    report_tns_metric
    report_tns_metric -hold
    report_worst_slack_metric
    report_worst_slack_metric -hold
    report_fmax_metric
    if {$skew} {
        report_clock_skew_metric
        report_clock_skew_metric -hold
    }
    if {$erc} {
        report_erc_metrics
    }
    report_power_metric
    report_design_area_metrics
}

# The pin of a tie cell as `repair_tie_fanout` takes it: <library>/<cell>/<port>. The name of the
# library comes from the Liberty file, so the platform does not need to tell it.
proc xeda_tie_pin {cell port} {
    set lib [get_name [get_property [lindex [get_lib_cell $cell] 0] library]]
    return "$lib/$cell/$port"
}

# The masters that only occupy the layout: filler, tap and end cap cells and the like. A netlist
# for simulation or equivalence checking must not hold their instances.
proc xeda_physical_only_masters {} {
    set patterns {COVER COVER_BUMP RING PAD_SPACER CORE_FEEDTHROUGH CORE_SPACER CORE_ANTENNACELL CORE_WELLTAP ENDCAP*}
    set masters {}
    foreach lib [[ord::get_db] getLibs] {
        foreach master [$lib getMasters] {
            foreach pattern $patterns {
                if {[string match $pattern [$master getType]]} {
                    lappend masters [$master getName]
                    break
                }
            }
        }
    }
    return $masters
}
proc xeda_floorplan {} {
    xeda_stage synth
    eliminate_dead_logic
    report_design_area
    report_design_area_metrics
    xeda_stage floorplan
    check_setup
    # The utilization is that of the netlist as it is now. The repairs below and the tap cells add
    # area, so the utilization after the placement is higher than this value.
    initialize_floorplan -utilization 50.0 -aspect_ratio 1.0 -core_space 1.0 -site "tinysite"
    make_tracks "m1" -x_offset 0.1 -x_pitch 0.2 -y_offset 0.05 -y_pitch 0.2
    make_tracks "m2" -x_offset 0.1 -x_pitch 0.2 -y_offset 0.15 -y_pitch 0.3
    make_tracks "m3" -x_offset 0.2 -x_pitch 0.4 -y_offset 0.2 -y_pitch 0.4
    set_global_routing_layer_adjustment "m2-m3" 0.5
    set_global_routing_layer_adjustment "m4" 0.25
    set_routing_layers -clock "m3-m4"
    set_routing_layers -signal "m2-m4"
    # The tie cells come from the synthesis: the flow never inserts them. Each load gets its own
    # copy of the tie cell. The repair comes before any removal of buffers, since one tie cell
    # can drive buffers that drive many loads.
    repair_tie_fanout -separation "0.0" [xeda_tie_pin "TIELO_X1" "Z"]
    repair_tie_fanout -separation "0.0" [xeda_tie_pin "TIEHI_SMALL" "Z"]
    # The repair runs before the placement, with no parasitics: the wires cost nothing. Do not add
    # `estimate_parasitics` here, since that changes what the repair decides.
    repair_timing -setup -setup_margin 0.0 -hold_margin 0.0 -sequence "unbuffer,sizeup,swap,vt_swap" -repair_tns "100.0" -skip_last_gasp -verbose
    report_units_metric
    xeda_metrics 0 0
}
proc xeda_tapcell {} {
    xeda_stage tapcell
    tapcell -distance 12.0 -tapcell_master "TAP_X1" -endcap_master "ENDCAP_X1"
}
proc xeda_pdn {} {
    xeda_stage pdn
    add_global_connection -net "VDD" -pin_pattern "^VDD\$" -power
    add_global_connection -net "VSS" -pin_pattern "^VSS\$" -ground
    global_connect
    set_voltage_domain -name CORE -power "VDD" -ground "VSS"
    define_pdn_grid -name grid -voltage_domains CORE -pins [list "m4"]
    add_pdn_stripe -grid grid -layer "m1" -width 0.12 -pitch 1.4 -offset 0.0 -followpins
    add_pdn_stripe -grid grid -layer "m3" -width 0.24 -pitch 8.0 -offset 1.0
    add_pdn_stripe -grid grid -layer "m4" -width 0.48 -pitch 12.0 -offset 1.0
    add_pdn_connect -grid grid -layers [list "m1" "m3"]
    add_pdn_connect -grid grid -layers [list "m3" "m4"]
    pdngen
}
# The target density for `global_placement`. The cells change between the placements (the buffers
# go and come back), so a density that comes from the netlist is computed again at each call, and
# no stage keeps a number from an earlier call.
proc xeda_density {} {
    return 0.6
}
proc xeda_global_place_skip_io {} {
    xeda_stage globalplace_skip_io
    if {![all_pins_placed]} {
        global_placement -skip_io -density [xeda_density] -pad_left 0 -pad_right 0
    }
}
proc xeda_place_pins {} {
    place_pins -hor_layers [list "m3"] -ver_layers [list "m4"]
}
proc xeda_io_placement {} {
    xeda_stage io_placement
    xeda_place_pins
}
proc xeda_global_place {} {
    xeda_stage globalplace
    remove_buffers
    buffer_ports
    global_placement -density [xeda_density] -pad_left 0 -pad_right 0 -routability_driven -timing_driven -force_center_initial_place
    estimate_parasitics -placement
    xeda_metrics 0 0
}
proc xeda_resize {} {
    xeda_stage placeopt
    estimate_parasitics -placement
    repair_design -slew_margin "0.0" -cap_margin "0.0" -verbose
    report_floating_nets
    xeda_metrics 1 0
}
proc xeda_detail_place {} {
    xeda_stage detailedplace
    set_placement_padding -global -left 0 -right 0
    detailed_placement
    improve_placement -max_displacement "5 1"
    optimize_mirroring
    if {[catch {check_placement -verbose} message]} {
        puts "xeda: check_placement found violations ($message), and the flow goes on"
    }
    estimate_parasitics -placement
    xeda_metrics 1 0
}
if {[catch {
    xeda_floorplan
    xeda_tapcell
    xeda_pdn
    xeda_global_place_skip_io
    xeda_io_placement
    xeda_global_place
    xeda_resize
    xeda_detail_place
} message options]} {
    catch { write_db "openroad_place-failed.odb" }
    return -options $options $message
}
write_db "@RUN@/place.odb"
write_sdc -no_timestamp "@RUN@/place.sdc"
