# Clone clock tree inverters next to register loads
# so cts does not try to buffer the inverted clocks.
repair_clock_inverters

# Run CTS
clock_tree_synthesis -root_buf "{{platform.cts_buf_cell}}" -buf_list "{{platform.cts_buf_cell}}" \
  -sink_clustering_enable \
  -sink_clustering_size {{settings.cts_cluster_size}} \
  -sink_clustering_max_diameter {{settings.cts_cluster_diameter}} {% if settings.cts_buf_distance %} -distance_between_buffers {{settings.cts_buf_distance}} {% endif %} \
  -balance_levels

set_propagated_clock [all_clocks]
set_dont_use {{settings.dont_use_cells|join(" ")|embrace}}

estimate_parasitics -placement
report_metrics "cts pre-repair"

repair_clock_nets
estimate_parasitics -placement
report_metrics "cts post-repair"

set_placement_padding -global \
  -left {{platform.cell_pad_in_sites_detail_placement}} \
  -right {{platform.cell_pad_in_sites_detail_placement}}
detailed_placement

estimate_parasitics -placement

puts "Repair setup and hold violations..."
repair_timing {% if settings.setup_slack_margin %} -setup_margin {{settings.setup_slack_margin}} {% endif %} {% if settings.hold_slack_margin %} -hold_margin {{settings.hold_slack_margin}} {% endif %}

detailed_placement
check_placement -verbose
report_metrics "cts final"

{{ write_checkpoint(step, sdc=true)}}
