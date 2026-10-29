"""Completion of flow settings through the Click shell protocol."""

import os
import re
import select
import subprocess
import sys
import sysconfig
import time
from pathlib import Path

try:
    import fcntl
    import pty
    import termios
except ImportError:  # Windows does not provide POSIX pseudo-terminals.
    fcntl = pty = termios = None
import pytest
from click.testing import CliRunner

from xeda.cli import cli
from xeda.cli_settings_completion import (
    _completion_records,
    _parse_request_words,
    _settings_candidates,
)
from xeda.flow_catalog import _builtin_flows

from .test_cli_completion import _require_shell
from .tool_utils import HANG_GUARD

pytestmark = pytest.mark.python_compat


def _complete(shell: str, words: list[str], incomplete: str) -> list[str]:
    """Run the real Click completion protocol with a complete-word context."""
    all_words = ["xeda", *words, incomplete]
    result = CliRunner().invoke(
        cli,
        [],
        env={
            "_XEDA_COMPLETE": f"{shell}_complete",
            "COMP_WORDS": " ".join(all_words),
            "COMP_CWORD": incomplete if shell == "fish" else str(len(all_words) - 1),
            "COMP_LINE": "",
            "COMP_POINT": "",
        },
    )
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    lines = [line for line in result.stdout.splitlines() if line]
    if shell == "zsh":
        return lines[1::3]
    return [line.partition(",")[2].partition("\t")[0] for line in lines]


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_trailing_settings_flag_keeps_flow_context(shell: str) -> None:
    assert _complete(shell, ["run", "vivado_synth", "-s"], "vivado_synth.fpga.p") == [
        "vivado_synth.fpga.package=",
        "vivado_synth.fpga.part=",
        "vivado_synth.fpga.pins=",
    ]


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_nested_setting_keys_complete_only_direct_children(shell: str) -> None:
    assert _complete(
        shell, ["run", "vivado_synth", "design.yaml", "-s"], "vivado_synth.fpga.p"
    ) == [
        "vivado_synth.fpga.package=",
        "vivado_synth.fpga.part=",
        "vivado_synth.fpga.pins=",
    ]


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_settings_complete_the_node_then_its_setting(shell: str) -> None:
    assert _complete(
        shell,
        ["run", "yosys_fpga+nextpnr", "design.yaml", "--settings", "nextpnr.docker=false"],
        "nextpnr.nthr",
    ) == ["nextpnr.nthreads="]


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_finite_setting_values_are_completed(shell: str) -> None:
    candidate = "true" if shell == "bash" else "nextpnr.docker=true"
    assert _complete(shell, ["run", "nextpnr", "design.yaml", "-s"], "nextpnr.docker=tr") == [
        candidate
    ]


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_omitted_design_keeps_a_later_path_prefix_ambiguous(shell: str) -> None:
    assert "fpga=" not in _complete(
        shell, ["run", "vivado_synth", "-s", "vivado_synth.nthreads=2"], "fpga"
    )


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_output_qualifier_restricts_the_next_chain_flow(shell: str) -> None:
    assert (
        _complete(
            shell,
            ["run", "yosys_fpga.netlist+vivado_impl", "design.yaml", "-s"],
            "vivado_impl.docker",
        )
        == []
    )


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_ordinary_option_value_keeps_normal_completion(shell: str, tmp_path) -> None:
    output_dir = tmp_path / "build-artifacts"
    output_dir.mkdir()
    words = [
        "run",
        "yosys_fpga",
        "design.yaml",
        "-s",
        "yosys_fpga.docker=true",
        "--outputs-to",
    ]
    actual = _complete(
        shell,
        words,
        str(tmp_path / "build"),
    )
    baseline = _complete(
        shell, ["run", "yosys_fpga", "design.yaml", "--outputs-to"], str(tmp_path / "build")
    )
    assert actual == baseline
    assert "true" not in actual


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_incomplete_settings_option_keeps_option_completion(shell: str) -> None:
    candidates = _complete(shell, ["run", "nextpnr", "design.yaml"], "--settings")
    assert "--settings" in candidates
    assert "allow_fabric_eclk=" not in candidates


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_a_node_of_the_chain_completes_its_own_settings(shell: str) -> None:
    assert _complete(
        shell,
        ["run", "yosys_fpga+nextpnr", "design.yaml", "-s"],
        "nextpnr.randomi",
    ) == ["nextpnr.randomize_seed="]


