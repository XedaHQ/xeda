{% for clock_name, clock in settings.clocks.items() -%}
{% if clock.port -%}
create_clock -period {{clock.period|round(3,'floor')}} -name {{clock_name}} [get_ports {{clock.port}}]
{% endif -%}
{% endfor -%}