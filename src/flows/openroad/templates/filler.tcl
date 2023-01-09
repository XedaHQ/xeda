set_propagated_clock [all_clocks]

filler_placement {{platform.fill_cells|join(" ")|embrace}}
check_placement

{{maybe_write_snapshot(step_id)}}
