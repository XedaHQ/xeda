proc reportCriticalPaths { fileName } {
  set FH [open $fileName w]
  puts $FH "Startpoint,Endpoint,DelayType,Slack,Levels,LogicDelay,TotalDelay"
  foreach delayType {max min} {
    # (max = setup/recovery, min = hold/removal)
    foreach path [get_timing_paths -delay_type $delayType -max_paths 50 -nworst 1] {
      set startpoint [get_property STARTPOINT_PIN $path]
      set endpoint [get_property ENDPOINT_PIN $path]
      # Get the slack on the Timing Path object
      set slack [get_property SLACK $path]
      # Get the number of logic levels between startpoint and endpoint
      set levels [get_property LOGIC_LEVELS $path]
      # Get the logic delay
      set logic_delay [get_property DATAPATH_LOGIC_DELAY $path]
      # Get the total datapath delay
      set delay [get_property DATAPATH_DELAY $path]
      # Write to the CSV file
      puts $FH "$startpoint,$endpoint,$delayType,$slack,$levels,$logic_delay,$delay"
    }
  }
  close $FH
  puts "Created critical paths report: $fileName\n"
}

proc showWarningsAndErrors {} {
  set num_errors     [get_msg_config -severity {ERROR} -count]
  set num_crit_warns [get_msg_config -severity {CRITICAL WARNING} -count]
  set num_warns      [get_msg_config -severity {WARNING} -count]
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
  if {$num_warns > 0} {
    puts "** Number of Warnings:           $num_warns"
  }
  puts "\n"
}
