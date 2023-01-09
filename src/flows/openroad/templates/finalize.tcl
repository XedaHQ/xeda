{% include "utils.tcl" %}

variable step_id {{step_id}}

read_libraries
read_db $results_dir/{{ get_step_id("filler") }}.odb
read_sdc $results_dir/{{ get_step_id("cts") }}.sdc
source {{platform.setrc_tcl}}

set_propagated_clock [all_clocks]

# Ensure all OR created (rsz/cts) instances are connected
global_connect

# Delete routing obstructions for final DEF
deleteRoutingObstructions

{%- if settings.density_fill %}
density_fill -rules {{platform.fill_config}}
{%- endif %}

write_db {{results_dir}}/{{step_id}}.odb
write_def {{results_dir}}/{{step_id}}.def
write_verilog {{results_dir}}/{{step_id}}.v

{% if rcx_rules %}
puts "Starting RCX"
# RCX section
define_process_corner -ext_model_index 0 X
extract_parasitics -ext_model_file {{rcx_rules}}

write_spef $results_dir/{{step_id}}.spef
file delete {{design.rtl.top}}.totCap

# Read Spef for OpenSTA
read_spef $results_dir/{{step_id}}.spef

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
write_cdl -masters{{cdl_masters_file}} $results_dir/{{step_id}}.cdl
{% endif %}

report_metrics "finish"

{% if settings.save_images %}
# Save a final image if openroad is compiled with the gui
if {[expr [llength [info procs save_image]] > 0]} {
    gui::show "save_images($reports_dir/{{ get_step_id('detailed_route') }}_drc.rpt)" false
}
{% endif %}

