{% from "macros.tcl.j2" import write_checkpoint, preamble, epilogue, section with context %}
{% include "utils.tcl" %}

{% set num_steps = flow_steps|length %}
{% set prev_step_id = "" %}
{% for step in flow_steps %}
{% set step_index = loop.index0 %}
{% set step_id = "%d_%s"|format(step_index, step) %}
{{ preamble(step, step_index + 1, num_steps) }}
{% include step + '.tcl' %}
{{ epilogue(step) }}
{% set prev_step_id = step_id %}
{% endfor %}
