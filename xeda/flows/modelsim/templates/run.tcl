puts "\n===========================( Compiling HDL Sources )==========================="
{% for src in (design.rtl.sources + design.tb.sources) if src.type %}
{% if src.type == 'verilog' %}
vlog {{src.file}} {% if src.variant == "systemverilog" -%} -sv {%- endif -%} {{vlog_opts}}
{% elif src.type == 'vhdl' %}
vcom {{src.file}} {{vcom_opts}} {%- if design.language.vhdl.standard == "08" %} -2008 {% elif design.language.vhdl.standard == "02" %} -2002 {% elif design.language.vhdl.standard == "93" %} -93 {% endif -%}
{% endif %}
{% endfor %}

puts "\n===========================( Running simulation )==========================="

{% if vcd %}
vcd file {{vcd}}
{% endif %}

puts "\n===========================( *ENABLE ECHO* )==========================="
vsim -t ps {{design.tb.top}} {{vsim_opts}} {{generics_options}}
vcd add -r {% if design.tb.uut %} {{design.tb.uut}}/* {% else %} * {% endif %}
#run_wave
run {% if 'stop_time' in flow %} {{flow.stop_time}} {% else %} -all {% endif %}
puts "\n===========================( *DISABLE ECHO* )==========================="

{% if vcd %}
vcd flush
{% endif %}
