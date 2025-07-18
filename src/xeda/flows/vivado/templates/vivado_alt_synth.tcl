set_param tclapp.enableGitAccess 0

set fail_critical_warning {{settings.fail_critical_warning}}
set reports_dir           {{settings.reports_dir}}
set settings.outputs_dir  {{settings.outputs_dir}}
set checkpoints_dir       {{settings.checkpoints_dir}}
set fpga_part             {{settings.fpga.part}}

{% include 'util.tcl' %}

{%- if settings.nthreads is not none %}
set_param general.maxThreads {{settings.nthreads}}
{%- endif %}

file mkdir ${settings.outputs_dir}
file mkdir ${reports_dir}
file mkdir [file join ${reports_dir} post_synth]
file mkdir [file join ${reports_dir} post_place]
file mkdir ${checkpoints_dir}


{%- for msg in settings.suppress_msgs %}
set_msg_config -id "\[{{msg}}\]" -suppress
{%- endfor %}

set_param tcl.collectionResultDisplayLimit 0
set parts [get_parts]

puts "\n================================( Read Design Files and Constraints )================================"

if {[lsearch -exact $parts $fpga_part] < 0} {
    puts "ERROR: device $fpga_part is not supported!"
    puts "Supported devices:"
    puts [join $parts " "]
    quit
}

puts "Targeting device: $fpga_part"

