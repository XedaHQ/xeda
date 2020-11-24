proc errorExit {errorString} {
  puts "\n===========================( *ENABLE ECHO* )==========================="
  puts "Error: $errorString"
  exit 1
}

set design_name    {{design.name}}
set tb_top         {{tb_top}}
set results_dir    results
set snapshot_name  {{tb_top}}

set xelab_flags "-log xelab.log"
{% if design.language.vhdl.standard == "93" %}
append xelab_flags " -93_mode"
{% endif %}

if { [catch {file delete -force xsim.dir} error]} {
    puts "Failed to delete previously existing xsim.dir: $error"
}
set analyze_flags "-incr -work {{lib_name}} {%- if debug %} -verbose 2 {%- endif %} {{analyze_flags}}"

puts "\n===========================( Analyzing HDL Sources )==========================="
{% for src in sim_sources %}
{% if src.type == 'verilog' %}

{% if src.variant == 'systemverilog' %}
puts "Analyzing SystemVerilog file {{src.file}}"
if { [catch {eval exec xvlog ${analyze_flags} -sv {{src.file}} } error]} {
    errorExit $error
}
{% else %}
puts "Analyzing Verilog file {{src.file}}"
if { [catch {eval exec xvlog ${analyze_flags} {{src.file}} } error]} {
    errorExit $error
}
{% endif %}

{% endif %}
{% if src.type == 'vhdl' %}
puts "Analyzing VHDL file {{src.file}} VHDL Standard: {{design.language.vhdl.standard}}"
if { [catch {eval exec xvhdl ${analyze_flags} {% if design.language.vhdl.standard == "08" %} -2008 {% elif design.language.vhdl.standard == "93" %} -93_mode {% endif %} {{src.file}} } error]} {
    errorExit $error
}
{% endif %}
{% endfor %}

puts "\n===========================( Elaborating design )==========================="
if { [catch {eval exec xelab -incr -mt auto -s ${snapshot_name} -L {{lib_name}} {{elab_flags}} ${xelab_flags} {{generics_options}} {% for top in sim_tops -%} {{lib_name}}.{{top}} {% endfor -%}  } error]} {
    errorExit $error
}

puts "\n===========================( Loading Simulation )==========================="
if { [catch {eval xsim ${snapshot_name} {{sim_flags}} } error] } {
    errorExit $error
}

{% if saif %}
puts "\n===========================( Setting up SAIF dump )==========================="
if {[file exists {{saif}}]} {
    puts "deleting existing SAIF file {{saif}}"
    file delete -force -- {{saif}}
}
open_saif {{saif}}
{% endif %}

## TODO: WDB support
## set wdb_file "${results_dir}/xsim_${tb_top}_dump"
## open_wave_database ${wdb_file}
{%- if vcd %}
puts "\n===========================( Setting up VCD dump )==========================="
open_vcd {{vcd}}
log_vcd *
{% endif -%}

{% if initialize_zeros %}} ## not needed with Xilinx libs as they use the global reset mechanism (GSR)
#
# puts "[get_objects -recursive -filter {type =~ signal} /LWC_TB/*]"
# set signals [get_objects -filter {TYPE == signal} /LWC_TB/*]
# puts "${objs}"
puts "setting initial values to 0"
foreach sig [get_objects -recursive -filter {TYPE =~ *signal} /{{tb_top}}/{{design.tb.uut}}/*] {
    set_value ${sig} {0}
    # set dim [llength [split [get_property VALUE ${sig}] ,] ]
    # if {$dim == 1} {
    # # add_force ${sig} {0} -cancel_after 27
    # } else {
    # # for {set i 0} {$i < $dim} {incr i} {
    # #   set_value ${sig}[$i] {0}
    # #   # add_force ${sig}[$i] {0} -cancel_after 27
    # # }
    # }

}
{% endif %}

{%- if debug_traces %}
ltrace on
ptrace on
{% endif -%}

puts "\n===========================( Running simulation )==========================="
puts "\n===========================( *ENABLE ECHO* )==========================="
{% if prerun_time %}
if { [catch {eval run {{prerun_time}} } error]} {
    errorExit $error
}
{% endif -%}

{%- if saif %}
puts "Recursively adding all signals to the SAIF log list (this can take a very long time if: xelab 'debug' is set to 'full', Xilinx primitive libraries are inlined, or the design is very large)..."
## -filter { type == signal || type == in_port || type == out_port || type == inout_port || type == port }
log_saif [get_objects -r /{{tb_top}}/{{design.tb.uut}}/*]
puts " ...done!"
{% endif -%}

if { [catch {eval run {% if 'stop_time' in flow %} {{flow.stop_time}} {% else %} all {% endif %} } error]} {
    errorExit $error
}

puts "\n===========================( *DISABLE ECHO* )==========================="
{% if vcd %}
puts "\n===========================( Saving VCD to  {{vcd}} )==========================="
flush_vcd
close_vcd
{% endif -%}

{%- if saif %}
puts "\n===========================( Saving SAIF file )==========================="
close_saif
{% endif %}
