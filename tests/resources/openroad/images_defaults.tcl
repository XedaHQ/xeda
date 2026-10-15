if {![ord::openroad_gui_compiled]} {
    error "this OpenROAD has no GUI; set save_images: false"
}
# The restart header. Every OpenROAD process starts with it, because the database stores the
# design and the options that openroad_place set, and none of the following.
#
# The driver sets two variables before it includes this file:
#   restart    "netlist" to read a netlist, or "database" to read the database of the flow before
#   sdc_files  the SDC files to read, in order
read_liberty "@STORE@/tiny/tiny.lib"
read_db "@RUN@/final.odb"
read_sdc "@RUN@/final.sdc"
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
proc xeda_images {} {
    gui::save_display_controls

    set height [[[ord::get_db_block] getBBox] getDY]
    set height [ord::dbu_to_microns $height]
    set resolution [expr $height / 1000]

    set markerdb [[ord::get_db_block] findMarkerCategory DRC]
    if {$markerdb != "NULL" && [$markerdb getMarkerCount] > 0} {
        gui::select_marker_category $markerdb
    }

    gui::clear_highlights -1
    gui::clear_selections
    gui::fit

    # Start from known visibility, whatever an earlier session left.
    gui::set_display_controls "*" visible false
    gui::set_display_controls "Layers/*" visible true
    gui::set_display_controls "Nets/*" visible true
    gui::set_display_controls "Instances/*" visible true
    gui::set_display_controls "Shape Types/*" visible true
    gui::set_display_controls "Misc/Instances/*" visible false
    gui::set_display_controls "Misc/Instances/Pins" visible true
    gui::set_display_controls "Misc/Instances/Blockages" visible true
    gui::set_display_controls "Misc/Scale bar" visible true
    gui::set_display_controls "Misc/Highlight selected" visible true
    gui::set_display_controls "Misc/Detailed view" visible true

    # The routing view
    save_image -resolution $resolution "images/final_all.webp"
    gui::set_display_controls "Nets/Power" visible false
    gui::set_display_controls "Nets/Ground" visible false
    save_image -resolution $resolution "images/final_routing.webp"

    # The placement view without routing
    gui::set_display_controls "Shape Types/Routing/*" visible false
    gui::set_display_controls "Instances/Physical/Fill cell" visible false
    gui::set_display_controls "Misc/Instances/*" visible false
    save_image -resolution $resolution "images/final_placement.webp"

    # The clock view: the clock nets and the clock buffers
    gui::set_display_controls "Shape Types/Routing/*" visible true
    gui::set_display_controls "Nets/*" visible false
    gui::set_display_controls "Nets/Clock" visible true
    gui::set_display_controls "Instances/*" visible false
    gui::set_display_controls "Instances/StdCells/Clock tree/*" visible true
    gui::set_display_controls "Instances/StdCells/Sequential" visible true
    gui::set_display_controls "Instances/Macro" visible true
    select -name "clk*" -type Inst
    save_image -resolution $resolution "images/final_clocks.webp"
    gui::clear_selections

    # The resizer view: the instances that the resizer made, in one color for each kind
    gui::set_display_controls "Nets/*" visible true
    gui::set_display_controls "Nets/Power" visible false
    gui::set_display_controls "Nets/Ground" visible false
    gui::set_display_controls "Shape Types/Routing/*" visible false
    gui::set_display_controls "Instances/*" visible true
    gui::set_display_controls "Instances/Physical/*" visible false
    select -name "hold*" -type Inst -highlight 0
    select -name "input*" -type Inst -highlight 1
    select -name "output*" -type Inst -highlight 1
    select -name "repeater*" -type Inst -highlight 3
    select -name "fanout*" -type Inst -highlight 3
    select -name "load_slew*" -type Inst -highlight 3
    select -name "max_cap*" -type Inst -highlight 3
    select -name "max_length*" -type Inst -highlight 3
    select -name "wire*" -type Inst -highlight 3
    select -name "rebuffer*" -type Inst -highlight 4
    select -name "split*" -type Inst -highlight 5
    save_image -resolution $resolution "images/final_resizer.webp"
    gui::clear_highlights -1
    gui::clear_selections

    # The routing congestion view
    gui::set_display_controls "Instances/*" visible true
    gui::set_display_controls "Instances/Physical/*" visible false
    gui::set_display_controls "Nets/*" visible false
    gui::set_display_controls "Heat Maps/Routing Congestion" visible true
    save_image -resolution $resolution "images/final_congestion.webp"

    # The view of the worst timing path
    gui::set_display_controls "Heat Maps/Routing Congestion" visible false
    gui::set_display_controls "Timing Path/*" visible true
    gui::set_display_controls "Nets/*" visible false
    gui::set_display_controls "Instances/*" visible true
    gui::set_display_controls "Shape Types/Routing/*" visible false
    gui::show_worst_path
    save_image -resolution $resolution "images/final_worst_path.webp"
    gui::set_display_controls "Timing Path/*" visible false

    gui::restore_display_controls
}
read_spef "@RUN@/final.spef"
gui::show "xeda_images" false