{% for src in design.rtl.sources %}
{% if src.type.name == "Verilog" %}
puts "Reading Verilog file {{src.file}}"
if { [catch {eval read_verilog \"{{src.file}}\" } myError]} {
    errorExit $myError
}
{%- elif src.type.name == "SystemVerilog" %}
puts "Reading SystemVerilog file {{src.file}}"
if { [catch {eval read_verilog -sv \"{{src.file}}\" } myError]} {
    errorExit $myError
}
{%- elif src.type.name == "Vhdl" %}
puts "Reading VHDL file {{src.file}}"
if { [catch {eval read_vhdl {% if design.language.vhdl.standard in ("08", "2008") %} -vhdl2008 {%- endif %} \"{{src.file}}\" } myError]} {
    errorExit $myError
}
{%- endif %}
{%- endfor %}

# TODO: Skip saving some artifects in case timing not met or synthesis failed for any reason

{%- for xdc_file in xdc_files %}
puts "Reading XDC file {{xdc_file}}"
read_xdc {{xdc_file}}
{%- endfor %}

puts "\n===========================( RTL Synthesize and Map )==========================="
eval synth_design -part $fpga_part -top {{design.rtl.top}} {{settings.synth.steps.synth|flatten_options}} {{design.rtl.parameters|vivado_generics}} {{design.rtl.defines|vivado_defines}}

{%- if settings.synth.strategy == "Debug" %}
set_property KEEP_HIERARCHY true [get_cells -hier * ]
set_property DONT_TOUCH true [get_cells -hier * ]
{%- endif %}
showWarningsAndErrors


{% if settings.synth.steps.opt is not none %}
puts "\n==============================( Optimize Design )================================"
eval opt_design {{settings.synth.steps.opt|flatten_options}}
{%- endif %}

{% if settings.write_checkpoint %}
write_checkpoint -force ${checkpoints_dir}/post_synth
{%- endif %}
report_timing_summary -file ${reports_dir}/post_synth/timing_summary.rpt
report_utilization -hierarchical -force -file ${reports_dir}/post_synth/hierarchical_utilization.rpt
# reportCriticalPaths ${reports_dir}/post_synth/critpath_report.csv
# report_methodology  -file ${reports_dir}/post_synth/methodology.rpt

{# post-synth and post-place power optimization steps are mutually exclusive! #}
{# TODO: check this is still the case with the most recent versions of Vivado #}
{% if settings.synth.steps.power_opt and not settings.impl.steps.power_opt %}
puts "\n===============================( Post-synth Power Optimization )================================"
# this is more effective than Post-placement Power Optimization but can hurt timing
eval power_opt_design
report_power_opt -file ${reports_dir}/post_synth/power_optimization.rpt
showWarningsAndErrors
{%- endif %}

puts "\n================================( Place Design )================================="
eval place_design {{settings.impl.steps.place|flatten_options}}
showWarningsAndErrors


{% if settings.impl.steps.power_opt %}
puts "\n===============================( Post-placement Power Optimization )================================"
eval power_opt_design
report_power_opt -file ${reports_dir}/post_place/post_place_power_optimization.rpt
showWarningsAndErrors
{%- endif %}

{% if settings.impl.steps.place_opt is not none %}

puts "\n==============================( Post-place optimization )================================"
eval opt_design {{settings.impl.steps.place_opt|flatten_options}}

{% if settings.impl.steps.place_opt2 is not none %}
puts "\n==============================( Post-place optimization 2)================================"
eval opt_design {{settings.impl.steps.place_opt2|flatten_options}}
{%- endif %}

{%- endif %}


{% if settings.impl.steps.phys_opt is not none %}
puts "\n========================( Post-place Physical Optimization )=========================="
eval phys_opt_design {{settings.impl.steps.phys_opt|flatten_options}}

{% if settings.impl.steps.phys_opt is not none %}
puts "\n========================( Post-place Physical Optimization 2 )=========================="
eval phys_opt_design {{settings.impl.steps.phys_opt|flatten_options}}
{%- endif %}
{%- endif %}

{% if settings.write_checkpoint %}
write_checkpoint -force ${checkpoints_dir}/post_place
report_timing_summary -file ${reports_dir}/post_place/timing_summary.rpt
report_utilization -hierarchical -force -file ${reports_dir}/post_place/hierarchical_utilization.rpt
{%- endif %}

puts "\n================================( Route Design )================================="
eval route_design {{settings.impl.steps.route|flatten_options}}
showWarningsAndErrors

{% if settings.impl.steps.post_route_phys_opt is not none %}
puts "\n=========================( Post-Route Physical Optimization )=========================="
phys_opt_design {{settings.impl.steps.post_route_phys_opt|flatten_options}}
showWarningsAndErrors
{%- endif %}

{% if settings.write_checkpoint %}
puts "\n=============================( Writing Checkpoint )=============================="
write_checkpoint -force ${checkpoints_dir}/post_route
{%- endif %}

puts "\n==============================( Writing Reports )================================"
set rep_dir [file join ${reports_dir} route_design]
file mkdir ${rep_dir}

set timing_summary_file [file join ${rep_dir} timing_summary.rpt]
report_timing_summary -check_timing_verbose -no_header -report_unconstrained -path_type full -input_pins -max_paths 10 -delay_type min_max -file ${timing_summary_file}
report_timing         -no_header -input_pins  -unique_pins -sort_by group -max_paths 100 -path_type full -delay_type min_max -file [file join ${rep_dir} timing.rpt]
reportCriticalPaths                [file join ${rep_dir} critpath_report.csv]
report_utilization                 -file [file join ${rep_dir} utilization.rpt]
report_utilization                 -file [file join ${rep_dir} utilization.xml] -format xml
report_utilization -hierarchical   -file [file join ${rep_dir} hierarchical_utilization.xml] -format xml

{% if settings.extra_reports -%}
report_clock_utilization           -file [file join ${rep_dir} clock_utilization.rpt]
report_power                       -file [file join ${rep_dir} power.rpt]
report_drc                         -file [file join ${rep_dir} drc.rpt]
report_methodology                 -file [file join ${rep_dir} methodology.rpt]
{%- endif %}

{% if settings.qor_suggestions -%}
report_qor_suggestions             -file [file join ${rep_dir} qor_suggestions.rpt]
{%- endif %}

set timing_slack [get_property SLACK [get_timing_paths]]

if {[string is double -strict $timing_slack]} {
    puts "Final timing slack: $timing_slack ns"

    if {[string is double -strict $timing_slack] && ($timing_slack < 0)} {
        puts "ERROR: Failed to meet timing by $timing_slack, see ${timing_summary_file} for details"
        {% if settings.fail_timing %}
        exit 1
        {%- endif %}
    }
}

{% if settings.write_netlist -%}
puts "\n==========================( Writing Netlist and SDF )============================="
write_verilog -mode funcsim -force ${settings.outputs_dir}/impl_funcsim.v
write_sdf -mode timesim -process_corner slow -force -file ${settings.outputs_dir}/impl_timesim.sdf
# should match sdf
write_verilog -mode timesim -sdf_anno false -force -file ${settings.outputs_dir}/impl_timesim.v
##    write_vhdl    -mode funcsim -include_xilinx_libs -write_all_overrides -force -file ${settings.outputs_dir}/impl_funcsim_xlib.vhd
write_xdc -no_fixed_only -force ${settings.outputs_dir}/impl.xdc
{% endif -%}

{% if settings.bitstream -%}
puts "\n==============================( Writing Bitstream )==============================="
write_bitstream -force {{{settings.bitstream}}}
{% endif -%}

showWarningsAndErrors
puts "\n===========================( *DISABLE ECHO* )==========================="
