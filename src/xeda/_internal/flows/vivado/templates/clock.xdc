{% for clock_name, clock in settings.clocks.items() -%}
{% if clock.port -%}
create_clock -period {{"%.03f" % (clock.period | to("ns"))}} -name {{clock_name|tcl_word}} [get_ports {{clock.port|tcl_word}}]
{% if settings.default_max_input_delay is not none -%} set_input_delay -max -clock {{clock_name|tcl_word}} {{settings.default_max_input_delay|to("ns")}} [filter [all_inputs] {{("NAME != " ~ clock.port)|tcl_word}}] {%- endif %}
{% if settings.default_min_input_delay is not none -%} set_input_delay -min -clock {{clock_name|tcl_word}} {{settings.default_min_input_delay|to("ns")}} [filter [all_inputs] {{("NAME != " ~ clock.port)|tcl_word}}] {%- endif %}
{% if settings.default_max_output_delay is not none -%} set_output_delay -max -clock {{clock_name|tcl_word}} {{settings.default_max_output_delay|to("ns")}} [all_outputs] {%- endif %}
{% if settings.default_min_output_delay is not none -%} set_output_delay -min -clock {{clock_name|tcl_word}} {{settings.default_min_output_delay|to("ns")}} [all_outputs] {%- endif %}
{% endif -%}
{% endfor -%}
