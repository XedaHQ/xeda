"""Lightweight settings completion routed from the original command words."""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any, cast

import click
from click.shell_completion import (
    BashComplete,
    CompletionItem,
    FishComplete,
    ShellComplete,
    ZshComplete,
    get_completion_class,
    split_arg_string,
)

from .flow_catalog import _builtin_flows, _walk_head

_SETTING_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.\[\]-]*\Z")
_OPAQUE_KEY_START = re.compile(r"(?:flows\.[A-Za-z_][A-Za-z0-9_-]*\.)?([A-Za-z_][A-Za-z0-9_]*)\.")


def _looks_like_setting(token: str, opaque_fields: set[str] | None = None) -> bool:
    key, separator, _ = token.partition("=")
    if not separator:
        return False
    if _SETTING_KEY_RE.fullmatch(key):
        return True
    match = _OPAQUE_KEY_START.match(key)
    return bool(match and opaque_fields and match.group(1) in opaque_fields)


@dataclass(frozen=True)
class _RequestState:
    settings_active: bool
    final_flow: str
    explicit_design: bool
    cursor_after_terminator: bool
    cursor_is_option_value: bool
    cursor_is_option_token: bool
    settings_option_count: int
    completed_setting_count: int
    allowed_scopes: list[str]
    setting_prefix: str
    attached_settings_token_prefix: str
    filtered_args: list[str]


def _completion_records() -> dict[str, dict[str, Any]]:
    """Read built-ins and enrich only runtime flows whose modules are already loaded."""
    records = cast(
        dict[str, dict[str, Any]],
        {name: deepcopy(record) for name, record in _builtin_flows()},
    )
    flow_module = sys.modules.get("xeda.flow.flow")
    introspection = sys.modules.get("xeda.introspect")
    info = getattr(introspection, "settings_completion_info", None)
    for _, flow_class in getattr(flow_module, "registered_flows", {}).values():
        if info is None:
            continue
        canonical = flow_class.name
        runtime_record = records.setdefault(
            canonical,
            {
                "aliases": [],
                "class_name": flow_class.__name__,
                "followers": {},
                "default_producers": [],
            },
        )
        runtime_record["aliases"] = list(flow_class.aliases)
        runtime_record["class_name"] = flow_class.__name__
        runtime_record["settings"] = info(flow_class)
    return records


def _opaque_setting_fields() -> set[str]:
    """Return opaque setting roots from the lightweight catalog, without loading flows."""
    return {
        path.split(".", 1)[0]
        for record in _completion_records().values()
        for path, setting in record.get("settings", {}).items()
        if setting.get("opaque_keys")
    }


def _resolve_record(
    token: str, records: dict[str, dict[str, Any]]
) -> tuple[str, dict[str, Any]] | None:
    wanted = token.strip().replace("-", "_").lower()
    class_wanted = "".join(word.title() for word in wanted.split("_"))
    for canonical, record in records.items():
        if (
            wanted
            in {
                canonical.lower(),
                record.get("class_name", "").lower(),
                *(alias.lower() for alias in record.get("aliases", [])),
            }
            or record.get("class_name") == class_wanted
        ):
            return canonical, record
    return None


def _run_command(cli: click.Command) -> click.Command | None:
    if not isinstance(cli, click.Group):
        return None
    return cli.get_command(click.Context(cli), "run")


def _settings_option_info(command: click.Command) -> tuple[set[str], dict[str, click.Option]]:
    settings_flags: set[str] = set()
    flags: dict[str, click.Option] = {}
    for param in command.params:
        if not isinstance(param, click.Option):
            continue
        for flag in (*param.opts, *param.secondary_opts):
            flags[flag] = param
            if param.name == "flow_settings":
                settings_flags.add(flag)
    return settings_flags, flags


