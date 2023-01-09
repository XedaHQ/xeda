variable reports_dir "{{reports_dir}}"
variable results_dir "{{results_dir}}"

proc read_libraries {} {
  {% if (platform.corner|length) > 1 %}
  #------------------------------- Multi-corner --------------------------------#
  define_corners {{platform.corner.keys()|join(" ")}}
  {% for corner,s in platform.corner %}
  {% for lib in s.lib_files %} read_liberty -corner {{corner}} {{lib}} {% endfor %}
  {% endfor %}
  #-----------------------------------------------------------------------------#
  {% else %}
  #------------------------------ Single corner --------------------------------#
  {% set s = (platform.corner.values()|first) %}
  {% for lib in s.lib_files %}
  read_liberty {{lib}}
  {% endfor %}
  #-----------------------------------------------------------------------------#
  {% endif %}
  {% for lib in settings.extra_liberty_files %}
  read_liberty {{lib}}
  {% endfor %}
}

proc print_banner {header} {
  puts "\n=========================================================================="
  puts "$header"
  puts "--------------------------------------------------------------------------"
}

proc report_metrics { when {include_erc true} {include_clock_skew true} } {
  print_banner "$when check_setup"
  check_setup

  print_banner "$when report_tns"
  report_tns
  report_tns_metric

  print_banner "$when report_wns"
  report_wns

  print_banner "$when report_worst_slack"
  report_worst_slack
  report_worst_slack_metric

  if {$include_clock_skew} {
    print_banner "$when report_clock_skew"
    report_clock_skew
    report_clock_skew_metric
    report_clock_skew_metric -hold
  }

  print_banner "$when report_checks -path_delay min"
  report_checks -path_delay min -fields {slew cap input nets fanout} -format full_clock_expanded

  print_banner "$when report_checks -path_delay max"
  report_checks -path_delay max -fields {slew cap input nets fanout} -format full_clock_expanded

  print_banner "$when report_checks -unconstrained"
  report_checks -unconstrained -fields {slew cap input nets fanout} -format full_clock_expanded

  if {$include_erc} {
    print_banner "$when report_check_types -max_slew -max_cap -max_fanout -violators"
    report_check_types -max_slew -max_capacitance -max_fanout -violators
    report_erc_metrics

    print_banner "$when max_slew_check_slack"
    puts "[sta::max_slew_check_slack]"

    print_banner "$when max_slew_check_limit"
    puts "[sta::max_slew_check_limit]"

    if {[sta::max_slew_check_limit] < 1e30} {
      print_banner "$when max_slew_check_slack_limit"
      puts [format "%.4f" [sta::max_slew_check_slack_limit]]
    }

    print_banner "$when max_fanout_check_slack"
    puts "[sta::max_fanout_check_slack]"

    print_banner "$when max_fanout_check_limit"
    puts "[sta::max_fanout_check_limit]"

    if {[sta::max_fanout_check_limit] < 1e30} {
      print_banner "$when max_fanout_check_slack_limit"
      puts [format "%.4f" [sta::max_fanout_check_slack_limit]]
    }

    print_banner "$when max_capacitance_check_slack"
    puts "[sta::max_capacitance_check_slack]"

    print_banner "$when max_capacitance_check_limit"
    puts "[sta::max_capacitance_check_limit]"

    if {[sta::max_capacitance_check_limit] < 1e30} {
      print_banner "$when max_capacitance_check_slack_limit"
      puts [format "%.4f" [sta::max_capacitance_check_slack_limit]]
    }

    print_banner "$when max_slew_violation_count"
    puts "max slew violation count [sta::max_slew_violation_count]"

    print_banner "$when max_fanout_violation_count"
    puts "max fanout violation count [sta::max_fanout_violation_count]"

    print_banner "$when max_cap_violation_count"
    puts "max cap violation count [sta::max_capacitance_violation_count]"

    print_banner "$when setup_violation_count"
    puts "setup violation count [llength [find_timing_paths -path_delay max -slack_max 0]]"

    print_banner "$when hold_violation_count"
    puts "hold violation count [llength [find_timing_paths -path_delay min -slack_max 0]]"

    set critical_path [lindex [find_timing_paths -sort_by_slack] 0]
    if {$critical_path != ""} {
      set path_delay [sta::format_time [[$critical_path path] arrival] 4]
      set path_slack [sta::format_time [[$critical_path path] slack] 4]
    } else {
      set path_delay -1
      set path_slack 0
    }
    print_banner "$when critical path delay"
    puts "$path_delay"

    print_banner "$when critical path slack"
    puts "$path_slack"

    print_banner "$when slack div critical path delay"
    puts "[format "%4f" [expr $path_slack / $path_delay * 100]]"
  }

  print_banner "$when report_power"
  if {[info exists ::env(CORNERS)]} {
    foreach corner $::env(CORNERS) {
      puts "Corner: $corner"
      report_power -corner $corner
      report_power_metric -corner $corner
    }
    unset corner
  } else {
    report_power
    report_power_metric
  }

  print_banner "$when report_design_area"
  report_design_area
  report_design_area_metrics

  puts ""
}

