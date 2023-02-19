{% for clock_name, clock in settings.clocks.items() -%}
{% if clock.port -%}
create_clock -period {{"%.03f" % clock.period}} -name {{clock_name}} [get_ports {{clock.port}}]
{% if settings.input_delay  is not none -%} set_input_delay  -clock {{clock_name}} {{settings.input_delay}} [filter [all_inputs] {NAME != {{clock.port}} } ] {%- endif %}
{% if settings.output_delay is not none -%} set_output_delay -clock {{clock_name}} {{settings.output_delay}} [all_outputs] {%- endif %}
{% endif -%}
{% endfor -%}

set_units -power mW