def _raw_bash_incomplete(args: list[str], incomplete: str) -> tuple[list[str], str]:
    """Recover a word split by Bash at '=' from the line buffer when it is available."""
    line = os.environ.get("COMP_LINE")
    point = os.environ.get("COMP_POINT")
    if not line or not point:
        return args, incomplete
    line_bytes = line.encode("utf-8", errors="surrogateescape")
    cursor_byte = min(max(int(point), 0), len(line_bytes))
    raw_line = line_bytes[:cursor_byte].decode("utf-8", errors="ignore")
    try:
        raw = split_arg_string(raw_line)
    except ValueError:
        # An open quote is normal while a user types a path. Preserve the active text and let
        # Click's shell-specific parser handle it.
        return args, incomplete
    if not raw:
        return args, incomplete
    if raw_line[-1:].isspace():
        return raw[1:], ""
    # COMP_LINE includes the executable and complete words. Its last token is the active word.
    return raw[1:-1], raw[-1]


def _parse_request_words(
    cli: click.Command, args: list[str], incomplete: str
) -> _RequestState | None:
    """Recover the flow context after removing settings from the declared run arguments."""
    command = _run_command(cli)
    if command is None or not args or args[0] != "run":
        return None
    settings_flags, option_flags = _settings_option_info(command)
    request = [*args[1:], incomplete]
    filtered_args: list[str] = []
    settings_active = False
    settings_option_count = 0
    completed_setting_count = 0
    after_terminator = False
    cursor_after_terminator = False
    cursor_is_option_value = False
    cursor_is_option_token = False
    explicit_design = False
    last_setting_prefix = ""
    attached_settings_token_prefix = ""
    opaque_fields: set[str] | None = None

    def is_setting_value(token: str) -> bool:
        nonlocal opaque_fields
        if _looks_like_setting(token):
            return True
        key, separator, _ = token.partition("=")
        if not separator or _OPAQUE_KEY_START.match(key) is None:
            return False
        if opaque_fields is None:
            opaque_fields = _opaque_setting_fields()
        return _looks_like_setting(token, opaque_fields)

    index = 0
    while index < len(request):
        token = request[index]
        is_current = index == len(request) - 1
        if token == "--":
            after_terminator = True
            cursor_after_terminator = True
            if not is_current:
                filtered_args.append(token)
            index += 1
            continue
        matched_flag = token
        attached_value = ""
        has_attached_value = False
        if token.startswith("--") and "=" in token:
            matched_flag, attached_value = token.split("=", 1)
            has_attached_value = True
        elif token.startswith("-s") and not token.startswith("--") and token != "-s":
            matched_flag, attached_value = "-s", token[2:]
            has_attached_value = True
        if (
            is_current
            and token.startswith("-")
            and not after_terminator
            and (matched_flag not in settings_flags or not has_attached_value)
        ):
            cursor_is_option_token = True
        if is_current and matched_flag in settings_flags and has_attached_value:
            attached_settings_token_prefix = (
                f"{matched_flag}=" if token.startswith("--") else matched_flag
            )
        param = option_flags.get(matched_flag) if not after_terminator else None
        if (
            param is not None
            and matched_flag in settings_flags
            and index == len(request) - 1
            and not has_attached_value
        ):
            # The cursor is completing the option name itself, not its first setting key.
            param = None
        if param is not None:
            is_settings = matched_flag in settings_flags
            if is_settings:
                settings_active = True
                settings_option_count += 1
                if has_attached_value:
                    last_setting_prefix = attached_value
                    if not is_current and is_setting_value(attached_value):
                        completed_setting_count += 1
                elif is_current:
                    # `--settings<TAB>` is still completing an option name.
                    param = None
            if param is None:
                index += 1
                continue
            if not getattr(param, "is_flag", False):
                if param.name == "design_file_opt":
                    explicit_design = True
                elif param.name == "design_name":
                    explicit_design = True
                if has_attached_value:
                    if is_current and not is_settings:
                        cursor_is_option_value = True
                    if not is_settings and not is_current:
                        filtered_args.append(token)
                    index += 1
                    continue
                if is_settings:
                    # This Click option accepts KEY=VALUE groups. Ignore only tokens owned by
                    # that option; the next ordinary positional remains the design path.
                    index += 1
                    while index < len(request):
                        candidate = request[index]
                        if candidate == "--":
                            after_terminator = True
                            cursor_after_terminator = True
                            if index < len(request) - 1:
                                filtered_args.append(candidate)
                            index += 1
                            break
                        if candidate.startswith("-"):
                            break
                        if not is_setting_value(candidate):
                            if index == len(request) - 1:
                                last_setting_prefix = candidate
                            else:
                                filtered_args.append(candidate)
                            index += 1
                            break
                        last_setting_prefix = candidate
                        if index < len(request) - 1:
                            completed_setting_count += 1
                        index += 1
                    continue
                value_count = max(1, param.nargs)
                if index < len(request) - 1 <= index + value_count:
                    cursor_is_option_value = True
                if not is_current:
                    filtered_args.append(token)
                    filtered_args.extend(
                        request[index + 1 : min(index + value_count + 1, len(request) - 1)]
                    )
                index += 1 + value_count
                continue
            if not is_current:
                filtered_args.append(token)
            index += 1
            continue
        if token.startswith("-") and not after_terminator:
            if not is_current:
                filtered_args.append(token)
            index += 1
            continue
        if is_current:
            # The current word is a setting key/value only after an explicit settings flag.
            if settings_active:
                last_setting_prefix = token
                index += 1
                continue
        if not is_current:
            filtered_args.append(token)
        index += 1

    # Run the original complete words through Click's declared parser after removing only the
    # settings option and its KEY=VALUE items. This preserves option arity and positional order.
    completion_args = ["run", *filtered_args]
    try:
        parser_ctx = click.Context(command, resilient_parsing=True)
        parsed_options, _, _ = command.make_parser(parser_ctx).parse_args(filtered_args.copy())
    except click.ClickException:
        return None
    chain_text = parsed_options.get("flow")
    if not isinstance(chain_text, str) or not chain_text:
        return None
    chain_tokens = chain_text.split("+")
    records = _completion_records()
    runtime_authority_loaded = "xeda.flow.flow" in sys.modules
    chain_head: list[tuple[str, str | None]] = []
    for part in chain_tokens:
        name, dot, output = part.partition(".")
        resolved = _resolve_record(name, records)
        if resolved is None:
            return None
        canonical, record = resolved
        if dot and not runtime_authority_loaded and output not in record["followers"]:
            return None
        chain_head.append((canonical, output if dot else None))
    runtime_request = None
    default_graph_fn: Callable[[Any], Any] | None = None
    walked: set[str] | None = None
    used_classes: set[Any] = set()
    if runtime_authority_loaded:
        # When models are already loaded, the normal request parser remains authoritative for
        # built-ins, extensions, and registry overrides. Cold requests use the static catalog.
        try:
            from .flow_runner.chains import default_graph as default_graph_fn
            from .flow_runner.chains import parse_request

            runtime_request = parse_request(chain_text)
        except Exception:
            return None
        used_classes = {element.flow_class for element in runtime_request.elements}
        for link in runtime_request.links:
            used_classes.update(link.through)
        walked = {flow.name for flow in used_classes}
    else:
        walked = _walk_head(cast(Any, records), chain_head)
    if walked is None:
        return None
    final_name = chain_tokens[-1].split(".", 1)[0]
    final = _resolve_record(final_name, records)
    if final is None:
        return None
    design_params = {
        param.name: param
        for param in command.params
        if param.name in {"design_file", "design_file_opt", "design_name"}
    }
    unset = getattr(click.core, "UNSET", None)
    explicit_design = explicit_design or any(
        parsed_options.get(name, unset) is not unset and parsed_options.get(name) != param.default
        for name, param in design_params.items()
    )
    if runtime_request is not None:
        for flow_class in used_classes:
            assert default_graph_fn is not None
            walked.update(flow.name for flow in default_graph_fn(flow_class).flows())
        allowed_scopes = walked
    else:
        allowed_scopes = walked | {
            producer
            for flow in walked
            for producer in records.get(flow, {}).get("default_producers", [])
        }
    return _RequestState(
        settings_active=settings_active,
        final_flow=final[0],
        explicit_design=explicit_design,
        cursor_after_terminator=cursor_after_terminator,
        cursor_is_option_value=cursor_is_option_value,
        cursor_is_option_token=cursor_is_option_token,
        settings_option_count=settings_option_count,
        completed_setting_count=completed_setting_count,
        allowed_scopes=sorted(allowed_scopes),
        setting_prefix=last_setting_prefix,
        attached_settings_token_prefix=attached_settings_token_prefix,
        filtered_args=completion_args,
    )


