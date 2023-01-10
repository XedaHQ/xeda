{% from "macros.tcl.j2" import write_checkpoint, load_checkpoint, preamble, epilogue, section with context %}

{% for step in steps_to_run %}
{% set step_index = loop.index0 + starting_index %}
{% set step_id = "%d_%s"|format(step_index, step) %}
{% set prev_step_id = get_prev_step_id(step) %}
{% if loop.index0 == 0 %}
{% include "utils.tcl" %}
{% endif %}
{{ preamble(step, step_index) }}
{% include step + '.tcl' %}
{{ epilogue(step) }}
{% endfor %}
