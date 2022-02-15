set step $ACTIVE_STEP
set reports_dir ../../{{settings.reports_dir}}

# if {$step == {route_design}} {
    report_route_status -file ${reports_dir}/post_route/route_status.rpt
    puts "\n==============================( Writing Reports )================================"
    report_timing_summary -check_timing_verbose -no_header -report_unconstrained -path_type full -input_pins -max_paths 10 -delay_type min_max -file ${reports_dir}/post_route/timing_summary.rpt
    report_timing  -no_header -input_pins  -unique_pins -sort_by group -max_paths 100 -path_type full -delay_type min_max -file ${reports_dir}/post_route/timing.rpt
    report_utilization                                              -force -file ${reports_dir}/post_route/utilization.xml -format xml
    report_utilization -hierarchical                                -force -file ${reports_dir}/post_route/hierarchical_utilization.xml -format xml

    set timing_slack [get_property SLACK [get_timing_paths]]
    puts "Final timing slack: $timing_slack ns"

    # report_qor_suggestions -file ${reports_dir}/post_route/qor_suggestions.rpt 
    # -max_strategies 5
    # write_qor_suggestions -force qor_suggestions.rqs

    # close_project

    # showWarningsAndErrors

    if {$timing_slack < 0} {
        puts "\n===========================( *ENABLE ECHO* )==========================="
        puts "ERROR: Failed to meet timing by $timing_slack, see [file join ${reports_dir} post_route timing_summary.rpt] for details"
        puts "\n===========================( *DISABLE ECHO* )==========================="
        {% if settings.fail_timing %}
        exit 1
        {% endif %}    
    }

# }