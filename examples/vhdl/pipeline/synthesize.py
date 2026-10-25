from subprocess import check_call


def quartus_synth():
    check_call(
        [
            "xeda",
            "run",
            "quartus",
            "pipelined_adder.yaml",
            "--settings",
            "dockerized=true",
            "fpga.part=10CL016YU256C6G",
            "clocks.main_clock.period=15ns",
        ]
    )


if __name__ == "__main__":
    quartus_synth()
