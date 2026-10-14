{% set all_clock_ports = settings.clocks.values()|map(attribute="port") %}

set all_input_ports [all_inputs]
set all_output_ports [all_outputs]
set all_clock_ports {{ all_clock_ports|list|tcl_list }}

{% for clock_name, clock in settings.clocks.items() %}
{% if clock.port %}
set clk_port [get_ports {{ clock.port|tcl_word }}]
create_clock -period {{ clock.period|lib_time }} -name {{ clock_name|tcl_word }} $clk_port

## NOTE: includes other clock ports if multiple clocks are present
set non_clock_inputs [lsearch -inline -all -not -exact $all_input_ports $clk_port]

{# A delay in picoseconds is divided by 1000, since `lib_time` takes nanoseconds. #}
{% if settings.input_delay is not none %}
set_input_delay {{ (settings.input_delay / 1000)|lib_time }} -clock {{ clock_name|tcl_word }} $non_clock_inputs
{% else %}
set_input_delay {{ (settings.input_delay_frac * clock.period)|lib_time }} -clock {{ clock_name|tcl_word }} $non_clock_inputs
{% endif %}
{% if settings.output_delay is not none %}
set_output_delay {{ (settings.output_delay / 1000)|lib_time }} -clock {{ clock_name|tcl_word }} $all_output_ports
{% else %}
set_output_delay {{ (settings.output_delay_frac * clock.period)|lib_time }} -clock {{ clock_name|tcl_word }} $all_output_ports
{% endif %}

{% endif %}
{% endfor %}
