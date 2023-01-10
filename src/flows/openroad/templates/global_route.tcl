{% if platform.fastroute_tcl %}
source {{platform.fastroute_tcl}}
{% else %}
set_global_routing_layer_adjustment {{platform.min_routing_layer}}-{{platform.max_routing_layer}} {{settings.global_routing_layer_adjustment}}
set_routing_layers -signal {{platform.min_routing_layer}}-{{platform.max_routing_layer}}
{% endif %}

global_route -guide_file {{settings.results_dir}}/route.guide \
  -congestion_iterations {{settings.congestion_iterations}} \
  -congestion_report_file {{settings.reports_dir}}/congestion.rpt {% if settings.verbose %} -verbose {% endif %}

set_propagated_clock [all_clocks]
estimate_parasitics -global_routing
report_metrics "global route"

{% if settings.repair_antennas %}
repair_antennas
{% endif %}

print_banner "check_antennas"
check_antennas -report_file {{settings.reports_dir}}/{{step_id}}_antenna.log

{% if (settings.clocks|length) == 1 and settings.update_sdc_margin %}
{{section("write_ref_sdc")}}
# Write an SDC file with clock periods that result in slightly negative (failing) slack.
set clks [all_clocks]
set clk [lindex $clks 0]
set clk_name [get_name $clk]
set period [get_property $clk "period"]
# Period is in sdc/liberty units.
utl::info "write_ref_sdc" 7 "clock $clk_name period $period"
set slack [sta::time_sta_ui [sta::worst_slack_cmd "max"]]
set ref_period [expr ($period - $slack) * (1.0 - {{settings.update_sdc_margin}})]
utl::info "write_ref_sdc" 8 "Clock $clk_name period [format %.3f $ref_period]"
utl::info "write_ref_sdc" 9 "Clock $clk_name slack [format %.3f $slack]"

set sources [$clk sources]
# Redefine clock with updated period.
create_clock -name $clk_name -period $ref_period $sources
# Undo the set_propagated_clock so SDC at beginning of flow uses ideal clocks.
unset_propagated_clock [all_clocks]
write_sdc {{settings.results_dir}}/{{step_id}}_updated_clks.sdc
# Reset
create_clock -name $clk_name -period $period $sources
set_propagated_clock [all_clocks]
{% endif %}

{{ write_checkpoint(step) }}
