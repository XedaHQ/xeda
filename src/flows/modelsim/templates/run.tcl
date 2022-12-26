puts "\n===========================( Compiling HDL Sources )==========================="
{% for src in design.sim_sources if src.type -%}
{% if src.type == 'verilog' -%}
if { [catch {eval vlog {{src.file}} {% if src.variant == "systemverilog" -%} -sv {%- endif -%} {{vlog_opts}} } error]} {
    puts $error
    exit 1
}
{% elif src.type == 'vhdl' -%}
if { [catch {eval vcom {{src.file}} {{vcom_opts}} {%- if design.language.vhdl.standard == "08" %} -2008 {% elif design.language.vhdl.standard == "02" %} -2002 {% elif design.language.vhdl.standard == "93" %} -93 {% endif -%} } error]} {
    puts $error
    exit 1
}
{%- endif %}
{%- endfor %}

puts "\n===========================( Running simulation )==========================="

{% if settings.vcd %}
vcd file {{settings.vcd}}
{% endif %}

puts "\n===========================( *ENABLE ECHO* )==========================="
if { [catch {eval vsim -t ps {{design.sim_tops|join(' ')}} {{vsim_opts}} {{generics_options}} } error]} {
    puts $error
    exit 1
}
vcd add -r {% if not debug and design.tb.uut %} {{design.tb.uut}}/* {% else %} * {% endif %}
#run_wave
run {% if 'stop_time' in flow %} {{settings.stop_time}} {%- else %} -all {%- endif %}
puts "\n===========================( *DISABLE ECHO* )==========================="

{% if settings.vcd %}
vcd flush
{% endif %}

exit
