# The restart header. Every OpenROAD process starts with it, because the database stores the
# design and the options that openroad_place set, and none of the following.
#
# The driver sets two variables before it includes this file:
#   restart    "netlist" to read a netlist, or "database" to read the database of the flow before
#   sdc_files  the SDC files to read, in order
read_liberty "@STORE@/tiny/tiny.lib"
read_db "@INPUTS@/grt.odb"
read_sdc "@INPUTS@/grt.sdc"
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
proc xeda_detail_route {} {
    xeda_stage detailedroute
    if {![grt::have_routes]} {
        error "xeda: the database has no global routes"
    }
    set_propagated_clock [all_clocks]
    set route_args [list -output_drc "reports/drc.rpt" -output_maze "reports/maze.log" -verbose 1 -droute_end_iter "64" -drc_report_iter_step "5"]
    detailed_route {*}$route_args
    set pass 1
    if {[repair_antennas "ANTENNA_X1"]} {
        detailed_route {*}$route_args
    }
    while {[check_antennas] && $pass < 5} {
        repair_antennas "ANTENNA_X1"
        detailed_route {*}$route_args
        incr pass
    }
    check_antennas -report_file "reports/antennas.rpt"
    if {![design_is_routed]} {
        error "xeda: the design has unrouted nets"
    }
    report_design_area
}
proc xeda_fillcell {} {
    xeda_stage fillcell
    filler_placement [list "FILL_X1"]
    check_placement
}
if {[catch {
    xeda_detail_route
    xeda_fillcell
} message options]} {
    catch { write_db "openroad-failed.odb" }
    return -options $options $message
}
write_db "route.odb"
write_sdc -no_timestamp "route.sdc"
