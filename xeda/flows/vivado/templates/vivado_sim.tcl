set post_synth_sim false

proc errorExit {errorString} {
  puts "\n===========================( *ENABLE ECHO* )==========================="
  puts "Error: $errorString"
  exit 1
}

set design_name           {{design.name}}
set tb_top                {{design.tb_top}}
set results_dir           results
# set vhdl_funcsim          ${results_dir}/${top}_impl_funcsim.vhd
# set verilog_funcsim       ${results_dir}/${top}_impl_funcsim.v
# set verilog_timesim       ${results_dir}/${top}_impl_timesim.v
# set sdf_file              "[file rootname ${verilog_timesim}].sdf"
set timing_sim            false
set funcsim_use_vhdl      true

set uut_scope             /${tb_top}/{{design.tb_uut}}


set wdb_file "${results_dir}/xsim_${tb_top}_dump"


#FIXME broken
set xsim_lib_name work

# -sdfmax /LWC_TB/uut=${sdf_file}
# open_wave_database ${wdb_file}

set snapshot_name "${tb_top}"


set xelab_flags "{{elab_flags}} -incr -s ${snapshot_name} -L ${xsim_lib_name} -log xelab.log  {{generics_options}}"


{%if debug or saif or vcd %}
append xelab_flags " -debug typical "
{% endif %}

{% if debug %}
append xelab_flags " -O0 "
{% else %}
append xelab_flags " -O3 -mt {{nthreads}} "
{% endif %}

if { [catch {file delete -force xsim.dir} myError]} {
    puts "Failed to delete previously existing xsim.dir: $myError"
}



set analyze_flags " -incr -work ${xsim_lib_name} {%- if debug %} -verbose 2 {%- endif %} {{analyze_flags}}"

set designs "${xsim_lib_name}.${tb_top}"

if {${post_synth_sim}} {

    append designs " glbl"

    if {${timing_sim}} {
        append xelab_flags " -maxdelay -transport_int_delays -pulse_r 0 -pulse_int_r 0 "
    }
}


puts "\n===========================( Analyzing HDL Sources )==========================="
{% for src in design.sources %}
{% if src.type == 'verilog' %}

{% if src.variant == 'systemverilog' %}
puts "Analyzing SystemVerilog file {{src.file}}"
if { [catch {eval exec xvlog ${analyze_flags} -sv {{src.file}} } myError]} {
    errorExit $myError
}
{% else %}
puts "Analyzing Verilog file {{src.file}}"
if { [catch {eval exec xvlog ${analyze_flags} {{src.file}} } myError]} {
    errorExit $myError
}
{% endif %}

{% endif %}
{% if src.type == 'vhdl' %}
puts "Analyzing VHDL file {{src.file}} VHDL Standard: {{design.vhdl_std}}"
if { [catch {eval  exec xvhdl ${analyze_flags} {% if design.vhdl_std == "08" %} -2008 {% elif design.vhdl_std == "93" %} -93_mode {% endif %} {{src.file}} } myError]} {
    errorExit $myError
}
{% endif %}
{% endfor %}


puts "\n===========================( Elaborating design: ${tb_top} )==========================="
eval exec xelab ${xelab_flags} {% if design.vhdl_std == "93"%} -93_mode {% endif %} ${designs} 

# eval exec xelab -incr -debug typical -relax -mt 8 -maxdelay -L xil_defaultlib -L simprims_ver -L secureip -s ${snapshot_name} -transport_int_delays -pulse_r 0 -pulse_int_r 0 -pulse_e 0 -pulse_int_e 0 ${xsim_lib_name}.LWC_TB -generic "G_PERIOD=${clock_period}ns" ${xsim_lib_name}.glbl -log elaborate.log


puts "\n===========================( Loading Simulation )==========================="
eval xsim ${snapshot_name} {{sim_flags}}


{% if saif %}
file delete {{saif}}
open_saif {{saif}}
log_saif  [get_objects -r ${uut_scope}/*]
{% endif %}

{% if vcd %}
open_vcd {{vcd}}
log_vcd *
{% endif %}
#
# puts "[get_objects -recursive -filter {type =~ signal} /LWC_TB/*]"
# set signals [get_objects -filter {TYPE == signal} /LWC_TB/*]
# puts "${objs}"
{% if initialize_zeros %}}
puts "setting initial values to 0"
foreach sig [get_objects -recursive -filter {TYPE =~ *signal} ${uut_scope}/*] {
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

{% if debug_traces %}
ltrace on
ptrace on
{% endif %}

puts "\n===========================( Running simulation )==========================="
puts "\n===========================( *ENABLE ECHO* )==========================="
run {% if 'stop_time' in flow %} {{flow.stop_time}} {% else %} all {% endif %}
puts "\n===========================( *DISABLE ECHO* )==========================="


# if debug
{% if vcd %}
puts "\n===========================( Saving VCD to  {{vcd}} )==========================="
flush_vcd
close_vcd
{% endif %}

{% if saif %}
puts "\n===========================( Saving SAIF file )==========================="
close_saif
{% endif %}


