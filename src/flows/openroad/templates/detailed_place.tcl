
set_placement_padding -global \
    -left {{platform.cell_pad_in_sites_detail_placement}} \
    -right {{platform.cell_pad_in_sites_detail_placement}}
detailed_placement

if {[info exists ::env(ENABLE_DPO)] && $::env(ENABLE_DPO)} {
    if {[info exist ::env(DPO_MAX_DISPLACEMENT)]} {
        improve_placement -max_displacement $::env(DPO_MAX_DISPLACEMENT)
    } else {
        improve_placement
    }
}
optimize_mirroring

utl::info FLW 12 "Placement violations [check_placement -verbose]."

estimate_parasitics -placement

report_metrics "detailed place" true false

{{ write_checkpoint(step) }}
