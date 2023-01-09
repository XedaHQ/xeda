{% from "macros.tcl.j2" import write_checkpoint, preamble with context %}
{{ preamble(step_id) }}
{% include "utils.tcl" %}

read_libraries
read_db $results_dir/{{ prev_step_id }}.odb
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

{{write_checkpoint(step_id, verilog=true, def=true)}}

{% if rcx_rules %}
puts "Starting RCX"
# RCX section
define_process_corner -ext_model_index 0 X
extract_parasitics -ext_model_file {{rcx_rules}}

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

log_begin $reports_dir/{{step_id}}.rpt
report_metrics "finish"
log_end

check_setup > $reports_dir/{{step_id}}_setup.rpt
report_power > $reports_dir/{{step_id}}_power.rpt

{% if settings.save_images %}
proc save_images {route_drc_rpt, step_id} {
  gui::save_display_controls

  set height [[[ord::get_db_block] getBBox] getDY]
  set height [ord::dbu_to_microns $height]
  set resolution [expr $height / 1000]

  # Show the drc markers (if any)
  if {[file exists $route_drc_rpt] == 1} {
    gui::load_drc $route_drc_rpt
  }

  gui::clear_selections

  # Setup initial visibility to avoid any previous settings
  gui::set_display_controls "*" visible false
  gui::set_display_controls "Layers/*" visible true
  gui::set_display_controls "Nets/*" visible true
  gui::set_display_controls "Instances/*" visible false
  gui::set_display_controls "Instances/StdCells/*" visible true
  gui::set_display_controls "Instances/Macro" visible true
  gui::set_display_controls "Instances/Pads/*" visible true
  gui::set_display_controls "Instances/Physical/*" visible true
  gui::set_display_controls "Pin Markers" visible true
  gui::set_display_controls "Misc/Instances/names" visible true
  gui::set_display_controls "Misc/Scale bar" visible true
  gui::set_display_controls "Misc/Highlight selected" visible true
  gui::set_display_controls "Misc/Detailed view" visible true

  # The routing view
  save_image -resolution $resolution $reports_dir/${step_id}_routing.webp

  # The placement view without routing
  gui::set_display_controls "Layers/*" visible false
  gui::set_display_controls "Instances/Physical/*" visible false
  save_image -resolution $resolution $reports_dir/${step_id}_placement.webp

  {% if platform.pwr_nets_voltages %}
  gui::set_display_controls "Heat Maps/IR Drop" visible true
  gui::set_heatmap IRDrop Layer {{platform.ir_drop_layer}}
  gui::set_heatmap IRDrop ShowLegend 1
  save_image -resolution $resolution $reports_dir/${step_id}_ir_drop.webp
  gui::set_display_controls "Heat Maps/IR Drop" visible false
  {% endif %}

  # The clock view: all clock nets and buffers
  gui::set_display_controls "Layers/*" visible true
  gui::set_display_controls "Nets/*" visible false
  gui::set_display_controls "Nets/Clock" visible true
  gui::set_display_controls "Instances/*" visible false
  gui::set_display_controls "Instances/StdCells/Clock tree/*" visible true
  select -name "clk*" -type Inst
  save_image -resolution $resolution $reports_dir/${step_id}_clocks.webp
  gui::clear_selections

  # The resizer view: all instances created by the resizer grouped
  gui::set_display_controls "Layers/*" visible false
  gui::set_display_controls "Instances/*" visible true
  gui::set_display_controls "Instances/Physical/*" visible false
  select -name "hold*" -type Inst -highlight 0       ;# green
  select -name "input*" -type Inst -highlight 1      ;# yellow
  select -name "output*" -type Inst -highlight 1
  select -name "repeater*" -type Inst -highlight 3   ;# magenta
  select -name "fanout*" -type Inst -highlight 3
  select -name "load_slew*" -type Inst -highlight 3
  select -name "max_cap*" -type Inst -highlight 3
  select -name "max_length*" -type Inst -highlight 3
  select -name "wire*" -type Inst -highlight 3
  select -name "rebuffer*" -type Inst -highlight 4   ;# red
  select -name "split*" -type Inst -highlight 5      ;# dark green

  save_image -resolution $resolution $reports_dir/${step_id}_resizer.webp
  for {set i 0} {$i <= 5} {incr i} {
    gui::clear_highlights $i
  }
  gui::clear_selections

  foreach clock [get_clocks *] {
    set clock_name [get_name $clock]
    gui::save_clocktree_image $reports_dir/cts_$clock_name.webp $clock_name
  }

  gui::restore_display_controls
}
# Save a final image if openroad is compiled with the gui
if {[expr [llength [info procs save_image]] > 0]} {
    gui::show "save_images($reports_dir/{{ get_step_id('detailed_route') }}_drc.rpt, {{step_id}})" false
}
{% endif %}
