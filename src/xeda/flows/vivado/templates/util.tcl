# CSV column name -> timing path property. Single source of truth for the header
# and the rows, and shared by every report below so they cannot drift apart.
proc timingPathColumns {} {
  return {
    Startpoint  STARTPOINT_PIN
    StartClock  STARTPOINT_CLOCK
    Endpoint    ENDPOINT_PIN
    EndClock    ENDPOINT_CLOCK
    PathGroup   GROUP
    Requirement REQUIREMENT
    Slack       SLACK
    Levels      LOGIC_LEVELS
    MaxFanout   MAX_FANOUT
    LogicDelay  DATAPATH_LOGIC_DELAY
    NetDelay    DATAPATH_NET_DELAY
    TotalDelay  DATAPATH_DELAY
    Skew        SKEW
    Uncertainty UNCERTAINTY
  }
}

proc writeTimingPathsCsv {fileName paths} {
  set columns [timingPathColumns]
  set FH [open $fileName w]
  puts $FH [join [dict keys $columns] ","]
  foreach path $paths {
    set fields {}
    foreach prop [dict values $columns] {
      lappend fields [get_property $prop $path]
    }
    puts $FH [join $fields ","]
  }
  close $FH
}

# The $num_paths worst setup paths, worst (lowest) slack first.
#
# Vivado returns them already in that order, so the rows are written exactly as
# they come back. -sort_by slack is what makes -max_paths a design-wide cap
# returning the $num_paths worst paths of the whole design; under -sort_by group
# it returns up to $num_paths paths *per path group* (one group per clock),
# concatenated group after group.
proc reportCriticalPaths {fileName num_paths} {
  # (max = setup/recovery, min = hold/removal)
  set paths [get_timing_paths -delay_type max -max_paths $num_paths -nworst 1 -sort_by slack]
  writeTimingPathsCsv $fileName $paths
  puts "Created critical paths report by slack ([llength $paths] paths): $fileName\n"
}

# The $num_paths longest setup paths, largest total datapath delay first.
#
# Vivado cannot hand these back in delay order -- get_timing_paths -sort_by only
# accepts slack or group -- and the longest paths are not necessarily the worst
# by slack, since slack also folds in the clock period, the clock skew and the
# setup requirement of the endpoint. So ask for the worst $num_candidates paths
# by slack and keep the $num_paths longest of those. Widen $num_candidates if a
# design has clock domains whose delays and slacks are far apart.
proc reportCriticalPathsByDelay {fileName num_paths {num_candidates 0}} {
  if {$num_candidates < $num_paths} {
    set num_candidates [expr {10 * $num_paths}]
  }
  # (max = setup/recovery, min = hold/removal)
  set candidates [get_timing_paths -delay_type max -max_paths $num_candidates \
                                   -nworst 1 -sort_by slack]
  set ranked {}
  foreach path $candidates {
    set delay [get_property DATAPATH_DELAY $path]
    # unconstrained paths can come back without a delay: keep them, but last
    if {![string is double -strict $delay]} { set delay -1e30 }
    lappend ranked [list $delay $path]
  }
  set paths {}
  foreach entry [lrange [lsort -real -decreasing -index 0 $ranked] 0 [expr {$num_paths - 1}]] {
    lappend paths [lindex $entry 1]
  }
  writeTimingPathsCsv $fileName $paths
  puts "Created critical paths report by delay ([llength $paths] of\
        [llength $candidates] candidates): $fileName\n"
}

proc showWarningsAndErrors {} {
  set num_errors [get_msg_config -severity {ERROR} -count]
  set num_crit_warns [get_msg_config -severity {CRITICAL WARNING} -count]
  set num_warns [get_msg_config -severity {WARNING} -count]
  if {$num_errors > 0} {
    puts "Exiting Vivado due to $num_errors error(s)!"
    exit 1
  }
  if {$num_crit_warns > 0} {
    puts "** Number of Critical Warnings:  $num_crit_warns"
    {%- if settings.fail_critical_warning %}
    puts "Exiting due to $num_crit_warns critical warning(s)!"
    exit 1
    {%- endif %}
  }
  # if $num_warns is non-emty and not zero
  if {[string length $num_warns] > 0 && $num_warns != 0} {
    puts "** Number of Warnings:      $num_warns"
  }
  puts "\n"
}