def _flow_candidates(
    chain: list[str], prefix: str, records: dict[str, dict[str, Any]]
) -> list[str]:
    candidates = set(chain)
    shown: list[str] = []
    for canonical in candidates:
        record = records.get(canonical)
        spellings = [canonical]
        if record:
            spellings.extend([record["class_name"], *record["aliases"]])
        shown.extend(value for value in spellings if value.startswith(prefix))
    return sorted(set(shown))


def _settings_candidates(
    flow: str, chain: list[str], prefix: str, records: dict[str, dict[str, Any]]
) -> list[CompletionItem[str]] | None:
    scope_prefix = ""
    if prefix.startswith("flows."):
        rest = prefix[len("flows.") :]
        if "." not in rest:
            flow_prefix = rest
            names = _flow_candidates(chain, flow_prefix, records)
            return [CompletionItem(f"flows.{name}.", type="setting") for name in names]
        spelling, key_prefix = rest.split(".", 1)
        resolved = _resolve_record(spelling, records)
        if resolved is None or resolved[0] not in set(_flow_candidates(chain, "", records)):
            return []
        flow = resolved[0]
        scope_prefix = f"flows.{spelling}."
        prefix = key_prefix

    record = records.get(flow)
    if not record:
        return []
    index: dict[str, dict[str, Any]] = record.get("settings", {})
    if "=" in prefix:
        key, _, value_prefix = prefix.partition("=")
        item = index.get(key)
        if item is None:
            item = next(
                (
                    candidate
                    for template, candidate in index.items()
                    if _matches_template(template, key)
                ),
                None,
            )
        if not item or not item.get("values"):
            return []
        return [
            CompletionItem(value, help=_one_line_help(item.get("help")))
            for value in item["values"]
            if value.startswith(value_prefix)
        ]

    parent, dot, tail = prefix.rpartition(".")
    prefix_node = f"{parent}." if dot else ""
    children: dict[str, dict[str, Any]] = {}
    for path, item in index.items():
        if path.count(".") != prefix_node.count("."):
            continue
        if parent:
            path_parent, _, child = path.rpartition(".")
            if not _matches_template(path_parent, parent):
                continue
        else:
            child = path
        if child == "*":
            continue
        if child.startswith(tail):
            children[child] = item
    result = []
    for name, item in sorted(children.items()):
        path = scope_prefix + prefix_node + name
        if item.get("children"):
            help_text = _one_line_help(item.get("help"))
            result.append(CompletionItem(path + ".", type="setting", help=help_text))
        if item.get("assignable"):
            help_text = _one_line_help(item.get("help"))
            result.append(CompletionItem(path + "=", type="setting", help=help_text))
    return result


