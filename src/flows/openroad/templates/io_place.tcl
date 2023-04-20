{% if settings.floorplan_def %}
puts "Skipping IO placement as DEF file was used to initialize floorplan."
{% else %}

{% if settings.io_constraints %} {# ---------------------------- #}
source {{settings.io_constraints}}

{% else %} {# -------------------------------------------------- #}
# check the lower boundary of the PLACE_DENSITY and add PLACE_DENSITY_LB_ADDON if it exists
{% if settings.place_density_lb_addon is not none %}
set place_density_lb [gpl::get_global_placement_uniform_density \
    -pad_left {{platform.cell_pad_in_sites_global_placement}} \
    -pad_right {{platform.cell_pad_in_sites_global_placement}}]
set place_density [expr $place_density_lb + {{settings.place_density_lb_addon}} + 0.01]
if {$place_density > 1.0} {
    set place_density 1.0
}
{% else %}
set place_density {{settings.place_density or platform.place_density}}
{% endif %}

global_placement -skip_io -density $place_density \
    -pad_left {{platform.cell_pad_in_sites_global_placement}} \
    -pad_right {{platform.cell_pad_in_sites_global_placement}} {{ settings.global_placement_args|join(" ") }}

{% endif %} {# ------------------------------------------------- #}

place_pins -hor_layer {{platform.io_placer_h}} -ver_layer {{platform.io_placer_v}} {% if settings.io_place_random %} -random {% endif %}  {{settings.place_pins_args|join(" ")}}
{% endif %} {# --not settings.floorplan_def-- #}

{{ write_checkpoint(step) }}
