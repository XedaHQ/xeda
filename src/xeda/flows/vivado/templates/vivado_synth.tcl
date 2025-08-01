set_param tclapp.enableGitAccess 0

set design_name {{design.name}}
set project_name ${design_name}
set fpga_part {{settings.fpga.part}}

create_project -part $fpga_part -force -verbose ${project_name}

{%- if settings.nthreads is not none %}
set_param general.maxThreads {{settings.nthreads}}
{%- endif %}
{%- for msg in settings.suppress_msgs %}
set_msg_config -id "\[{{msg}}\]" -suppress
{%- endfor %}

puts "\n=====================( Read Design Files and Constraints )======================"
{%- for src in design.rtl.sources %}
{%- if src.type.name == "Verilog" %}
puts "Reading Verilog file {{src}}"
if { [catch {read_verilog "{{src}}"} myError]} {
  errorExit $myError
}
{%- elif src.type.name == "SystemVerilog" %}
puts "Reading SystemVerilog file {{src}}"
if { [catch {read_verilog -sv "{{src}}"} myError]} {
  errorExit $myError
}
{%- elif src.type.name == "Vhdl" %}
puts "Reading VHDL file {{src}}"
if { [catch {read_vhdl {% if design.language.vhdl.standard in ("08", "2008") -%} -vhdl2008 {% endif -%} "{{src}}"} myError]} {
  errorExit $myError
}
{%- elif src.type.name == "MemoryFile" %}
puts "Adding MemoryFile file {{src}}"
add_files -fileset sources_1 -norecurse {{src}}
set_property -name "file_type" -value "Memory File" -objects [get_files {{src}}]
{%- elif src.type.name == "Xdc" %}
# puts "Reading XDC file {{src}}"
# source -verbose {{src}}
{%- elif src.type.name == "Tcl" %}
puts "Reading TCL file {{src}}"
source -verbose {{src}}
{%- else %}
puts "Adding source file with unknown type: {{src}}"
add_files -fileset sources_1 -norecurse {{src}}
{%- endif %}
{%- endfor %}

{% if design.rtl.top is not none -%}
puts "==================( Setting Top Module to {{design.rtl.top}} )========================================"
set_property top {{design.rtl.top}} [get_fileset sources_1]
{% endif -%}


{%- for file in tcl_files %}
puts "====================( Adding TCL file {{file}} )======================================"
add_files -fileset utils_1 -norecurse {{file}}
{%- endfor %}
{%- for file in xdc_files %}
puts "====================( Adding constraints file {{file}} )======================================"
add_files -fileset constrs_1 -norecurse {{file}}
# read_xdc {{file}}
{%- endfor %}

{%- if settings.show_available_strategies %}
set avail_synth_strategies [join [list_property_value strategy [get_runs synth_1] ] " "]
puts "====================( Available synthesis strategies: $avail_synth_strategies )===================="
set avail_impl_strategies [join [list_property_value strategy [get_runs impl_1] ] " "]
puts "====================( Available implementation strategies: $avail_impl_strategies )====================\n"
{%- endif %}

{%- if settings.synth.strategy %}
puts "====================( Using {{settings.synth.strategy}} strategy for synthesis )===================="
set_property strategy {{settings.synth.strategy}} [get_runs synth_1]
{%- endif %}

{%- if settings.impl.strategy %}
puts "====================( Using {{settings.impl.strategy}} strategy for implementation )===================="
set_property strategy {{settings.impl.strategy}} [get_runs impl_1]
{%- endif %}

{%- if generics %}
set_property generic {% raw -%} { {%- endraw -%} {{generics | join(" ")}} {%- raw -%} } {%- endraw %} [current_fileset]
{%- endif %}

{# see https://www.xilinx.com/support/documentation/sw_manuals/xilinx2022_1/ug912-vivado-properties.pdf #}
{# and https://www.xilinx.com/support/documentation/sw_manuals/xilinx2022_1/ug835-vivado-tcl-commands.pdf #}
{%- for run,run_name in [(settings.synth, "synth_1"), (settings.impl, "impl_1")] %}
{%- for step,options in run.steps.items() %}
{%- for name,value in options.items() %}
{% if value is mapping %}
{%- for k,v in value.items() %}
{% if v is mapping %}
{%- for kk,vv in v.items() %}
{%- if vv is iterable and (vv is not string) %}
{%- set vv = vv | join(" ") %}
{%- endif %}
set_property -name {{"{"}}STEPS.{{step}}.{{name}}.{{k}} {{kk}}{{"}"}} -value {{"{"}}{{vv}}{{"}"}} -objects [get_runs {{run_name}}]
{%- endfor %}
{%- else %}
{% if v is iterable and (v is not string) %}
{%- set v = v | join(" ") %}
{%- endif %}
set_property -name {{"{"}}STEPS.{{step}}.{{name}}.{{k}}{{"}"}} -value {{"{"}}{{v}}{{"}"}} -objects [get_runs {{run_name}}]
{%- endif %}
{%- endfor %}
{%- else %}
set_property -name {{"{"}}STEPS.{{step}}.{{name}}{{"}"}} -value {{"{"}}{{value}}{{"}"}} -objects [get_runs {{run_name}}]
{%- endif %}
{%- endfor %}
{%- endfor %}
{%- endfor %}

# puts "\n====================( set_synth_properties )=============================="
{% for k,v in settings.set_synth_properties.items() -%}
set_property {{k}} { {{-v-}} } [get_runs synth_1]
{% endfor -%}
# puts "\n====================( set_impl_properties )=============================="
{% for k,v in settings.set_impl_properties.items() -%}
set_property { {{-k-}} } { {{-v-}} } [get_runs impl_1]
{% endfor -%}

# puts "\n====================( reset_run )=============================="

reset_run synth_1

# puts "\n====================( Elaborating Design )=============================="
# synth_design -rtl -rtl_skip_mlo -name rtl_1


unset design_name
unset project_name
unset fpga_part



puts "\n=============================( Running Synthesis )=============================="
reset_run synth_1
launch_runs synth_1 {% if settings.nthreads %} -jobs {{settings.nthreads}} {%- endif %}
wait_on_run synth_1 {# <-- renamed to wait_on_runs in Vivado 2021.2 #}
puts "\n===========================( Running Implementation )==========================="
reset_run impl_1
launch_runs impl_1 {%-if settings.nthreads %} -jobs {{settings.nthreads}} {%- endif %} {% if settings.bitstream is none %} -to_step route_design {%- endif %}
wait_on_run impl_1
puts "\n====================================( DONE )===================================="
