{# TODO: write/load snapshots, check errors/warnings, load pre/post scripts, etc #}
{%- macro preamble(step, index, total) -%}
{%- set line_len = 80 -%}
{%- set msg = (" Starting %s (%d/%d) "|format(step, index, total)) -%}
{%- set msg_len = (msg|length) -%}
{%- set sp1 = (line_len - msg_len) // 2 -%}
{%- set sp2 = line_len - sp1 - msg_len -%}
puts "{{ '=' * line_len }}"
puts "{{ '=' * sp1 }}{{msg}}{{'=' * sp2 }}"
puts "{{ '=' * line_len }}"
{% endmacro %}

{%- macro epilogue(step) -%}
puts "{{step}} done"
puts ""
{{ "#" * 80 }}
{% endmacro %}

{%- macro maybe_load_snapshot(db_step_id, sdc_step_id=none) -%}
{% if db_step_id -%} load_db {{results_dir}}/{{db_step_id}}.odb {%- endif %}
{% if sdc_step_id -%} load_sdc {{results_dir}}/{{sdc_step_id}}.sdc {%- endif %}
{% if platform.derate_tcl %}
source {{platform.derate_tcl}}
{% endif %}
source {{platform.setrc_tcl}}
{% endmacro %}

{%- macro maybe_write_snapshot(step_id, db=true, sdc=false, def=false) -%}
{% if db -%} write_db {{results_dir}}/{{step_id}}.odb {%- endif %}
{% if sdc -%} write_sdc {{results_dir}}/{{step_id}}.sdc {%- endif %}
{% if sdc -%} write_def {{results_dir}}/{{step_id}}.def {%- endif %}
{% endmacro %}

{%- macro section(name) -%}
puts "-- {{name}}"
{% endmacro %}

{%- include "utils.tcl" %}

{% set num_steps = flow_steps|length -%}
{% set prev_step_id = "" -%}
{% for step in flow_steps -%}
{% set step_id = "%d_%s"|format(loop.index0, step) -%}
{{ preamble(step, loop.index0, num_steps) }}
{% include step + '.tcl' %}
{{ epilogue(step) }}
{% set prev_step_id = step_id -%}
{% endfor %}
