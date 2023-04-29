{{ load_checkpoint(step, sdc_from="cts") }}

set_propagated_clock [all_clocks]

# Ensure all OR created (rsz/cts) instances are connected
global_connect

# Delete routing obstructions for final DEF
deleteRoutingObstructions

{%- if settings.density_fill %}
density_fill -rules {{platform.fill_config}}
{%- endif %}

{{ write_checkpoint(step, verilog=true, def=true) }}

{% if platform.default_corner_settings.rcx_rules %}
puts "Starting RCX"
# RCX section
define_process_corner -ext_model_index 0 X
extract_parasitics -ext_model_file {{platform.default_corner_settings.rcx_rules}}

set spef_file $results_dir/{{step_id}}.spef
write_spef $spef_file
file delete {{design.rtl.top}}.totCap

# Read Spef for OpenSTA
if { [file exists $spef_file]} {
  read_spef $spef_file
} else {
  utl::error FLW 99 "SPEF file $spef_file does not exist!"
}

{% if settings.final_irdrop_analysis %}
# Static IR drop analysis
{% for net, voltage in platform.pwr_nets_voltages.items() %}
puts "set_pdnsim_net_voltage {{net}}..."
set_pdnsim_net_voltage -net {{net}} -voltage {{voltage}}
puts "analyze_power_grid {{net}}..."
analyze_power_grid -net {{net}}
{% endfor %}
{% for net, voltage in platform.gnd_nets_voltages.items() %}
puts "set_pdnsim_net_voltage {{net}}..."
set_pdnsim_net_voltage -net {{net}} -voltage {{voltage}}
puts "analyze_power_grid {{net}}..."
analyze_power_grid -net {{net}}
{% endfor %}
{% endif %}

{% endif %}

{% if settings.write_cdl and settings.cdl_masters_file %}
write_cdl -masters {{cdl_masters_file}} $results_dir/{{step_id}}.cdl
{% endif %}

report_metrics {{step}}

{% if settings.save_images %}
{% include "save_images.tcl" %}

# Save a final image if openroad is compiled with the gui
if {[expr [llength [info procs save_image]] > 0]} {
  gui::show save_images false
}
{% endif %}
