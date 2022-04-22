from subprocess import check_call


def quartus_synth():
    check_call(
        [
            "xeda",
            "run",
            "quartus",
            "--design",
            "pipelined_adder.toml",
            "--settings",
            "dockerized=true",
            "fpga.part=10CL016YU256C6G",
            "clock_period=15",
        ]
    )


if __name__ == "__main__":
    quartus_synth()