def test_opaque_setting_before_omitted_design_is_counted_as_a_setting() -> None:
    opaque_setting = "vivado_synth.set_synth_properties.STEPS.SYNTH_DESIGN.ARGS.MORE OPTIONS=foo"
    state = _parse_request_words(cli, ["run", "vivado_synth", "-s", opaque_setting], "")

    assert state is not None
    assert state.completed_setting_count == 1
    assert not state.explicit_design


def test_attached_setting_before_omitted_design_is_counted_as_a_setting() -> None:
    state = _parse_request_words(cli, ["run", "vivado_synth", "-svivado_synth.nthreads=2"], "")

    assert state is not None
    assert state.completed_setting_count == 1
    assert not state.explicit_design


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_active_option_terminator_disables_setting_completion(shell: str) -> None:
    args = ["run", "nextpnr", "DESIGN", "-s", "nextpnr.docker=tr"]
    state = _parse_request_words(cli, args, "--")

    assert state is not None
    assert state.cursor_after_terminator
    assert not {"true", "docker=true"}.intersection(_complete(shell, args, "--"))


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_design_name_option_keeps_settings_completion_context(shell: str) -> None:
    args = ["run", "vivado_synth", "-s", "vivado_synth.nthreads=2", "--design-name", "ascon"]
    state = _parse_request_words(cli, args, "vivado_synth.fpga.p")

    assert state is not None
    assert state.explicit_design
    assert _complete(shell, args, "vivado_synth.fpga.p") == [
        "vivado_synth.fpga.package=",
        "vivado_synth.fpga.part=",
        "vivado_synth.fpga.pins=",
    ]


def test_node_candidates_are_shell_setting_completions() -> None:
    items = _settings_candidates(["yosys_fpga", "nextpnr"], "nextp", _completion_records())
    assert [item.value for item in items] == ["nextpnr."]
    assert items[0].type == "setting"


@pytest.mark.parametrize("shell", ["zsh", "fish"])
def test_attached_settings_key_candidates_keep_option_prefix(shell: str) -> None:
    assert _complete(shell, ["run", "vivado_synth", "DESIGN"], "-svivado_synth.fpga.p") == [
        "-svivado_synth.fpga.package=",
        "-svivado_synth.fpga.part=",
        "-svivado_synth.fpga.pins=",
    ]


@pytest.mark.parametrize("shell", ["zsh", "fish"])
def test_attached_long_settings_candidates_keep_option_prefix(shell: str) -> None:
    assert _complete(
        shell,
        ["run", "vivado_synth", "DESIGN"],
        "--settings=vivado_synth.fpga.p",
    ) == [
        "--settings=vivado_synth.fpga.package=",
        "--settings=vivado_synth.fpga.part=",
        "--settings=vivado_synth.fpga.pins=",
    ]


@pytest.mark.parametrize("shell", ["zsh", "fish"])
def test_finite_value_keeps_the_node_and_the_setting_key(shell: str) -> None:
    assert _complete(
        shell,
        ["run", "yosys_fpga+nextpnr", "DESIGN", "-s"],
        "nextpnr.docker=tr",
    ) == ["nextpnr.docker=true"]


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_empty_attached_option_value_keeps_path_completion(shell: str, tmp_path) -> None:
    output_dir = tmp_path / "build-artifacts"
    output_dir.mkdir()
    actual = _complete(
        shell,
        ["run", "yosys_fpga", "DESIGN", "-s", "yosys_fpga.docker=true"],
        "--outputs-to=",
    )
    state = _parse_request_words(
        cli,
        ["run", "yosys_fpga", "DESIGN", "-s", "yosys_fpga.docker=true"],
        "--outputs-to=",
    )
    assert state is not None and state.cursor_is_option_value
    assert not any(candidate.endswith("=") for candidate in actual)


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_current_option_name_overrides_prior_settings_context(shell: str) -> None:
    assert "--dry-run" in _complete(
        shell,
        ["run", "nextpnr", "DESIGN", "-s", "nextpnr.nthreads=2"],
        "--dry-run",
    )
    assert "--settings" in _complete(
        shell,
        ["run", "nextpnr", "DESIGN", "-s", "nextpnr.nthreads=2"],
        "--settings",
    )


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_attached_settings_option_completes_hierarchical_keys(shell: str) -> None:
    assert _complete(shell, ["run", "vivado_synth", "design.yaml"], "-svivado_synth.fpga.p") == [
        "-svivado_synth.fpga.package=",
        "-svivado_synth.fpga.part=",
        "-svivado_synth.fpga.pins=",
    ]


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_first_setting_value_completes_when_design_is_omitted(shell: str) -> None:
    candidate = "true" if shell == "bash" else "nextpnr.docker=true"
    assert _complete(shell, ["run", "nextpnr", "-s"], "nextpnr.docker=tr") == [candidate]


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_repeated_settings_options_keep_the_latest_key_context(shell: str) -> None:
    assert _complete(
        shell,
        ["run", "vivado_synth", "design.yaml", "-s", "vivado_synth.nthreads=2", "--settings"],
        "vivado_synth.fpga.p",
    ) == ["vivado_synth.fpga.package=", "vivado_synth.fpga.part=", "vivado_synth.fpga.pins="]


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_scopes_reject_flows_outside_the_request_and_routes(shell: str) -> None:
    assert (
        _complete(
            shell,
            ["run", "yosys_fpga+nextpnr", "design.yaml", "-s"],
            "vivado_impl.debug",
        )
        == []
    )


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_setting_value_completion_uses_finite_enum_values(shell: str) -> None:
    candidate = "A" if shell == "bash" else "bsc.unspecified_to=A"
    assert _complete(shell, ["run", "bsc", "design.yaml", "-s"], "bsc.unspecified_to=A") == [
        candidate
    ]


