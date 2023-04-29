
{{ section("io_placement_random") }}
{% if not settings.floorplan_def %}
{% if settings.io_constraints %}
source {{settings.io_constraints}}
{% endif %}
place_pins -hor_layer {{platform.io_placer_h}} -ver_layer {{platform.io_placer_v}} -random {{settings.place_pins_args|join(" ")}}
{% endif %}

{% if not settings.macro_placement_file %}
{{ section("tdms_place") }}
set_dont_use {{settings.dont_use_cells|join(" ")|embrace}}

set macros_found [find_macros]

{% if not settings.rtlmp_flow %}
if {$macros_found != ""} {
    global_placement -density {{settings.place_density or platform.place_density}} \
        -pad_left {{platform.cell_pad_in_sites_global_placement}} \
        -pad_right {{platform.cell_pad_in_sites_global_placement}}
} else {
    puts "No macros found: Skipping global_placement"
}
{% endif %}

{% endif %}

if {$macros_found != ""} {
    # If wrappers defined replace macros with their wrapped version
    # # ----------------------------------------------------------------------------
    {% if settings.macro_wrappers %}
    source {{settings.macro_wrappers}}

    set wrapped_macros [dict keys [dict get $wrapper around]]
    set db [ord::get_db]
    set block [ord::get_db_block]

    foreach inst [$block getInsts] {
        if {[lsearch -exact $wrapped_macros [[$inst getMaster] getName]] > -1} {
            set new_master [dict get $wrapper around [[$inst getMaster] getName]]
            puts "Replacing [[$inst getMaster] getName] with $new_master for [$inst getName]"
            $inst swapMaster [$db findMaster $new_master]
        }
    }
    {% endif %}

    set halo_max [expr max({{platform.macro_place_halo[0]}}, {{platform.macro_place_halo[1]}})]
    set channel_max [expr max({{platform.macro_place_channel[0]}}, {{platform.macro_place_channel[1]}})]
    set blockage_width [expr max($halo_max, $channel_max/2)]

    if {[info exists ::env(MACRO_BLOCKAGE_HALO)]} {
        set blockage_width $::env(MACRO_BLOCKAGE_HALO)
    }

    {% if settings.rtlmp_flow %}
    puts "HierRTLMP Flow enabled..."
    set additional_rtlmp_args ""
    if { [info exists ::env(RTLMP_MAX_LEVEL)]} {
        append additional_rtlmp_args " -max_num_level $env(RTLMP_MAX_LEVEL)"
    }
    if { [info exists ::env(RTLMP_MAX_INST)]} {
        append additional_rtlmp_args " -max_num_inst $env(RTLMP_MAX_INST)"
    }
    if { [info exists ::env(RTLMP_MIN_INST)]} {
        append additional_rtlmp_args " -min_num_inst $env(RTLMP_MIN_INST)"
    }
    if { [info exists ::env(RTLMP_MAX_MACRO)]} {
        append additional_rtlmp_args " -max_num_macro $env(RTLMP_MAX_MACRO)"
    }
    if { [info exists ::env(RTLMP_MIN_MACRO)]} {
        append additional_rtlmp_args " -min_num_macro $env(RTLMP_MIN_MACRO)"
    }

    append additional_rtlmp_args " -halo_width $halo_max"

    if { [info exists ::env(RTLMP_MIN_AR)]} {
        append additional_rtlmp_args " -min_ar $env(RTLMP_MIN_AR)"
    }
    if { [info exists ::env(RTLMP_AREA_WT)]} {
        append additional_rtlmp_args " -area_weight $env(RTLMP_AREA_WT)"
    }
    if { [info exists ::env(RTLMP_WIRELENGTH_WT)]} {
        append additional_rtlmp_args " -wirelength_weight $env(RTLMP_WIRELENGTH_WT)"
    }
    if { [info exists ::env(RTLMP_OUTLINE_WT)]} {
        append additional_rtlmp_args " -outline_weight $env(RTLMP_OUTLINE_WT)"
    }
    if { [info exists ::env(RTLMP_BOUNDARY_WT)]} {
        append additional_rtlmp_args " -boundary_weight $env(RTLMP_BOUNDARY_WT)"
    }

    if { [info exists ::env(RTLMP_NOTCH_WT)]} {
        append additional_rtlmp_args " -notch_weight $env(RTLMP_NOTCH_WT)"
    }

    if { [info exists ::env(RTLMP_DEAD_SPACE)]} {
        append additional_rtlmp_args " -dead_space $env(RTLMP_DEAD_SPACE)"
    }
    if { [info exists ::env(RTLMP_CONFIG_FILE)]} {
        append additional_rtlmp_args " -config_file $env(RTLMP_CONFIG_FILE)"
    }
    if { [info exists ::env(RTLMP_RPT_DIR)]} {
        append additional_rtlmp_args " -report_directory $env(RTLMP_RPT_DIR)"
    }
    puts "Call Macro Placer $additional_rtlmp_args"
    rtl_macro_placer \
        {*}$additional_rtlmp_args
    puts "Delete buffers for RTLMP flow..."
    remove_buffers

    {% else %}

    {% if settings.macro_placement_file %}
    puts "\[INFO\]\[FLOW-xxxx\] Using manual macro placement file {{settings.macro_placement_file}}"
    read_macro_placement {{settings.macro_placement_file}}
    {% else %}
    macro_placement \
        -halo {{platform.macro_place_halo[0]}} {{platform.macro_place_halo[1]}} \
        -channel {{platform.macro_place_channel[0]}} {{platform.macro_place_channel[1]}}
    {% endif %}
    {% endif %}
    block_channels $blockage_width
} else {
    puts "No macros found: Skipping macro_placement"
}

{% if platform.tapcell_tcl %}
################ tapcell
{{ section("tapcell") }}
source {{platform.tapcell_tcl}}
{% endif %}
################ PDN
{{ section("pdn") }}
source {{platform.pdn_tcl}}

pdngen

{% if settings.post_pdn_tcl %}
source $::env(POST_PDN_TCL)
{% endif %}

# Check all supply nets
set block [ord::get_db_block]
foreach net [$block getNets] {
    set type [$net getSigType]
    if {$type == "POWER" || $type == "GROUND"} {
        ###
        puts "Check supply: [$net getName]"
        check_power_grid -net [$net getName]
    }
}

{{ write_checkpoint(step) }}
