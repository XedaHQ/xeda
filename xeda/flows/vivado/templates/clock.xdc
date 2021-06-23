{% if design.rtl.clock_port -%}
create_clock -period {{settings.clock_period|round(3,'floor')}} -name clock [get_ports {{design.rtl.clock_port}}]
{% if settings.constrain_io %}
set_input_delay  -clock clock {{settings.input_delay}} [filter [all_inputs] {NAME != {{design.rtl.clock_port}} } ]
set_output_delay -clock clock {{settings.output_delay}} [all_outputs]
{% endif %}
{%- endif %}

set_units -power mW