proc errorExit {errorString} {
  puts "\n===========================( *ENABLE ECHO* )==========================="
  puts "Error: $errorString"
  exit 1
}

set design_name    {{design.name}}
set snapshot_name  {{design.tb.primary_top}}

load_feature simulator

set xelab_flags "-log xelab.log"
{% if design.language.vhdl.standard == "93" %}
append xelab_flags " -93_mode"
{% endif %}

if { [catch {file delete -force xsim.dir} error]} {
    puts "Failed to delete previously existing xsim.dir: $error"
}

set analyze_flags "-work {{lib_name}} {%- if settings.debug %} -verbose 2 {%- endif %} {{settings.analyze_flags|join(' ')}}"

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

{% for rc in multirun_configs %}

puts "\n===========================( Elaborating design )==========================="
if { [catch {eval exec xelab -s ${snapshot_name} -L {{lib_name}} {{settings.elab_flags|join(' ')}} ${xelab_flags} {%- for k,v in rc.generics.items() %} {{"-generic_top %s=%s"|format(k,v)}} {%- endfor %} {%- for top in design.sim_tops %} {{lib_name}}.{{top}} {% endfor -%}  } error]} {
    errorExit $error
}

puts "\n===========================( Loading Simulation )==========================="
if { [catch {eval xsim ${snapshot_name} {{settings.sim_flags|join(' ')}} } error] } {
    errorExit $error
}

{% if rc.saif %}
puts "\n===========================( Setting up SAIF )==========================="
if {[file exists {{rc.saif}}]} {
    puts "deleting existing SAIF file {{rc.saif}}"
    file delete -force -- {{rc.saif}}
}
open_saif {{rc.saif}}
{% endif %}

## TODO: WDB support
## set wdb_file "xsim_waves"
## open_wave_database ${wdb_file}

{%- if rc.vcd %}
puts "\n===========================( Setting up VCD )==========================="
open_vcd {{rc.vcd}}
## Vivado (tested on 2020.1) crashes if using * and shared/protected variables are present
log_vcd [get_objects -r -filter { type == variable || type == signal || type == internal_signal || type == in_port || type == out_port || type == inout_port || type == port } *]
{% endif -%}

{%- if settings.debug_traces %}
ltrace on
ptrace on
{% endif -%}

puts "\n===========================( Running simulation )==========================="
puts "\n===========================( *ENABLE ECHO* )==========================="
{% if settings.prerun_time %}
puts "Pre-run for {{settings.prerun_time}}"
if { [catch {eval run {{settings.prerun_time}} } error]} {
    errorExit $error
}
{% endif -%}

{%- if rc.saif %}
puts "Adding nets to be logged in SAIF"

log_saif [get_objects -r -filter { type == signal || type == internal_signal || type == in_port || type == out_port || type == inout_port || type == port } /{{design.tb.primary_top}}/{{design.tb.uut}}/*]
{% endif -%}


puts "Main Run\n"

if { [catch {eval run {% if settings.stop_time %} {{settings.stop_time}} {% else %} all {% endif %} } error]} {
    errorExit $error
}

set fin_time [eval current_time]

puts "\[Vivado\] Simulation finished at ${fin_time}"

puts "\n===========================( *DISABLE ECHO* )==========================="
{% if rc.vcd %}
puts "\n===========================( Closing VCD file )==========================="
flush_vcd
close_vcd
{% endif -%}

{%- if rc.saif %}
puts "\n===========================( Closing SAIF file )==========================="
close_saif
{% endif %}

{% endfor %}
