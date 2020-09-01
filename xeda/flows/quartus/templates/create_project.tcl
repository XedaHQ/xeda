set design_name           {{design.name}}
set clock_port            {{design.clock_port}}
set clock_period          {{flow.clock_period}}
set top                   {{design.top}}
set tb_top                {{design.tb_top}}
# set strategy              {{flow.strategy}}

set debug                 {{debug}}
# set run_synth_flow        {{run_synth_flow}}
# set run_postsynth_sim     {{run_postsynth_sim}}
# set optimize_power        {{flow.optimize_power}}

foreach key [array names quartus] {
    puts "${key}=$quartus($key)"
}

package require ::quartus::project

puts "\n===========================( Setting up project and settings )==========================="
project_new ${design_name} -overwrite

set_global_assignment -name NUM_PARALLEL_PROCESSORS {{nthreads}}

set_global_assignment -name DEVICE {{flow.fpga_part}}


set_global_assignment -name TOP_LEVEL_ENTITY ${top}

{% if design.vhdl_std == "08" %}
    set_global_assignment -name VHDL_INPUT_VERSION VHDL_2008
{% endif %}

{% for src in design.sources if not src.sim_only and src.type %}
set_global_assignment -name {% if src.type == "verilog" and src.variant == "systemverilog" -%} SYSTEMVERILOG {%- else -%} {{src.type|upper}} {%- endif -%}_FILE {{src.file}}
{% endfor %}

# TODO move to jinja template
puts "writing sdc file...\n"

set sdc_filename "clock.sdc"
set sdc_file [open ${sdc_filename} w]
puts $sdc_file "create_clock -period ${clock_period} -name clock \[get_ports ${clock_port}\]"
close $sdc_file

set_global_assignment -name SDC_FILE $sdc_filename

puts "clocks: [get_clocks]"

set_global_assignment -name NUM_PARALLEL_PROCESSORS  {{nthreads}}

# see https://www.intel.com/content/www/us/en/programmable/documentation/zpr1513988353912.html
# https://www.intel.com/content/www/us/en/programmable/quartushelp/current/index.htm    
## BALANCED "HIGH PERFORMANCE EFFORT" AGGRESSIVE PERFORMANCE
# "High Performance with Maximum Placement Effort"
# "Superior Performance"
# "Superior Performance with Maximum Placement Effort"
# "Aggressive Area"
# "High Placement Routability Effort"
# "High Packing Routability Effort"
# "Optimize Netlist for Routability
# "High Power Effort"
set_global_assignment -name OPTIMIZATION_MODE  "HIGH PERFORMANCE EFFORT"
set_global_assignment -name REMOVE_REDUNDANT_LOGIC_CELLS ON
set_global_assignment -name AUTO_RESOURCE_SHARING ON
set_global_assignment -name ALLOW_REGISTER_RETIMING ON

set_global_assignment -name SYNTH_GATED_CLOCK_CONVERSION ON


# faster: AUTO FIT, fastest: FAST_FIT
set_global_assignment -name FITTER_EFFORT "STANDARD FIT"

# AREA, SPEED, BALANCED
set_global_assignment -name STRATIX_OPTIMIZATION_TECHNIQUE SPEED
set_global_assignment -name CYCLONE_OPTIMIZATION_TECHNIQUE SPEED

# see https://www.intel.com/content/www/us/en/programmable/documentation/rbb1513988527943.html
# The Router Effort Multiplier controls how quickly the router tries to find a valid solution. The default value is 1.0 and legal values must be greater than 0.
# Numbers higher than 1 help designs that are difficult to route by increasing the routing effort.
# Numbers closer to 0 (for example, 0.1) can reduce router runtime, but usually reduce routing quality slightly.
# Experimental evidence shows that a multiplier of 3.0 reduces overall wire usage by approximately 2%. Using a Router Effort Multiplier higher than the default value can benefit designs with complex datapaths with more than five levels of logic. However, congestion in a design is primarily due to placement, and increasing the Router Effort Multiplier does not necessarily reduce congestion.
# Note: Any Router Effort Multiplier value greater than 4 only increases by 10% for every additional 1. For example, a value of 10 is actually 4.6.
set_global_assignment -name PLACEMENT_EFFORT_MULTIPLIER 3.0
set_global_assignment -name ROUTER_EFFORT_MULTIPLIER 3.0

# NORMAL, MINIMUM,MAXIMUM
set_global_assignment -name ROUTER_TIMING_OPTIMIZATION_LEVEL MAXIMUM

# ALWAYS, AUTOMATICALLY, NEVER
set_global_assignment -name FINAL_PLACEMENT_OPTIMIZATION ALWAYS


# set_global_assignment -name PHYSICAL_SYNTHESIS_COMBO_LOGIC_FOR_AREA ON

set_global_assignment -name ADV_NETLIST_OPT_SYNTH_GATE_RETIME ON
set_global_assignment -name ADV_NETLIST_OPT_SYNTH_WYSIWYG_REMAP ON
set_global_assignment -name AUTO_PACKED_REGISTERS_STRATIX OFF
set_global_assignment -name PHYSICAL_SYNTHESIS_COMBO_LOGIC ON
set_global_assignment -name PHYSICAL_SYNTHESIS_REGISTER_DUPLICATION ON
set_global_assignment -name PHYSICAL_SYNTHESIS_REGISTER_RETIMING ON
set_global_assignment -name PHYSICAL_SYNTHESIS_EFFORT EXTRA
set_global_assignment -name AUTO_DSP_RECOGNITION OFF


#NORMAL, OFF, EXTRA_EFFORT
#set_global_assignment -name OPTIMIZE_POWER_DURING_SYNTHESIS NORMAL

# Used during placement. Use of a higher value increases compilation time, but may increase the quality of placement.
set_global_assignment -name INNER_NUM 8



export_assignments

project_close