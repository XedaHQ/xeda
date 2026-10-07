set_param tclapp.enableGitAccess 0

set fail_critical_warning {{settings.fail_critical_warning}}
set reports_dir           {{settings.reports_dir|tcl_word}}
set settings.outputs_dir  {{settings.outputs_dir|tcl_word}}
set checkpoints_dir       {{settings.checkpoints_dir|tcl_word}}
set fpga_part             {{settings.fpga.part|tcl_word}}

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
set_msg_config -id {{("[" ~ msg ~ "]")|tcl_word}} -suppress
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

{% for src in sources_read() %}
{% if src.type.name == "Verilog" %}
puts "Reading Verilog file {{src.file|tcl_quote}}"
if { [catch {read_verilog {{src.file|tcl_list}}} myError]} {
    errorExit $myError
}
{%- elif src.type.name == "SystemVerilog" %}
puts "Reading SystemVerilog file {{src.file|tcl_quote}}"
if { [catch {read_verilog -sv {{src.file|tcl_list}}} myError]} {
    errorExit $myError
}
{%- elif src.type.name == "Vhdl" %}
puts "Reading VHDL file {{src.file|tcl_quote}}"
if { [catch {read_vhdl {% if design.language.vhdl.standard in ("08", "2008") %} -vhdl2008 {%- endif %} {{src.file|tcl_list}}} myError]} {
    errorExit $myError
}
{%- endif %}
{%- endfor %}

{%- if design.header_dirs() %}
set_property include_dirs {{design.header_dirs()|tcl_list}} [current_fileset]
{%- endif %}

# TODO: Skip saving some artifects in case timing not met or synthesis failed for any reason

{%- for xdc_file in xdc_files %}
puts "Reading XDC file {{xdc_file|tcl_quote}}"
read_xdc {{xdc_file|tcl_list}}
{%- endfor %}

puts "\n===========================( RTL Synthesize and Map )==========================="
synth_design -part $fpga_part -top {{design.rtl.top|tcl_word}} {{settings.synth.steps.synth|flatten_options}} {{design.rtl.parameters|vivado_generics}} {{design.rtl.defines|vivado_defines}}

{%- if settings.synth.strategy == "Debug" %}
set_property KEEP_HIERARCHY true [get_cells -hier * ]
set_property DONT_TOUCH true [get_cells -hier * ]
{%- endif %}
showWarningsAndErrors


{% if settings.synth.steps.opt is not none %}
puts "\n==============================( Optimize Design )================================"
opt_design {{settings.synth.steps.opt|flatten_options}}
{%- endif %}

{% if settings.write_checkpoint %}
write_checkpoint -force ${checkpoints_dir}/post_synth
{%- endif %}
report_timing_summary -file ${reports_dir}/post_synth/timing_summary.rpt
report_utilization -hierarchical -force -file ${reports_dir}/post_synth/hierarchical_utilization.rpt
# reportCriticalPaths ${reports_dir}/post_synth/critpath_report.csv 100
# report_methodology  -file ${reports_dir}/post_synth/methodology.rpt

{# post-synth and post-place power optimization steps are mutually exclusive! #}
{# TODO: check this is still the case with the most recent versions of Vivado #}
{% if settings.synth.steps.power_opt and not settings.impl.steps.power_opt %}
puts "\n===============================( Post-synth Power Optimization )================================"
# this is more effective than Post-placement Power Optimization but can hurt timing
power_opt_design
report_power_opt -file ${reports_dir}/post_synth/power_optimization.rpt
showWarningsAndErrors
{%- endif %}

{% include 'implementation.tcl' %}