## density_fill/finalize
# Delete routing obstructions for final DEF
proc deleteRoutingObstructions {} {
  set db [ord::get_db]
  set chip [$db getChip]
  set block [$chip getBlock]
  set obstructions [$block getObstructions]

  foreach obstruction $obstructions {
    odb::dbObstruction_destroy $obstruction
  }
  puts "\[INFO\] Deleted [llength $obstructions] routing obstructions"
}

proc find_macros {} {
  set macros ""

  set db [::ord::get_db]
  set block [[$db getChip] getBlock]
  foreach inst [$block getInsts] {
    set inst_master [$inst getMaster]

    # BLOCK means MACRO cells
    if { [string match [$inst_master getType] "BLOCK"] } {
      append macros " " $inst
    }
  }
  return $macros
}

## floorplan functions
proc read_macro_placement {macro_placement_file} {
  set block [ord::get_db_block]
  set units [$block getDefUnits]

  set ch [open $macro_placement_file]

  while {![eof $ch]} {
    set line [gets $ch]
    if {[llength $line] == 0} {continue}

    set inst_name [lindex $line 0]
    set orientation [lindex $line 1]
    set x [expr round([lindex $line 2] * $units)]
    set y [expr round([lindex $line 3] * $units)]

    if {[set inst [$block findInst $inst_name]] == "NULL"} {
      error "Cannot find instance $inst_name"
    }

    $inst setOrient $orientation
    $inst setOrigin $x $y
    $inst setPlacementStatus FIRM
  }

  close $ch
}

proc block_channels {channel_width_in_microns} {
  set tech [ord::get_db_tech]
  set units [$tech getDbUnitsPerMicron]
  set block [ord::get_db_block]

  #
  # Collect up all the macros
  #
  set shapes {}
  foreach inst [$block getInsts] {
    if {[[$inst getMaster] getType] == "BLOCK"} {
      set box [$inst getBBox]
      lappend shapes [odb::newSetFromRect [$box xMin] [$box yMin] [$box xMax] [$box yMax]]
    }
  }

  #
  # Resize to fill the channels and edge gap
  #
  set resize_by [expr round($channel_width_in_microns * $units)]
  set shapeSet [odb::orSets $shapes]
  set shapeSet [odb::bloatSet $shapeSet $resize_by]

  #
  # Clip result to the core area
  #
  set core [$block getCoreArea]
  set xl [$core xMin]
  set yl [$core yMin]
  set xh [$core xMax]
  set yh [$core yMax]
  set core_rect [odb::newSetFromRect $xl $yl $xh $yh]
  set shapeSet [odb::andSet $shapeSet $core_rect]

  #
  # Output the blockages
  #
  set rects [odb::getRectangles $shapeSet]
  foreach rect $rects {
    set b [odb::dbBlockage_create $block \
      [$rect xMin] [$rect yMin] [$rect xMax] [$rect yMax]]
    $b setSoft
  }
}

