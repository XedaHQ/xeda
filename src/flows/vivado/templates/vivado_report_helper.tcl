{% include 'util.tcl' %}
showWarningsAndErrors

puts "\n==========================( Writing Reports after $ACTIVE_STEP )============================"
set reports_dir [file join ../../{{settings.reports_dir}} $ACTIVE_STEP]
set outputs_dir [file join ../../{{settings.outputs_dir}} $ACTIVE_STEP]
file mkdir ${reports_dir}
file mkdir ${outputs_dir}

report_route_status -file [file join ${reports_dir} route_status.rpt]
report_timing_summary -check_timing_verbose -no_header -report_unconstrained -path_type full -input_pins -max_paths 10 -delay_type min_max -file [file join ${reports_dir} timing_summary.rpt]
report_timing  -no_header -input_pins  -unique_pins -sort_by group -max_paths 100 -path_type full -delay_type min_max -file [file join ${reports_dir} timing.rpt]
report_utilization -force -file [file join ${reports_dir} utilization.xml]              -format xml
report_utilization -force -file [file join ${reports_dir} hierarchical_utilization.xml] -format xml  -hierarchical 

if {$ACTIVE_STEP == "route_design"} {
    set timing_slack [get_property SLACK [get_timing_paths]]
    puts "Final timing slack: $timing_slack ns"

    {%- if settings.qor_suggestions %}
    report_qor_suggestions -quiet -max_strategies 5 -file [file join ${reports_dir} qor_suggestions.rpt] 
    write_qor_suggestions -quiet -strategy_dir  ./strategy_suggestions -force ./qor_suggestions.rqs
    {%- endif %}

    if {$timing_slack < 0} {
        puts "\n===========================( *ENABLE ECHO* )==========================="
        puts "ERROR: Failed to meet timing by $timing_slack, see [file join ${reports_dir} post_route timing_summary.rpt] for details"
        puts "\n===========================( *DISABLE ECHO* )==========================="
        
        {%- if settings.fail_timing %}
        exit 1
        {%- endif %}
    }

    {%- if settings.write_netlist -%}
    puts "\n==========================( Writing netlists and SDF )=========================="
    write_verilog -mode timesim -sdf_anno false -force -file ${outputs_dir}/timesim.v
    write_sdf -mode timesim -process_corner slow -force -file ${outputs_dir}/timesim.min.sdf
    write_sdf -mode timesim -process_corner fast -force -file ${outputs_dir}/timesim.max.sdf
    write_vhdl -mode funcsim -include_xilinx_libs -write_all_overrides -force -file ${outputs_dir}/funcsim.vhdl
    write_xdc -no_fixed_only -force ${outputs_dir}/impl.xdc
    {%- endif %}

    {%- if settings.write_bitstream %}
    puts "\n===========================( Writing bitstream )================================="
    write_bitstream -force {{design.rtl.top}}.bit
    {%- endif %}
}

puts "\n=======================( Finished $ACTIVE_STEP reports )========================"
