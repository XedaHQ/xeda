{% set all_clock_ports = settings.clocks.values()|map(attribute="port")|select|list %}

set all_input_ports [all_inputs]
set all_output_ports [all_outputs]
set all_clock_ports {{ all_clock_ports|tcl_list }}

# The data inputs are the inputs without the port of any clock.
set non_clock_inputs $all_input_ports
foreach clock_port_name $all_clock_ports {
    set non_clock_inputs [lsearch -inline -all -not -exact $non_clock_inputs [get_ports $clock_port_name]]
}

{% for clock_name, clock in settings.clocks.items() %}
{% if clock.port %}
set clk_port [get_ports {{ clock.port|tcl_word }}]
create_clock -period {{ clock.period|lib_time }} -name {{ clock_name|tcl_word }} $clk_port

{% if settings.input_delay is not none %}
set_input_delay {{ settings.input_delay|lib_time }} -clock {{ clock_name|tcl_word }} $non_clock_inputs
{% else %}
set_input_delay {{ fraction_of(clock.period, settings.input_delay_frac)|lib_time }} -clock {{ clock_name|tcl_word }} $non_clock_inputs
{% endif %}
{% if settings.output_delay is not none %}
set_output_delay {{ settings.output_delay|lib_time }} -clock {{ clock_name|tcl_word }} $all_output_ports
{% else %}
set_output_delay {{ fraction_of(clock.period, settings.output_delay_frac)|lib_time }} -clock {{ clock_name|tcl_word }} $all_output_ports
{% endif %}

{% endif %}
{% endfor %}
