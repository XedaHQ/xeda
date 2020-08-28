{% include 'interface.tcl' %}

{% include 'run_synth.tcl' %}

{% include 'run_sim.tcl' %}

if {${gen_saif} && false} {
    if {$run_synth_flow != 1} {
        open_checkpoint ${checkpoints_dir}/post_route.dcp
    }

    eval read_saif ${saif_file}

    report_power -file ${reports_dir}/post_route/post_synth_power.rpt
}