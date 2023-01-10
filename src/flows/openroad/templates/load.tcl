#===========================(Read liberty libs)===============================#
read_libraries
#=============================(Read LEF files)================================#
read_lef {{platform.tech_lef}}
{% if platform.std_cell_lef %}
read_lef {{platform.std_cell_lef}}
{% endif %}

{% for lef in platform.additional_lef_files %}
read_lef {{lef}}
{% endfor %}

read_verilog {{netlist}}
link_design {{design.rtl.top}}

{% for sdc in settings.sdc_files %}
read_sdc {{sdc}}
{% endfor %}

{% if platform.derate_tcl %}
source {{platform.derate_tcl}}
{% endif %}
{% if platform.setrc_tcl %}
source {{platform.setrc_tcl}}
{% endif %}

puts "Default units for flow"
report_units
report_units_metric

puts "number instances in verilog is [llength [get_cells -hier *]]"

{{ write_checkpoint(step) }}