def _one_line_help(help_text: str | None) -> str | None:
    return " ".join(help_text.replace("\t", " ").splitlines()) if help_text else None


def _matches_template(template: str, concrete: str) -> bool:
    template_parts = template.split(".")
    concrete_parts = concrete.split(".")
    return len(template_parts) == len(concrete_parts) and all(
        pattern == "*" or pattern == value for pattern, value in zip(template_parts, concrete_parts)
    )


class SettingsCompletionMixin:
    def get_completions(
        self: SettingsCompletionMixin, args: list[str], incomplete: str
    ) -> list[CompletionItem[str]]:
        adapter = cast(ShellComplete, self)
        if getattr(adapter, "shell_name", None) == "bash":
            args, incomplete = _raw_bash_incomplete(args, incomplete)
        state = _parse_request_words(adapter.cli, args, incomplete)
        # With an omitted design, a later token can be a path. Keep Click's path candidates
        # unless the user repeated the settings option, which makes the intent explicit.
        if (
            state
            and state.settings_active
            and not state.cursor_after_terminator
            and not state.cursor_is_option_value
            and not state.cursor_is_option_token
            and (
                state.explicit_design
                or state.settings_option_count > 1
                or state.completed_setting_count == 0
            )
        ):
            items = _settings_candidates(
                state.final_flow,
                state.allowed_scopes,
                state.setting_prefix,
                _completion_records(),
            )
            if items is not None:
                if "=" in state.setting_prefix and getattr(adapter, "shell_name", None) == "bash":
                    return [
                        CompletionItem(item.value, type="setting", help=item.help) for item in items
                    ]
                if "=" in state.setting_prefix and getattr(adapter, "shell_name", None) != "bash":
                    key, _, _ = state.setting_prefix.partition("=")
                    return [
                        CompletionItem(
                            f"{state.attached_settings_token_prefix}{key}={item.value}",
                            help=item.help,
                        )
                        for item in items
                    ]
                if state.attached_settings_token_prefix and (
                    getattr(adapter, "shell_name", None) != "bash"
                    or state.attached_settings_token_prefix == "-s"
                ):
                    return [
                        CompletionItem(
                            f"{state.attached_settings_token_prefix}{item.value}",
                            type=item.type,
                            help=item.help,
                        )
                        for item in items
                    ]
                return items
        if state and state.cursor_is_option_token:
            state = replace(state, settings_active=False)
        return ShellComplete.get_completions(
            adapter, state.filtered_args if state else args, incomplete
        )