def test_bash_raw_line_recovers_a_value_split_at_equals(tmp_path) -> None:
    executable = Path(sysconfig.get_path("scripts")) / "xeda"
    assert executable.is_file()
    raw_line = "xeda run nextpnr -s nextpnr.docker=tr"
    env = {
        **os.environ,
        "_XEDA_COMPLETE": "bash_complete",
        "COMP_WORDS": "xeda run nextpnr -s nextpnr.debug tr",
        "COMP_CWORD": "5",
        "COMP_LINE": raw_line,
        "COMP_POINT": str(len(raw_line)),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
    }
    result = subprocess.run(
        [str(executable)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=HANG_GUARD,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["setting,true"]
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("design", "cursor_prefix"),
    [("design.yaml", "vivado_synth.fpga.p"), ("café.yaml", "vivado_synth.fpga.p")],
)
def test_bash_raw_line_uses_byte_cursor_position(design: str, cursor_prefix: str) -> None:
    from click.testing import CliRunner

    line = f"xeda run vivado_synth {design} -s vivado_synth.fpga.package=tail"
    cursor_prefix_line = f"xeda run vivado_synth {design} -s {cursor_prefix}"
    result = CliRunner().invoke(
        cli,
        [],
        env={
            "_XEDA_COMPLETE": "bash_complete",
            "COMP_WORDS": line,
            "COMP_CWORD": "5",
            "COMP_LINE": line,
            "COMP_POINT": str(len(cursor_prefix_line.encode("utf-8"))),
        },
    )
    assert result.exit_code == 0, result.output
    assert "setting,vivado_synth.fpga.pins=" in result.stdout
    assert result.stderr == ""


def test_cold_completion_does_not_import_flow_models_or_runtime_helpers(tmp_path) -> None:
    code = """\
import json, sys
from xeda.cli import cli
try:
    cli.main(args=[], prog_name='xeda', complete_var='_XEDA_COMPLETE')
except SystemExit:
    pass
heavy = ('xeda.design', 'xeda.flow.flow', 'xeda.flow_runner', 'xeda.flows', 'pydantic', 'pint')
print(json.dumps([name for name in heavy if name in sys.modules]))
"""
    env = {
        **os.environ,
        "_XEDA_COMPLETE": "bash_complete",
        "COMP_WORDS": "xeda run nextpnr -s nextpnr.debug tr",
        "COMP_CWORD": "5",
        "COMP_LINE": "xeda run nextpnr -s nextpnr.docker=tr",
        "COMP_POINT": str(len("xeda run nextpnr -s nextpnr.docker=tr")),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
    }
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=HANG_GUARD,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["setting,true", "[]"]
    assert result.stderr == ""


def test_runtime_completion_does_not_mutate_cached_builtin_records(monkeypatch) -> None:
    import sys
    from types import SimpleNamespace

    before = dict(dict(_builtin_flows())["vivado_synth"])
    monkeypatch.setitem(
        sys.modules,
        "xeda.introspect",
        SimpleNamespace(settings_completion_info=lambda flow: {"temporary": {}}),
    )
    runtime = SimpleNamespace(
        registered_flows={
            "vivado_synth": (
                None,
                SimpleNamespace(name="vivado_synth", __name__="TemporaryVivadoSynth"),
            )
        }
    )
    monkeypatch.setitem(sys.modules, "xeda.flow.flow", runtime)
    assert _completion_records()["vivado_synth"]["settings"] == {"temporary": {}}
    assert dict(_builtin_flows())["vivado_synth"] == before


def test_loaded_extension_chain_uses_runtime_route_validation() -> None:
    state = _parse_request_words(
        cli,
        ["run", "t_chain_producer.json_a+t_chain_aliased", "DESIGN", "-s"],
        "",
    )
    assert state is not None
    assert "t_chain_producer" in state.allowed_scopes
    assert "t_chain_aliased" in state.allowed_scopes


def test_runtime_override_uses_runtime_output_qualifiers(monkeypatch) -> None:
    from tests import io_flows
    from xeda.flow import registered_flows

    original_name = io_flows.TChainProducer.name
    original_class_name = io_flows.TChainProducer.__name__
    io_flows.TChainProducer.name = "vivado_synth"
    io_flows.TChainProducer.__name__ = "VivadoSynth"
    monkeypatch.setitem(
        registered_flows,
        "vivado_synth",
        (io_flows.__name__, io_flows.TChainProducer),
    )
    try:
        state = _parse_request_words(
            cli,
            ["run", "vivado_synth.json_a+t_chain_ambiguous_consumer", "DESIGN", "-s"],
            "",
        )
        assert state is not None
        assert "t_chain_ambiguous_consumer" in state.allowed_scopes
    finally:
        io_flows.TChainProducer.name = original_name
        io_flows.TChainProducer.__name__ = original_class_name


def test_runtime_override_does_not_use_stale_catalog_default_scopes(monkeypatch) -> None:
    from tests import io_flows
    from xeda.flow import registered_flows

    original_name = io_flows.TChainProducer.name
    io_flows.TChainProducer.name = "fpga_pack"
    monkeypatch.setitem(
        registered_flows,
        "fpga_pack",
        (io_flows.__name__, io_flows.TChainProducer),
    )
    try:
        state = _parse_request_words(cli, ["run", "fpga_pack", "DESIGN", "-s"], "")
        assert state is not None
        assert state.allowed_scopes == ["fpga_pack"]
    finally:
        io_flows.TChainProducer.name = original_name


def test_settings_completion_matches_multiple_wildcard_segments() -> None:
    records = {
        "mapping": {
            "settings": {
                "a.*": {"children": True, "assignable": True, "dynamic": True},
                "a.*.b": {"children": True},
                "a.*.b.*": {"children": True, "assignable": True, "dynamic": True},
                "a.*.b.*.leaf": {"children": False, "assignable": True},
            }
        }
    }
    assert [
        item.value for item in _settings_candidates(["mapping"], "mapping.a.one.b.two.l", records)
    ] == ["mapping.a.one.b.two.leaf="]


def test_finite_value_completion_resolves_a_concrete_key_through_template() -> None:
    records = {
        "mapping": {
            "settings": {
                "a.*.mode": {
                    "children": False,
                    "assignable": True,
                    "dynamic": True,
                    "values": ["fast", "safe"],
                    "help": "first line\nsecond line\twith tab",
                }
            }
        }
    }
    candidates = _settings_candidates(["mapping"], "mapping.a.key.mode=fa", records)
    assert [item.value for item in candidates] == ["fast"]
    assert candidates[0].help == "first line second line with tab"


def _read_pty_until(fd: int, marker: bytes, timeout: float = 15) -> bytes:
    output = bytearray()
    deadline = time.monotonic() + timeout
    while marker not in output:
        remaining = deadline - time.monotonic()
        assert remaining > 0, output.decode(errors="replace")
        ready, _, _ = select.select([fd], [], [], remaining)
        assert ready, output.decode(errors="replace")
        output.extend(os.read(fd, 4096))
    return bytes(output)


def _interactive_shell(
    shell: str, home: Path, executable_dir: Path
) -> tuple[subprocess.Popen, int]:
    executable = _require_shell(shell)
    command = {
        "bash": [executable, "--noprofile", "--norc", "-i"],
        "zsh": [executable, "-f", "-i"],
        "fish": [executable, "--no-config", "-i"],
    }[shell]
    master, slave = pty.openpty()
    terminal_settings = termios.tcgetattr(slave)
    terminal_settings[3] &= ~termios.ECHO
    termios.tcsetattr(slave, termios.TCSANOW, terminal_settings)
    env = {
        "HOME": str(home),
        "PATH": f"{executable_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "TERM": "dumb",
        "LANG": "C.UTF-8",
        "XDG_CONFIG_HOME": str(home / ".config"),
    }

    def acquire_controlling_terminal() -> None:
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    process = subprocess.Popen(
        command,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        cwd=home,
        env=env,
        close_fds=True,
        preexec_fn=acquire_controlling_terminal,
    )
    os.close(slave)
    os.write(master, b"\n")
    idle_deadline = time.monotonic() + 0.2
    while True:
        ready, _, _ = select.select([master], [], [], 0.05)
        if ready:
            os.read(master, 4096)
            idle_deadline = time.monotonic() + 0.2
        elif time.monotonic() >= idle_deadline:
            break
    terminal_settings = termios.tcgetattr(master)
    terminal_settings[3] &= ~termios.ECHO
    termios.tcsetattr(master, termios.TCSANOW, terminal_settings)
    if shell == "bash":
        setup = (
            "source <(xeda completion bash --stdout); "
            '__xeda_dump() { builtin echo "__BUFFER__${READLINE_LINE}__END__"; }; '
            "bind -x '\"\\C-x\":__xeda_dump'; PS1='READY> '; printf '\\n__SETUP_READY__\\n'\n"
        )
    elif shell == "zsh":
        setup = (
            'autoload -Uz compinit; compinit -u; eval "$(xeda completion zsh --stdout)"; '
            '__xeda_dump() { builtin print -r -- "__BUFFER__${BUFFER}__END__"; }; '
            "zle -N __xeda_dump; bindkey '^X' __xeda_dump; PROMPT='READY> '; print __SETUP_READY__\n"
        )
    else:
        setup = (
            "source (xeda completion fish --stdout | psub); "
            "function __xeda_dump; printf '__BUFFER__%s__END__\\n' (commandline -b); end; "
            "bind \\cx __xeda_dump; function fish_prompt; echo -n 'READY> '; end; echo __SETUP_READY__\n"
        )
    setup_file = home / f"{shell}-completion-setup"
    setup_file.write_text(setup)
    os.write(master, f"source {setup_file}\n".encode())
    _read_pty_until(master, b"__SETUP_READY__")
    return process, master


@pytest.mark.parametrize(
    "shell,source,expected",
    [
        (
            shell,
            "xeda run vivado_synth DESIGN -s vivado_synth.fpga.packa",
            "xeda run vivado_synth DESIGN -s vivado_synth.fpga.package=ZZ",
        )
        for shell in ("bash", "zsh", "fish")
    ]
    + [
        (
            shell,
            "xeda run vivado_synth DESIGN -svivado_synth.fpga.packa",
            "xeda run vivado_synth DESIGN -svivado_synth.fpga.package=ZZ",
        )
        for shell in ("bash", "zsh", "fish")
    ]
    + [
        (
            shell,
            "xeda run ghdl_sim DESIGN -s ghdl_sim.coco",
            "xeda run ghdl_sim DESIGN -s ghdl_sim.cocotb.ZZ",
        )
        for shell in ("bash", "zsh", "fish")
    ]
    + [
        (
            "bash",
            "xeda run nextpnr DESIGN -s nextpnr.docker=tr",
            "xeda run nextpnr DESIGN -s nextpnr.docker=trueZZ",
        )
    ]
    + [
        (
            shell,
            "xeda run yosys_fpga+nextpnr DESIGN -s nextp",
            "xeda run yosys_fpga+nextpnr DESIGN -s nextpnr.ZZ",
        )
        for shell in ("bash", "zsh")
    ]
    + [
        (
            "bash",
            "xeda run yosys_fpga+nextpnr DESIGN -s nextpnr.docker=tr",
            "xeda run yosys_fpga+nextpnr DESIGN -s nextpnr.docker=trueZZ",
        )
    ],
)
@pytest.mark.skipif(pty is None, reason="POSIX pseudo-terminals are not available")
def test_shell_inserts_settings_without_a_space(
    shell: str, source: str, expected: str, tmp_path
) -> None:
    executable = Path(sysconfig.get_path("scripts")) / "xeda"
    assert executable.is_file()
    _require_shell(shell)
    process, fd = _interactive_shell(shell, tmp_path, executable.parent)
    try:
        os.write(fd, source.encode() + b"\tZZ\x18")
        output = _read_pty_until(fd, b"__END__")
        match = re.search(rb"__BUFFER__(.*?)__END__", output, re.DOTALL)
        assert match, output.decode(errors="replace")
        assert match.group(1).decode() == expected
    finally:
        os.killpg(process.pid, 9)
        deadline = time.monotonic() + 2
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert process.poll() is not None, f"{shell} shell did not exit"
        os.close(fd)
