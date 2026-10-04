"""A boolean is `true` or `false`: no other text is read as one, and YAML 1.1 words say what to write."""

import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from xeda.cli import cli
from xeda.dataclass import ValidationError, _is_boolean_annotation, validation_errors
from xeda.design import Design, DesignValidationError
from xeda.flow import FlowSettingsError
from xeda.flows import VivadoSynth
from xeda.utils import try_convert_to_primitives

from .settings_samples import flow_classes
from .tool_utils import use_fake_tools

SQRT = Path(__file__).parent.parent / "examples" / "vhdl" / "sqrt" / "sqrt.yaml"

TRUE_WORDS = ["yes", "on", "y", "Yes", "ON"]
FALSE_WORDS = ["no", "off", "n", "No", "OFF"]


def _design(tmp_path, body):
    path = tmp_path / "probe.yaml"
    path.write_text(f"name: probe\nrtl: {{sources: []}}\n{body}\n")
    return Design.from_file(path)


@pytest.mark.parametrize(
    "word,fix", [*[(w, "true") for w in TRUE_WORDS], *[(w, "false") for w in FALSE_WORDS]]
)
def test_a_design_boolean_given_a_yaml_11_word_says_what_to_write(tmp_path, word, fix):
    with pytest.raises(DesignValidationError) as raised:
        _design(tmp_path, f"language: {{vhdl: {{synopsys: {word}}}}}")
    assert f"`{word}` is text, not a boolean: write `{fix}`" in str(raised.value)
    assert "language.vhdl.synopsys" in str(raised.value)


@pytest.mark.parametrize("value", ['"true"', "true", "false"])
def test_text_true_and_false_are_still_a_boolean(tmp_path, value):
    # a command line has no other way to write one
    expected = value.strip('"') == "true"
    assert (
        _design(tmp_path, f"language: {{vhdl: {{synopsys: {value}}}}}").language.vhdl.synopsys
        is expected
    )


def test_a_design_text_that_is_not_a_boolean_names_both_spellings(tmp_path):
    with pytest.raises(DesignValidationError) as raised:
        _design(tmp_path, 'language: {vhdl: {synopsys: "maybe"}}')
    assert "`maybe` is text, not a boolean: write `true` or `false`" in str(raised.value)


@pytest.mark.parametrize("word,fix", [("yes", "true"), ("off", "false")])
def test_a_yaml_11_word_for_a_non_boolean_field_says_what_xeda_yaml_wants(tmp_path, word, fix):
    with pytest.raises(DesignValidationError) as raised:
        _design(tmp_path, f"tb: {{cocotb: {word}}}")
    assert f"`{word}` is text in xeda YAML (YAML 1.2): write `{fix}`" in str(raised.value)


def test_a_number_for_a_text_field_says_to_quote_it(tmp_path):
    path = tmp_path / "probe.yaml"
    path.write_text("name: probe\nrtl: {sources: [], top: 010}\n")
    with pytest.raises(DesignValidationError) as raised:
        Design.from_file(path)
    assert 'write `"10"` for text' in str(raised.value)


@pytest.mark.parametrize(
    "word,fix", [("yes", "true"), ("on", "true"), ("n", "false"), ("off", "false")]
)
def test_a_flow_boolean_given_a_yaml_11_word_is_a_settings_error(word, fix):
    with pytest.raises(FlowSettingsError) as raised:
        VivadoSynth.Settings.from_input({"fpga": "xc7a12tcsg325-1", "print_commands": word})
    assert f"`{word}` is text, not a boolean: write `{fix}`" in str(raised.value)
    assert "print_commands" in str(raised.value)


def _boolean_fields():
    # a list: pytest 10 refuses a generator, which it could only run once
    return [
        pytest.param(flow, name, id=f"{flow.__name__}.{name}")
        for flow, _ in flow_classes()
        for name, info in flow.Settings.model_fields.items()
        if _is_boolean_annotation(info.annotation)
    ]


@pytest.mark.parametrize("flow,name", _boolean_fields())
def test_every_boolean_setting_of_every_flow_refuses_text(flow, name):
    with pytest.raises(ValidationError) as raised:
        flow.Settings.model_validate({name: "yes"})
    assert [msg for loc, msg, *_ in validation_errors(raised.value.errors()) if loc == name] == [
        "`yes` is text, not a boolean: write `true`"
    ]


def test_assigning_a_boolean_word_fails_as_constructing_does():
    settings = VivadoSynth.Settings.from_input({"fpga": "xc7a12tcsg325-1"})
    with pytest.raises(ValidationError) as raised:
        settings.debug = "on"
    assert validation_errors(raised.value.errors())[0][1] == (
        "`on` is text, not a boolean: write `true`"
    )
    assert settings.debug is False


def test_a_yaml_11_word_for_a_non_boolean_flow_setting_says_what_to_write():
    with pytest.raises(FlowSettingsError) as raised:
        VivadoSynth.Settings.from_input({"fpga": "xc7a12tcsg325-1", "nthreads": "on"})
    assert "`on` is text in xeda YAML (YAML 1.2): write `true`" in str(raised.value)


@pytest.mark.parametrize("word", ["true", "True", "TRUE", "false", "False"])
def test_try_convert_to_primitives_converts_only_true_and_false(word):
    assert try_convert_to_primitives(word) is (word.lower() == "true")


@pytest.mark.parametrize("word", ["yes", "no", "on", "off", "y", "n"])
def test_try_convert_to_primitives_leaves_other_words_as_text(word):
    assert try_convert_to_primitives(word) == word


@pytest.mark.parametrize("setting", ["debug=yes", "debug=on", "dockerized=n"])
def test_dash_s_given_a_boolean_word_says_what_to_write(tmp_path, monkeypatch, setting):
    use_fake_tools(monkeypatch)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        cli,
        ["run", "vivado_synth", "-s", "fpga.part=xc7a12tcsg325-1", setting, str(SQRT), "--json"],
    )
    document = json.loads(result.stdout)
    assert result.exit_code != 0
    assert document["success"] is False
    key, word = setting.split("=")
    fix = "true" if word in ("yes", "on") else "false"
    assert f"`{word}` is text, not a boolean: write `{fix}`" in document["error"]["message"]


@pytest.mark.parametrize("setting", ["quiet=true", "quiet=TRUE", "quiet=false"])
def test_dash_s_true_and_false_set_a_boolean(tmp_path, monkeypatch, setting):
    use_fake_tools(monkeypatch)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        cli,
        ["run", "vivado_synth", "-s", "fpga.part=xc7a12tcsg325-1", setting, str(SQRT), "--json"],
    )
    assert result.exit_code == 0, result.stdout


@pytest.mark.parametrize("number", [0, 1, 2, 1.0])
def test_a_number_is_not_a_boolean_anywhere(tmp_path, number):
    message = f"`{number}` is a number, not a boolean: write `true` or `false`"
    with pytest.raises(FlowSettingsError, match=re.escape(message)):
        VivadoSynth.Settings.from_input({"fpga": "xc7a12tcsg325-1", "debug": number})
    settings = VivadoSynth.Settings.from_input({"fpga": "xc7a12tcsg325-1"})
    with pytest.raises(ValidationError, match=re.escape(message)):
        settings.dockerized = number
    with pytest.raises(DesignValidationError, match=re.escape(message)):
        _design(tmp_path, f"language: {{vhdl: {{synopsys: {number}}}}}")
