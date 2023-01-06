read_lef {{platform.tech_lef}}
{%- if platform.merged_lef %}
read_lef {{platform.merged_lef}}
{%- endif %}
{%- for lef in platform.additional_lef_files %}
read_lef {{lef}}
{%- endfor %}

{%- if (platform.corner|length) > 1 %}
define_corners {{platform.corner.keys()|join(" ")}}

{%- for corner,s in platform.corner %}
    {%- for lib in s.lib_files %}
    read_liberty -corner {{corner}} {{lib}}
    {%- endfor %}
    {%- if s.dff_lib_file %}
    read_liberty {{s.dff_lib_file}}
    {%- endif %}
{%- endfor %}

{%- else %}

{%- set s = (platform.corner.values()|first) %}
{%- for lib in s.lib_files %}
read_liberty {{lib}}
{%- endfor %}
{%- if s.dff_lib_file %}
read_liberty {{s.dff_lib_file}}
{%- endif %}

{%- endif %}

read_verilog {{netlist}}
link_design {{design.rtl.top}}

{%- for sdc in sdc_files %}
read_sdc {{sdc}}
{%- endfor %}

{%- if platform.derate_tcl %}
source {{platform.derate_tcl}}
{%- endif %}
{%- if platform.setrc_tcl %}
source {{platform.setrc_tcl}}
{%- endif %}

set SCRIPTS_DIR $::env(SCRIPTS_DIR)
set SOURCE_FLAGS [list -verbose {%- if settings.debug %} -echo {%- endif %}]

# set standalone 0

# Floorplan
source {*}$SOURCE_FLAGS $SCRIPTS_DIR/floorplan.tcl
source {*}$SOURCE_FLAGS $SCRIPTS_DIR/io_placement_random.tcl
source {*}$SOURCE_FLAGS $SCRIPTS_DIR/tdms_place.tcl
source {*}$SOURCE_FLAGS $SCRIPTS_DIR/macro_place.tcl
source {*}$SOURCE_FLAGS $SCRIPTS_DIR/tapcell.tcl
source {*}$SOURCE_FLAGS $SCRIPTS_DIR/pdn.tcl

# Place
source {*}$SOURCE_FLAGS $SCRIPTS_DIR/global_place_skip_io.tcl
source {*}$SOURCE_FLAGS $SCRIPTS_DIR/io_placement.tcl
source {*}$SOURCE_FLAGS $SCRIPTS_DIR/global_place.tcl
source {*}$SOURCE_FLAGS $SCRIPTS_DIR/resize.tcl
source {*}$SOURCE_FLAGS $SCRIPTS_DIR/detail_place.tcl

# CTS
source {*}$SOURCE_FLAGS $SCRIPTS_DIR/cts.tcl
source {*}$SOURCE_FLAGS $SCRIPTS_DIR/fillcell.tcl

# Route
source {*}$SOURCE_FLAGS $SCRIPTS_DIR/global_route.tcl
source {*}$SOURCE_FLAGS $SCRIPTS_DIR/detail_route.tcl

# if {[info exists ::env(USE_FILL)] && $::env(USE_FILL)} {
#   source {*}$SOURCE_FLAGS $SCRIPTS_DIR/density_fill.tcl
# }

# Finishing
source {*}$SOURCE_FLAGS $SCRIPTS_DIR/final_report.tcl
source {*}$SOURCE_FLAGS $SCRIPTS_DIR/klayout.tcl