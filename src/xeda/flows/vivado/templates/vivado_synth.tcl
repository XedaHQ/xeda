set_param tclapp.enableGitAccess 0

{% include 'util.tcl' %}

set design_name {{design.name|tcl_word}}
set project_name ${design_name}
set fpga_part {{settings.fpga.part|tcl_word}}

create_project -part $fpga_part -force -verbose ${project_name}

{%- if settings.nthreads is not none %}
set_param general.maxThreads {{settings.nthreads}}
{%- endif %}
{%- for msg in settings.suppress_msgs %}
set_msg_config -id {{("[" ~ msg ~ "]")|tcl_word}} -suppress
{%- endfor %}

puts "\n=====================( Read Design Files and Constraints )======================"
{%- for src in design.rtl.sources %}
{%- if src.type.name == "Verilog" %}
puts "Reading Verilog file {{src|tcl_quote}}"
if { [catch {read_verilog {{src|tcl_list}}} myError]} {
  errorExit $myError
}
{%- elif src.type.name == "SystemVerilog" %}
puts "Reading SystemVerilog file {{src|tcl_quote}}"
if { [catch {read_verilog -sv {{src|tcl_list}}} myError]} {
  errorExit $myError
}
{%- elif src.type.name == "Vhdl" %}
puts "Reading VHDL file {{src|tcl_quote}}"
if { [catch {read_vhdl {% if design.language.vhdl.standard in ("08", "2008") -%} -vhdl2008 {% endif -%} {{src|tcl_list}}} myError]} {
  errorExit $myError
}
{%- elif src.type.name == "MemoryFile" %}
puts "Adding MemoryFile file {{src|tcl_quote}}"
add_files -fileset sources_1 -norecurse {{src|tcl_list}}
set_property -name "file_type" -value "Memory File" -objects [get_files {{src|tcl_list}}]
{%- elif src.type.name == "Xdc" %}
# puts "Reading XDC file {{src}}"
# source -verbose {{src}}
{%- elif src.type.name == "Tcl" %}
puts "Reading TCL file {{src|tcl_quote}}"
source -verbose {{src|tcl_word}}
{%- else %}
puts "Adding source file with unknown type: {{src|tcl_quote}}"
add_files -fileset sources_1 -norecurse {{src|tcl_list}}
{%- endif %}
{%- endfor %}

{% if design.rtl.top is not none -%}
puts "==================( Setting Top Module to {{design.rtl.top|tcl_quote}} )========================================"
set_property top {{design.rtl.top|tcl_word}} [get_fileset sources_1]
{% endif -%}


{%- for file in tcl_files %}
puts "====================( Adding TCL file {{file|tcl_quote}} )======================================"
add_files -fileset utils_1 -norecurse {{file|tcl_list}}
{%- endfor %}
{%- for file in xdc_files %}
puts "====================( Adding constraints file {{file|tcl_quote}} )======================================"
add_files -fileset constrs_1 -norecurse {{file|tcl_list}}
# read_xdc {{file}}
{%- endfor %}

{%- if settings.show_available_strategies %}
set avail_synth_strategies [join [list_property_value strategy [get_runs synth_1] ] " "]
puts "====================( Available synthesis strategies: $avail_synth_strategies )===================="
set avail_impl_strategies [join [list_property_value strategy [get_runs impl_1] ] " "]
puts "====================( Available implementation strategies: $avail_impl_strategies )====================\n"
{%- endif %}

{%- if settings.synth.strategy %}
puts "====================( Using {{settings.synth.strategy|tcl_quote}} strategy for synthesis )===================="
set_property strategy {{settings.synth.strategy|tcl_word}} [get_runs synth_1]
{%- endif %}

{%- if settings.impl.strategy %}
puts "====================( Using {{settings.impl.strategy|tcl_quote}} strategy for implementation )===================="
set_property strategy {{settings.impl.strategy|tcl_word}} [get_runs impl_1]
{%- endif %}

{%- if generics %}
set_property generic {{generics|join(" ")|tcl_word}} [current_fileset]
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
set_property -name {{("STEPS." ~ step ~ "." ~ name ~ "." ~ k ~ " " ~ kk)|tcl_word}} -value {{vv|tcl_word}} -objects [get_runs {{run_name}}]
{%- endfor %}
{%- else %}
{% if v is iterable and (v is not string) %}
{%- set v = v | join(" ") %}
{%- endif %}
set_property -name {{("STEPS." ~ step ~ "." ~ name ~ "." ~ k)|tcl_word}} -value {{v|tcl_word}} -objects [get_runs {{run_name}}]
{%- endif %}
{%- endfor %}
{%- else %}
set_property -name {{("STEPS." ~ step ~ "." ~ name)|tcl_word}} -value {{value|tcl_word}} -objects [get_runs {{run_name}}]
{%- endif %}
{%- endfor %}
{%- endfor %}
{%- endfor %}

# puts "\n====================( set_synth_properties )=============================="
{% for k,v in settings.set_synth_properties.items() -%}
set_property {{k|tcl_word}} {{tcl_property_value(v)|tcl_word}} [get_runs synth_1]
{% endfor -%}
# puts "\n====================( set_impl_properties )=============================="
{% for k,v in settings.set_impl_properties.items() -%}
set_property {{k|tcl_word}} {{tcl_property_value(v)|tcl_word}} [get_runs impl_1]
{% endfor -%}

# puts "\n====================( reset_run )=============================="

reset_run synth_1

# puts "\n====================( Elaborating Design )=============================="
# synth_design -rtl -rtl_skip_mlo -name rtl_1

unset design_name
unset project_name
unset fpga_part

{#- What became of a run is in its properties alone: `wait_on_run` returns normally for a failed
    run in Vivado 2021.1, and raises an error in 2024.2. So the script waits for the run either
    way, then asks whether it completed the step it was launched to -- its STATUS is
    "<step> Complete!" (not "<step> ERROR", "Not started", ...) and its PROGRESS 100% -- and
    records the status for the results (`status`). A run that did not complete its step ends the
    script with one message naming the run, its status and its log. #}
proc xedaWaitOnRun {run step} {
  catch {wait_on_run $run} {# <-- renamed to wait_on_runs in Vivado 2021.2 #}
  set status [get_property STATUS [get_runs $run]]
  set progress [get_property PROGRESS [get_runs $run]]
  set status_file [open {{run_status_file|tcl_word}} w]
  puts $status_file $status
  close $status_file
  if {$status ne "$step Complete!" || $progress ne "100%"} {
    set log [file join [get_property DIRECTORY [get_runs $run]] runme.log]
    puts "\n=========( ERROR: The Vivado run $run did not complete $step: its status is\
          \"$status\", its progress $progress. See its log, $log )=========="
    exit 1
  }
}

puts "\n=============================( Running Synthesis )=============================="
reset_run synth_1
launch_runs synth_1 {% if settings.nthreads %} -jobs {{settings.nthreads}} {%- endif %}
xedaWaitOnRun synth_1 synth_design

puts "\n===========================( Running Implementation )==========================="
reset_run impl_1
launch_runs impl_1 {%-if settings.nthreads %} -jobs {{settings.nthreads}} {%- endif %} -to_step {{impl_to_step}}
xedaWaitOnRun impl_1 {{impl_to_step}}
puts "\n====================================( DONE )===================================="