class SettingsBashComplete(SettingsCompletionMixin, BashComplete):
    shell_name = "bash"
    source_template = BashComplete.source_template.replace(
        'COMP_WORDS="${COMP_WORDS[*]}" COMP_CWORD=$COMP_CWORD %(complete_var)s=bash_complete $1',
        'COMP_WORDS="${COMP_WORDS[*]}" COMP_CWORD=$COMP_CWORD COMP_LINE="$COMP_LINE" COMP_POINT=$COMP_POINT %(complete_var)s=bash_complete $1',
    ).replace(
        "        elif [[ $type == 'plain' ]]; then\n            COMPREPLY+=($value)",
        "        elif [[ $type == 'plain' ]]; then\n            COMPREPLY+=($value)\n        elif [[ $type == 'setting' ]]; then\n            compopt -o nospace\n            COMPREPLY+=($value)",
    )


class SettingsZshComplete(SettingsCompletionMixin, ZshComplete):
    shell_name = "zsh"
    source_template = (
        ZshComplete.source_template.replace(
            "    local -a completions_with_descriptions",
            "    local -a completions_with_descriptions\n    local -a setting_completions",
        )
        .replace(
            'if [[ "$type" == "plain" ]]; then',
            'if [[ "$type" == "setting" ]]; then\n            setting_completions+=("$key")\n        elif [[ "$type" == "plain" ]]; then',
        )
        .replace(
            '    if [ -n "$completions" ]; then\n        compadd -U -V unsorted -a completions\n    fi',
            '    if [ -n "$completions" ]; then\n        compadd -U -V unsorted -a completions\n    fi\n    if [ -n "$setting_completions" ]; then\n        compadd -S \'\' -U -V unsorted -a setting_completions\n    fi',
        )
    )


class SettingsFishComplete(SettingsCompletionMixin, FishComplete):
    shell_name = "fish"
    source_template = FishComplete.source_template.replace(
        'else if test $metadata[1] = "plain";\n            echo $metadata[2];',
        'else if test $metadata[1] = "plain";\n            echo $metadata[2];\n        else if test $metadata[1] = "setting";\n            echo $metadata[2];',
    )


_COMPLETION_CLASSES: dict[str, type[ShellComplete]] = {
    "bash": SettingsBashComplete,
    "zsh": SettingsZshComplete,
    "fish": SettingsFishComplete,
}


def completion_class(shell: str) -> type[ShellComplete] | None:
    """Return Xeda's adapter for a shell, retaining Click's supported-shell behavior."""
    return _COMPLETION_CLASSES.get(shell) or get_completion_class(shell)
