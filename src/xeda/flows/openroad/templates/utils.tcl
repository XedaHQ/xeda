variable reports_dir "{{settings.reports_dir}}"
variable results_dir "{{settings.results_dir}}"

proc read_libraries {} {
  {% if (settings.multi_corner and platform.corner|length) > 1 %}
  #------------------------------- Multi-corner --------------------------------#
  define_corners {{platform.corner.keys()|join(" ")}}
  {% for corner,s in platform.corner.items() %}
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
  {% if (settings.multi_corner and platform.corner|length) > 1 %}
  {% for corner,s in platform.corner %}
  puts "Corner: {{corner}}"
  report_power -corner {{corner}}
  report_power_metric -corner {{corner}}
  {% endfor %}
  {% else %}
  report_power
  report_power_metric
  {% endif %}

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
