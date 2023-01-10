set_propagated_clock [all_clocks]

filler_placement {{platform.fill_cells|join(" ")|embrace}}
check_placement

{{ write_checkpoint(step) }}
