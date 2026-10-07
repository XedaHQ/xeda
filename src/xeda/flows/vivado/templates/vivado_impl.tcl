set_param tclapp.enableGitAccess 0

set reports_dir {{settings.reports_dir|tcl_word}}
set fpga_part   {{settings.fpga.part|tcl_word}}

{% include 'util.tcl' %}

{%- if settings.nthreads is not none %}
set_param general.maxThreads {{settings.nthreads}}
{%- endif %}

file mkdir ${reports_dir}

{% for msg in settings.suppress_msgs -%}
set_msg_config -id {{("[" ~ msg ~ "]")|tcl_word}} -suppress
{% endfor %}
set_param tcl.collectionResultDisplayLimit 0

puts "\n================================( Read Constraints and Netlist )================================"
puts "Targeting device: $fpga_part"

{% for xdc_file in xdc_files -%}
puts "Reading XDC file {{xdc_file|tcl_quote}}"
read_xdc {{xdc_file|tcl_list}}
{% endfor %}
# Vivado finds the top module of an EDIF netlist by the name of its file, so the netlist is
# `<top>.edif` (`VivadoImpl.run`).
puts "Reading EDIF netlist {{netlist|tcl_quote}}"
if { [catch {read_edif {{netlist|tcl_list}}} myError]} {
    errorExit $myError
}
if { [catch {link_design -part $fpga_part -top {{design.rtl.top|tcl_word}}} myError]} {
    errorExit $myError
}
showWarningsAndErrors

puts "\n==============================( Optimize Design )================================"
opt_design
showWarningsAndErrors

{% include 'implementation.tcl' %}
