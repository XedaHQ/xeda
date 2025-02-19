{% for clock_name, clock in settings.clocks.items() -%}
{% if clock.port -%}
create_clock -period {{"%.03f" % clock.period}} -name {{clock_name}} [get_ports {{clock.port}}]
{% endif -%}
{% endfor -%}

{% if other_constraints %}
{{ other_constraints }}
{% endif -%}
