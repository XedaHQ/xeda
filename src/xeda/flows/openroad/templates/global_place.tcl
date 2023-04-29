
set_dont_use {{settings.dont_use_cells|join(" ")|embrace}}

# set fastroute layer reduction
{% if platform.fastroute_tcl %}
source {{platform.fastroute_tcl}}
{% else %}

set_global_routing_layer_adjustment {{platform.min_routing_layer}}-{{platform.max_routing_layer}} 0.5
set_routing_layers -signal {{platform.min_routing_layer}}-{{platform.max_routing_layer}}
set_macro_extension 2
{% endif %}

# check the lower boundary of the PLACE_DENSITY and add PLACE_DENSITY_LB_ADDON if it exists
{% if settings.place_density_lb_addon %}
set place_density_lb [gpl::get_global_placement_uniform_density \
    -pad_left {{platform.cell_pad_in_sites_global_placement}} \
    -pad_right {{platform.cell_pad_in_sites_global_placement}}]
set place_density [expr $place_density_lb + ((1.0 - $place_density_lb) * {{settings.place_density_lb_addon}} + 0.01]
if {$place_density > 1.0} {
    utl::error FLW 24 "Place density exceeds 1.0 (current PLACE_DENSITY_LB_ADDON = {{settings.place_density_lb_addon}}). Please check if the value of PLACE_DENSITY_LB_ADDON is between 0 and 0.99."
}
{% else %}
set place_density {{settings.place_density or platform.place_density}}
{% endif %}

set global_placement_args "{{ settings.global_placement_args|join(" ") }}"
{% if settings.gpl_routability_driven %}
append global_placement_args " -routability_driven"
{% endif %}
{% if settings.gpl_timing_driven %}
append global_placement_args " -timing_driven"
{% endif %}
{% if settings.min_phi_coef is not none %}
append global_placement_args " -min_phi_coef {{settings.min_phi_coef}}"
{% endif %}

{% if settings.max_phi_coef is not none %}
append global_placement_args " -max_phi_coef {{settings.max_phi_coef}}"
{% endif %}

global_placement -density $place_density \
    -pad_left {{platform.cell_pad_in_sites_global_placement}} \
    -pad_right {{platform.cell_pad_in_sites_global_placement}} \
    {*}$global_placement_args

estimate_parasitics -placement
report_metrics "global place" false false

{{ write_checkpoint(step) }}
