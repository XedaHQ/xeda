{% for clock_name, clock in settings.clocks.items() -%}
{% if clock.port -%}
create_clock -period {{"%.03f" % clock.period}} -name {{clock_name}} [get_ports {{clock.port}}]
{% if settings.default_max_input_delay is not none -%} set_input_delay -max -clock {{clock_name}} {{settings.default_max_input_delay}} [filter [all_inputs] {NAME != {{clock.port}} } ] {%- endif %}
{% if settings.default_min_input_delay is not none -%} set_input_delay -min -clock {{clock_name}} {{settings.default_min_input_delay}} [filter [all_inputs] {NAME != {{clock.port}} } ] {%- endif %}
{% if settings.default_max_output_delay is not none -%} set_output_delay -max -clock {{clock_name}} {{settings.default_max_output_delay}} [all_outputs] {%- endif %}
{% if settings.default_min_output_delay is not none -%} set_output_delay -min -clock {{clock_name}} {{settings.default_min_output_delay}} [all_outputs] {%- endif %}
{% endif -%}
{% endfor -%}

set_units -power mW
