proc reportCriticalPaths { fileName } {
    # Open the specified output file in write mode
    set FH [open $fileName w]
    # Write the current date and CSV format to a file header
    puts $FH "#\n# File created on [clock format [clock seconds]]\n#\n"
    puts $FH "Startpoint,Endpoint,DelayType,Slack,#Levels,#LUTs"
    # Iterate through both Min and Max delay types
    foreach delayType {max min} {
        # Collect details from the 50 worst timing paths for the current analysis
        # (max = setup/recovery, min = hold/removal)
        # The $path variable contains a Timing Path object.
        foreach path [get_timing_paths -delay_type $delayType -max_paths 50 -nworst 1] {
            # Get the LUT cells of the timing paths
            # set luts [get_cells -filter {REF_NAME =~ LUT*} -of_object $path] # print  ,[llength $luts] << TODO warnings
            # Get the startpoint of the Timing Path object
            set startpoint [get_property STARTPOINT_PIN $path]
            # Get the endpoint of the Timing Path object
            set endpoint [get_property ENDPOINT_PIN $path]
            # Get the slack on the Timing Path object
            set slack [get_property SLACK $path]
            # Get the number of logic levels between startpoint and endpoint
            set levels [get_property LOGIC_LEVELS $path]
            # Save the collected path details to the CSV file
            puts $FH "$startpoint,$endpoint,$delayType,$slack,$levels"
        }
    }
    # Close the output file
    close $FH
    puts "CSV file $fileName has been created.\n"
    return 0
}; # End PROC


proc showWarningsAndErrors {} {
  set num_errors     [get_msg_config -severity {ERROR} -count]
  set num_crit_warns [get_msg_config -severity {CRITICAL WARNING} -count]
  set num_warns      [get_msg_config -severity {WARNING} -count]

  if {$num_errors > 0} {
    puts "Exiting Vivado due to $num_errors error(s)!"
    exit 1
  }

  if {$num_crit_warns > 0} {
    puts "\n===========================( *ENABLE ECHO* )==========================="
    puts "** Number of Critical Warnings:  $num_crit_warns"
    {% if settings.fail_critical_warning -%}
    puts "Exiting due to $num_crit_warns critical warning(s)!"
    exit 1
    {%- endif %}
    puts "\n===========================( *DISABLE ECHO* )==========================="
  }

  if {$num_warns > 0} {
    puts "** Number of Warnings:           $num_warns"
  }

  puts "\n"
}

