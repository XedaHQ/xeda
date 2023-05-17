set_propagated_clock [all_clocks]

{% if settings.nthreads %}
set_thread_count {{settings.nthreads}}
{% endif %}

{%- set additional_args = settings.detailed_route_additional_args|join(" ") %}
{%- if platform.min_routing_layer %}
{%- set additional_args = additional_args + " -bottom_routing_layer " + platform.min_routing_layer %}
{%- endif %}
{%- if platform.max_routing_layer %}
{%- set additional_args = additional_args + " -top_routing_layer " + platform.max_routing_layer %}
{%- endif %}
{%- if platform.via_in_pin_min_layer %}
{%- set additional_args = additional_args + " -via_in_pin_bottom_layer " + platform.via_in_pin_min_layer %}
{%- endif %}
{%- if platform.via_in_pin_max_layer %}
{%- set additional_args = additional_args + " -via_in_pin_bottom_layer " + platform.via_in_pin_max_layer %}
{%- endif %}
{%- if settings.disable_via_gen %}
{%- set additional_args = additional_args + " -disable_via_gen" %}
{%- endif %}
{%- if settings.db_process_node is not none %}
{%- set additional_args = additional_args + " -db_process_node " + settings.db_process_node %}
{%- endif %}
{%- if settings.detailed_route_or_seed is not none %}
{%- set additional_args = additional_args + " -or-seed " + settings.detailed_route_or_seed %}
{%- endif %}
{%- if settings.detailed_route_or_k is not none %}
{%- set additional_args = additional_args + " -or-k " + settings.detailed_route_or_k %}
{%- endif %}
{%- if settings.repair_pdn_via_layer %}
{%- set additional_args = additional_args + " -repair_pdn_vias " + settings.repair_pdn_via_layer %}
{%- endif %}

detailed_route -output_drc {{settings.reports_dir}}/{{step_id}}_drc.rpt \
    -output_maze {{settings.results_dir}}/maze.log \
    -save_guide_updates -verbose {{settings.verbose}} {{additional_args}}

{% if settings.post_detailed_route_tcl %}
source {{settings.post_detailed_route_tcl}}
{% endif %}

{{ write_checkpoint(step) }}
