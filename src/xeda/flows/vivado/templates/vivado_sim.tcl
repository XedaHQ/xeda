proc errorExit {errorString} {
  puts "\n===========================( *ENABLE ECHO* )==========================="
  puts "Error: $errorString"
  exit 1
}

set design_name    {{design.name}}
set snapshot_name  snapshot

load_feature simulator

if { [catch {file delete -force xsim.dir} error]} {
    puts "Failed to delete previously existing xsim.dir: $error"
}

set analyze_flags "-work {{settings.work_lib}} {%- if settings.debug %} -verbose 2 {%- endif %} {{settings.analyze_flags|join(' ')}}"

puts "\n===========================( Analyzing HDL Sources )==========================="
{%- for src in design.sim_sources %}
{%- if src.type.name == "Verilog" %}
puts "Analyzing Verilog file {{src.file}}"
if { [catch {eval exec xvlog ${analyze_flags} {{src.file}} } error]} {
    errorExit $error
}
{%- elif src.type.name == "SystemVerilog" %}
puts "Analyzing SystemVerilog file {{src.file}}"
if { [catch {eval exec xvlog ${analyze_flags} -sv {{src.file}} } error]} {
    errorExit $error
}
{%- elif src.type.name == "Vhdl" %}
puts "Analyzing VHDL file {{src.file}} VHDL Standard: {{design.language.vhdl.standard}}"
if { [catch {eval exec xvhdl ${analyze_flags} {% if design.language.vhdl.standard == "08" %} -2008 {% elif design.language.vhdl.standard == "93" %} -93_mode {% endif %} {{src.file}} } error]} {
    errorExit $error
}
{%- endif %}
{%- endfor %}

puts "\n===========================( Elaborating design )==========================="
if { [catch {eval exec xelab -s ${snapshot_name} -L {{settings.work_lib}} {%- for l,_ in settings.lib_paths %} -L {{l}} {%- endfor %} {{settings.elab_flags|join(' ')}} {{settings.optimization_flags|join(' ')}} {% if settings.xelab_log %} -log {{settings.xelab_log}} {%- endif %} {%- for k,v in design.tb.parameters.items() %} {{"-generic_top %s=%s"|format(k,v)}} {%- endfor %} {%- for top in design.tb.top %} {{top}} {%- endfor %}  } error]} {
    errorExit $error
}

puts "\n===========================( Loading Simulation )==========================="
if { [catch {eval xsim ${snapshot_name} {{settings.sim_flags|join(' ')}} } error] } {
    errorExit $error
}

{%- if settings.saif %}
puts "\n===========================( Setting up SAIF )==========================="
if {[file exists {{settings.saif}}]} {
    puts "deleting existing SAIF file {{settings.saif}}"
    file delete -force -- {{settings.saif}}
}
open_saif {{settings.saif}}
{%- endif %}

{#- ## TODO: WDB support: open_wave_database ${wdb_file} #}

{%- if settings.vcd %}
puts "\n===========================( Setting up VCD )==========================="
open_vcd {{settings.vcd}}
## Vivado (tested on 2020.1) crashes if using * and shared/protected variables are present
## log_vcd [get_objects -r -filter { type == variable || type == signal || type == internal_signal || type == in_port || type == out_port || type == inout_port || type == port } /*]
log_vcd {%- if settings.quiet %} -quiet {%- elif settings.verbose %} -verbose {%- endif %} {%- if settings.vcd_level %} -level {{settings.vcd_level}} {%- endif %} {{settings.vcd_scope}}
{%- endif %}
{%- if settings.debug_traces %}
ltrace on
ptrace on
{%- endif %}

puts "\n===========================( Running simulation )==========================="
{%- if settings.prerun_time %}
puts "Pre-run for {{settings.prerun_time}}"
if { [catch {eval run {{settings.prerun_time}} } error]} {
    errorExit $error
}
{%- endif %}

{%- if settings.saif %}
puts "Adding nets to be logged in SAIF"
set netlist_scope ./{{design.tb.uut}}
eval describe ${netlist_scope}
log_saif [get_objects -r -filter { type == signal || type == internal_signal || type == in_port || type == out_port || type == inout_port || type == port } ${netlist_scope}/*]
{%- endif %}

if { [catch {eval run {%- if settings.stop_time %} {{settings.stop_time}} {%- else %} all {%- endif %} } error]} {
    errorExit $error
}

puts "Vivado simulation finished at [eval current_time]"

{%- if settings.vcd %}
puts "\n===========================( Closing VCD file )==========================="
flush_vcd
close_vcd
{%- endif %}

{%- if settings.saif %}
puts "\n===========================( Closing SAIF file )==========================="
close_saif
{%- endif %}