proc save_images {route_drc_rpt} {
  gui::save_display_controls

  set height [[[ord::get_db_block] getBBox] getDY]
  set height [ord::dbu_to_microns $height]
  set resolution [expr $height / 1000]

  # Show the drc markers (if any)
  if {[file exists $route_drc_rpt] == 1} {
    gui::load_drc $route_drc_rpt
  }

  gui::clear_selections

  # Setup initial visibility to avoid any previous settings
  gui::set_display_controls "*" visible false
  gui::set_display_controls "Layers/*" visible true
  gui::set_display_controls "Nets/*" visible true
  gui::set_display_controls "Instances/*" visible false
  gui::set_display_controls "Instances/StdCells/*" visible true
  gui::set_display_controls "Instances/Macro" visible true
  gui::set_display_controls "Instances/Pads/*" visible true
  gui::set_display_controls "Instances/Physical/*" visible true
  gui::set_display_controls "Pin Markers" visible true
  gui::set_display_controls "Misc/Instances/names" visible true
  gui::set_display_controls "Misc/Scale bar" visible true
  gui::set_display_controls "Misc/Highlight selected" visible true
  gui::set_display_controls "Misc/Detailed view" visible true

  # The routing view
  save_image -resolution $resolution $reports_dir/final_routing.webp

  # The placement view without routing
  gui::set_display_controls "Layers/*" visible false
  gui::set_display_controls "Instances/Physical/*" visible false
  save_image -resolution $resolution $reports_dir/final_placement.webp

  {% if platform.pwr_nets_voltages %}
  gui::set_display_controls "Heat Maps/IR Drop" visible true
  gui::set_heatmap IRDrop Layer {{platform.ir_drop_layer}}
  gui::set_heatmap IRDrop ShowLegend 1
  save_image -resolution $resolution $reports_dir/final_ir_drop.webp
  gui::set_display_controls "Heat Maps/IR Drop" visible false
  {% endif %}

  # The clock view: all clock nets and buffers
  gui::set_display_controls "Layers/*" visible true
  gui::set_display_controls "Nets/*" visible false
  gui::set_display_controls "Nets/Clock" visible true
  gui::set_display_controls "Instances/*" visible false
  gui::set_display_controls "Instances/StdCells/Clock tree/*" visible true
  select -name "clk*" -type Inst
  save_image -resolution $resolution $reports_dir/final_clocks.webp
  gui::clear_selections

  # The resizer view: all instances created by the resizer grouped
  gui::set_display_controls "Layers/*" visible false
  gui::set_display_controls "Instances/*" visible true
  gui::set_display_controls "Instances/Physical/*" visible false
  select -name "hold*" -type Inst -highlight 0       ;# green
  select -name "input*" -type Inst -highlight 1      ;# yellow
  select -name "output*" -type Inst -highlight 1
  select -name "repeater*" -type Inst -highlight 3   ;# magenta
  select -name "fanout*" -type Inst -highlight 3
  select -name "load_slew*" -type Inst -highlight 3
  select -name "max_cap*" -type Inst -highlight 3
  select -name "max_length*" -type Inst -highlight 3
  select -name "wire*" -type Inst -highlight 3
  select -name "rebuffer*" -type Inst -highlight 4   ;# red
  select -name "split*" -type Inst -highlight 5      ;# dark green

  save_image -resolution $resolution $reports_dir/final_resizer.webp
  for {set i 0} {$i <= 5} {incr i} {
    gui::clear_highlights $i
  }
  gui::clear_selections

  foreach clock [get_clocks *] {
    set clock_name [get_name $clock]
    gui::save_clocktree_image $reports_dir/cts_$clock_name.webp $clock_name
  }

  gui::restore_display_controls
}
