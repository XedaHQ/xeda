puts "\n===========================( Compiling HDL Sources )==========================="
{% for src in design.sources if src.type %}
{% if src.type == 'verilog' %}
vlog {{src.file}} {{vlog_opts}}
{% elif src.type == 'vhdl' %}
vcom {{src.file}} {{vcom_opts}} {%- if design.vhdl_std == "08" %} -2008 {% elif design.vhdl_std == "02" %} -2002 {% elif design.vhdl_std == "93" %} -93 {% endif -%}
{% endif %}
{% endfor %}

puts "\n===========================( Running simulation )==========================="

{% if vcd %}
vcd file {{vcd}}
{% endif %}

puts "\n===========================( *ENABLE ECHO* )==========================="
vsim -t ps {{design.tb_top}} {{generics_options}}
vcd add -r *
#run_wave
run {% if 'stop_time' in flow %} {{flow.stop_time}} {% else %} -all {% endif %}
puts "\n===========================( *DISABLE ECHO* )==========================="

{% if vcd %}
vcd flush
{% endif %}

exit