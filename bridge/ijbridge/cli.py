from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

from .config import (
    BridgeConfig,
    get_connection_file_path,
    get_daemon_socket_path,
    load_bridge_config,
)
from .daemon import DaemonError, daemon_ping, daemon_request_call, run_daemon_server
from .discovery import discover_intellij
from .installer import ensure_plugin_installed, launch_intellij
from .rpc import IntelliJRpcClient


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _resolve_timeout_seconds(args: argparse.Namespace, config: BridgeConfig) -> float:
    if getattr(args, "timeout", None) is not None:
        return float(args.timeout)
    return config.request_timeout_seconds


def _resolve_connection_path(args: argparse.Namespace, config: BridgeConfig) -> Path:
    connection_file = getattr(args, "connection_file", None)
    if connection_file:
        return Path(connection_file).expanduser()
    return get_connection_file_path(config)


def _resolve_daemon_socket(args: argparse.Namespace, config: BridgeConfig) -> Path:
    daemon_socket = getattr(args, "daemon_socket", None)
    if daemon_socket:
        return Path(daemon_socket).expanduser()
    return get_daemon_socket_path(config)


def _resolve_client(args: argparse.Namespace) -> IntelliJRpcClient:
    config = load_bridge_config(Path.cwd())
    timeout_seconds = _resolve_timeout_seconds(args, config)
    connection_path = _resolve_connection_path(args, config)

    return IntelliJRpcClient.from_connection_file(
        connection_path,
        wait_seconds=timeout_seconds,
        timeout_seconds=timeout_seconds,
    )


def _is_daemon_disabled_by_env() -> bool:
    raw = os.getenv("INTELLIJ_BRIDGE_DISABLE_DAEMON", "") or os.getenv(
        "OPENCODE_IDEA_DISABLE_DAEMON", ""
    )
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _wait_for_bridge_ready(
    connection_path: Path,
    timeout_seconds: float,
    *,
    min_connection_mtime: float | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    require_fresh_connection = min_connection_mtime is not None

    while time.monotonic() < deadline:
        remaining = max(0.1, deadline - time.monotonic())
        try:
            wait_seconds = remaining
            if require_fresh_connection:
                wait_seconds = min(2.0, remaining)

            client = IntelliJRpcClient.from_connection_file(
                connection_path=connection_path,
                wait_seconds=wait_seconds,
                timeout_seconds=min(remaining, 10.0),
                min_connection_mtime=min_connection_mtime
                if require_fresh_connection
                else None,
            )
            health = client.health()
            if isinstance(health, dict) and health.get("status") == "ok":
                return health
        except TimeoutError as exc:
            last_error = exc

            if require_fresh_connection:
                require_fresh_connection = False
                try:
                    fallback_client = IntelliJRpcClient.from_connection_file(
                        connection_path=connection_path,
                        wait_seconds=min(1.0, remaining),
                        timeout_seconds=min(remaining, 10.0),
                    )
                    health = fallback_client.health()
                    if isinstance(health, dict) and health.get("status") == "ok":
                        return health
                except Exception as fallback_exc:
                    last_error = fallback_exc
        except Exception as exc:
            last_error = exc

        time.sleep(1.0)

    if last_error is not None:
        raise RuntimeError(
            f"Timed out waiting for bridge readiness at {connection_path}: {last_error}"
        ) from last_error

    raise RuntimeError(f"Timed out waiting for bridge readiness at {connection_path}")


def _cmd_discover(_: argparse.Namespace) -> int:
    config = load_bridge_config(Path.cwd())
    installs = discover_intellij(explicit_app_path=config.intellij_app_path)
    _print_json(
        {
            "count": len(installs),
            "installs": [install.to_dict() for install in installs],
        }
    )
    return 0


def _cmd_launch(args: argparse.Namespace) -> int:
    config = load_bridge_config(Path.cwd())

    selected_app = args.app_path
    if selected_app is None:
        installs = discover_intellij(explicit_app_path=config.intellij_app_path)
        if not installs:
            raise RuntimeError("No IntelliJ installations found")
        selected_app = installs[0].app_path

    launch_started_at = time.time()
    result = launch_intellij(
        app_path=selected_app,
        project_path=args.project_path,
        extra_args=args.args,
        gui=bool(args.gui),
    )

    if bool(args.wait_ready):
        timeout_seconds = _resolve_timeout_seconds(args, config)
        connection_path = _resolve_connection_path(args, config)
        health = _wait_for_bridge_ready(
            connection_path=connection_path,
            timeout_seconds=timeout_seconds,
            min_connection_mtime=launch_started_at,
        )
        payload = result.to_dict()
        payload["ready"] = True
        payload["health"] = health
        _print_json(payload)
        return 0

    _print_json(result.to_dict())
    return 0


def _cmd_install_plugin(args: argparse.Namespace) -> int:
    config = load_bridge_config(Path.cwd())
    resolved_plugins_path = args.plugins_path or config.plugins_path

    result = ensure_plugin_installed(
        plugin_zip=args.plugin_zip,
        plugins_path=resolved_plugins_path,
        app_path=args.app_path,
    )
    _print_json(result.to_dict())
    return 0


def _cmd_connection_file(_: argparse.Namespace) -> int:
    config = load_bridge_config(Path.cwd())
    _print_json({"connectionFile": str(get_connection_file_path(config))})
    return 0


def _cmd_health(args: argparse.Namespace) -> int:
    client = _resolve_client(args)
    result = client.health()
    _print_json(result)
    return 0


def _cmd_open_file(args: argparse.Namespace) -> int:
    client = _resolve_client(args)
    result = client.open_file(
        path=args.path,
        focus=bool(args.focus),
        project_key=args.project_key,
    )
    _print_json(result)
    return 0


def _cmd_get_text(args: argparse.Namespace) -> int:
    client = _resolve_client(args)
    result = client.get_document_text(
        path=args.path,
        project_key=args.project_key,
    )
    _print_json(result)
    return 0


def _cmd_apply_edits(args: argparse.Namespace) -> int:
    client = _resolve_client(args)
    edits = json.loads(args.edits_json)
    if not isinstance(edits, list):
        raise ValueError("--edits-json must be a JSON array")
    if not all(isinstance(edit, dict) for edit in edits):
        raise ValueError("--edits-json must be an array of JSON objects")

    result = client.apply_text_edits(
        path=args.path,
        edits=edits,
        project_key=args.project_key,
    )
    _print_json(result)
    return 0


def _cmd_get_caret(args: argparse.Namespace) -> int:
    client = _resolve_client(args)
    result = client.get_caret_state(
        path=args.path,
        project_key=args.project_key,
    )
    _print_json(result)
    return 0


def _cmd_set_caret(args: argparse.Namespace) -> int:
    client = _resolve_client(args)

    has_offset = args.offset is not None
    has_line_character = args.line is not None or args.character is not None
    if not has_offset and not (args.line is not None and args.character is not None):
        raise ValueError("set-caret requires --offset or both --line and --character")
    if has_offset and has_line_character:
        raise ValueError("set-caret accepts either --offset or --line/--character")

    if (args.selection_start is None) != (args.selection_end is None):
        raise ValueError(
            "selection requires both --selection-start and --selection-end"
        )

    result = client.set_caret_state(
        path=args.path,
        project_key=args.project_key,
        offset=args.offset,
        line=args.line,
        character=args.character,
        selection_start=args.selection_start,
        selection_end=args.selection_end,
    )
    _print_json(result)
    return 0


def _cmd_list_actions(args: argparse.Namespace) -> int:
    client = _resolve_client(args)
    result = client.list_actions(
        filter_text=args.filter,
        include_hidden=bool(args.include_hidden),
        limit=args.limit,
    )
    _print_json(result)
    return 0


def _cmd_perform_action(args: argparse.Namespace) -> int:
    client = _resolve_client(args)
    result = client.perform_action(
        action_id=args.action_id,
        project_key=args.project_key,
        path=args.path,
        focus=bool(args.focus),
    )
    _print_json(result)
    return 0


def _cmd_find_in_project(args: argparse.Namespace) -> int:
    client = _resolve_client(args)
    result = client.find_in_project(
        query=args.query,
        project_key=args.project_key,
        case_sensitive=bool(args.case_sensitive),
        limit=args.limit,
    )
    _print_json(result)
    return 0


def _cmd_resolve_symbol(args: argparse.Namespace) -> int:
    client = _resolve_client(args)
    result = client.resolve_symbol_at(
        path=args.path,
        offset=args.offset,
        project_key=args.project_key,
    )
    _print_json(result)
    return 0


def _cmd_rename_symbol(args: argparse.Namespace) -> int:
    client = _resolve_client(args)
    result = client.rename_symbol(
        path=args.path,
        offset=args.offset,
        new_name=args.new_name,
        project_key=args.project_key,
    )
    _print_json(result)
    return 0


def _cmd_list_run_configs(args: argparse.Namespace) -> int:
    client = _resolve_client(args)
    result = client.list_run_configurations(project_key=args.project_key)
    _print_json(result)
    return 0


def _cmd_run_config(args: argparse.Namespace) -> int:
    client = _resolve_client(args)
    result = client.run_configuration(name=args.name, project_key=args.project_key)
    _print_json(result)
    return 0


def _cmd_run_tests(args: argparse.Namespace) -> int:
    client = _resolve_client(args)
    result = client.run_tests(
        project_key=args.project_key,
        configuration_name=args.configuration_name,
    )
    _print_json(result)
    return 0


def _cmd_diagnostics(args: argparse.Namespace) -> int:
    client = _resolve_client(args)
    result = client.get_diagnostics(
        path=args.path,
        project_key=args.project_key,
        severity=args.severity,
        limit=args.limit,
    )
    _print_json(result)
    return 0


def _cmd_completions(args: argparse.Namespace) -> int:
    client = _resolve_client(args)
    result = client.get_completions(
        path=args.path,
        offset=args.offset,
        project_key=args.project_key,
        limit=args.limit,
    )
    _print_json(result)
    return 0


def _cmd_hover(args: argparse.Namespace) -> int:
    client = _resolve_client(args)
    result = client.get_hover(
        path=args.path,
        offset=args.offset,
        project_key=args.project_key,
    )
    _print_json(result)
    return 0


def _cmd_signature_help(args: argparse.Namespace) -> int:
    client = _resolve_client(args)
    result = client.get_signature_help(
        path=args.path,
        offset=args.offset,
        project_key=args.project_key,
    )
    _print_json(result)
    return 0


def _cmd_code_actions(args: argparse.Namespace) -> int:
    client = _resolve_client(args)
    result = client.get_code_actions(
        path=args.path,
        offset=args.offset,
        project_key=args.project_key,
        limit=args.limit,
    )
    _print_json(result)
    return 0


def _cmd_apply_code_action(args: argparse.Namespace) -> int:
    client = _resolve_client(args)
    result = client.apply_code_action(
        action_id=args.action_id,
        path=args.path,
        offset=args.offset,
        project_key=args.project_key,
    )
    _print_json(result)
    return 0


def _cmd_format_file(args: argparse.Namespace) -> int:
    client = _resolve_client(args)
    result = client.format_file(
        path=args.path,
        project_key=args.project_key,
    )
    _print_json(result)
    return 0


def _cmd_format_range(args: argparse.Namespace) -> int:
    client = _resolve_client(args)
    result = client.format_range(
        path=args.path,
        start_line=args.start_line,
        start_character=args.start_character,
        end_line=args.end_line,
        end_character=args.end_character,
        project_key=args.project_key,
    )
    _print_json(result)
    return 0


def _cmd_optimize_imports(args: argparse.Namespace) -> int:
    client = _resolve_client(args)
    result = client.optimize_imports(
        path=args.path,
        project_key=args.project_key,
    )
    _print_json(result)
    return 0


def _cmd_unsafe_status(args: argparse.Namespace) -> int:
    client = _resolve_client(args)
    result = client.unsafe_get_status()
    _print_json(result)
    return 0


def _cmd_unsafe_invoke(args: argparse.Namespace) -> int:
    client = _resolve_client(args)

    if args.target_handle:
        target: dict[str, Any] = {"handle": args.target_handle}
    else:
        target = {"className": args.target_class}

    parsed_args = json.loads(args.args_json) if args.args_json else []
    if not isinstance(parsed_args, list):
        raise ValueError("--args-json must be a JSON array")

    capability_tokens = [token for token in args.capability_token if token]
    result = client.unsafe_invoke(
        target=target,
        method=args.method,
        args=parsed_args,
        return_handle=bool(args.return_handle),
        capability_tokens=capability_tokens,
    )
    _print_json(result)
    return 0


def _cmd_daemon_run(args: argparse.Namespace) -> int:
    config = load_bridge_config(Path.cwd())
    socket_path = _resolve_daemon_socket(args, config)
    run_daemon_server(socket_path)
    return 0


def _cmd_daemon_ping(args: argparse.Namespace) -> int:
    config = load_bridge_config(Path.cwd())
    timeout_seconds = _resolve_timeout_seconds(args, config)
    socket_path = _resolve_daemon_socket(args, config)
    result = daemon_ping(socket_path=socket_path, timeout_seconds=timeout_seconds)
    _print_json(result)
    return 0


def _cmd_call(args: argparse.Namespace) -> int:
    config = load_bridge_config(Path.cwd())
    timeout_seconds = _resolve_timeout_seconds(args, config)
    connection_path = _resolve_connection_path(args, config)
    daemon_socket_path = _resolve_daemon_socket(args, config)

    request = json.loads(args.json)
    if not isinstance(request, dict):
        raise ValueError("--json must be an object")

    method = request.get("method")
    if not isinstance(method, str) or not method:
        raise ValueError("RPC payload requires a non-empty method")

    params = request.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    request_id = request.get("id")
    if request_id is not None and not isinstance(request_id, (str, int)):
        raise ValueError("id must be a string or integer")

    project_key_value = request.get("projectKey")
    project_key = project_key_value if isinstance(project_key_value, str) else None

    editor_context_value = request.get("editorContext")
    editor_context = (
        editor_context_value if isinstance(editor_context_value, dict) else None
    )

    capability_tokens: list[str] | None = None
    capability_tokens_value = request.get("capabilityTokens")
    if capability_tokens_value is not None:
        if not isinstance(capability_tokens_value, list):
            raise ValueError("capabilityTokens must be a list of strings")
        if not all(isinstance(token, str) for token in capability_tokens_value):
            raise ValueError("capabilityTokens must be a list of strings")
        capability_tokens = capability_tokens_value

    api_version_value = request.get("apiVersion")
    api_version = api_version_value if isinstance(api_version_value, str) else "0.1"

    rpc_request: dict[str, Any] = {
        "method": method,
        "params": params,
        "apiVersion": api_version,
    }
    if request_id is not None:
        rpc_request["id"] = request_id
    if project_key is not None:
        rpc_request["projectKey"] = project_key
    if editor_context is not None:
        rpc_request["editorContext"] = editor_context
    if capability_tokens is not None:
        rpc_request["capabilityTokens"] = capability_tokens

    daemon_enabled = not bool(args.no_daemon) and not _is_daemon_disabled_by_env()
    if daemon_enabled:
        try:
            daemon_result = daemon_request_call(
                socket_path=daemon_socket_path,
                rpc_request=rpc_request,
                connection_file=connection_path,
                timeout_seconds=timeout_seconds,
            )
            _print_json({"result": daemon_result})
            return 0
        except (DaemonError, OSError, TimeoutError) as exc:
            if bool(args.no_direct_fallback):
                raise RuntimeError(
                    f"Daemon request failed and direct fallback is disabled: {exc}"
                ) from exc

    client = IntelliJRpcClient.from_connection_file(
        connection_path,
        wait_seconds=timeout_seconds,
        timeout_seconds=timeout_seconds,
    )
    result = client.call(
        method=method,
        params=params,
        request_id=request_id,
        project_key=project_key,
        editor_context=editor_context,
        capability_tokens=capability_tokens,
        api_version=api_version,
    )
    _print_json({"result": result})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ijbridge", description="Headless IntelliJ bridge CLI"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    parser_discover = subparsers.add_parser(
        "discover", help="Discover IntelliJ installs"
    )
    parser_discover.set_defaults(func=_cmd_discover)

    parser_launch = subparsers.add_parser(
        "launch",
        help="Launch IntelliJ (no visible GUI by default)",
    )
    parser_launch.add_argument("--app-path", help="Path to IntelliJ .app bundle")
    parser_launch.add_argument("--project-path", help="Project path to open")
    parser_launch.add_argument(
        "--args",
        action="append",
        default=[],
        help="Repeatable argument passed to IntelliJ process",
    )
    parser_launch.add_argument(
        "--gui",
        action="store_true",
        default=False,
        help="Launch with visible IntelliJ GUI (default: background UIElement mode)",
    )
    parser_launch.add_argument(
        "--wait-ready",
        action="store_true",
        help="Wait for bridge connection file and healthy RPC endpoint",
    )
    parser_launch.add_argument(
        "--connection-file",
        help="Optional connection file path to wait on",
    )
    parser_launch.add_argument(
        "--timeout",
        type=float,
        help="Readiness timeout in seconds when --wait-ready is used",
    )
    parser_launch.set_defaults(func=_cmd_launch)

    parser_install_plugin = subparsers.add_parser(
        "install-plugin",
        help="Install plugin zip into IntelliJ plugins directory",
    )
    parser_install_plugin.add_argument(
        "--plugin-zip",
        required=True,
        help="Path to plugin zip built by Gradle buildPlugin",
    )
    parser_install_plugin.add_argument(
        "--app-path",
        help="Optional IntelliJ .app path used to resolve plugins dir",
    )
    parser_install_plugin.add_argument(
        "--plugins-path",
        help="Optional explicit IntelliJ plugins directory",
    )
    parser_install_plugin.set_defaults(func=_cmd_install_plugin)

    parser_connection = subparsers.add_parser(
        "connection-file", help="Print resolved connection file path"
    )
    parser_connection.set_defaults(func=_cmd_connection_file)

    parser_daemon = subparsers.add_parser("daemon", help="Bridge daemon operations")
    daemon_subparsers = parser_daemon.add_subparsers(
        dest="daemon_command", required=True
    )

    parser_daemon_run = daemon_subparsers.add_parser(
        "run", help="Run daemon foreground process"
    )
    parser_daemon_run.add_argument(
        "--daemon-socket",
        help="Path to daemon Unix socket",
    )
    parser_daemon_run.set_defaults(func=_cmd_daemon_run)

    parser_daemon_ping = daemon_subparsers.add_parser(
        "ping", help="Ping daemon over Unix socket"
    )
    parser_daemon_ping.add_argument(
        "--daemon-socket",
        help="Path to daemon Unix socket",
    )
    parser_daemon_ping.add_argument("--timeout", type=float, help="Timeout in seconds")
    parser_daemon_ping.set_defaults(func=_cmd_daemon_ping)

    parser_health = subparsers.add_parser("health", help="Call plugin health endpoint")
    parser_health.add_argument(
        "--connection-file", help="Path to plugin connection file"
    )
    parser_health.add_argument("--timeout", type=float, help="Timeout in seconds")
    parser_health.set_defaults(func=_cmd_health)

    parser_open_file = subparsers.add_parser("open-file", help="Open file in IntelliJ")
    parser_open_file.add_argument("--path", required=True, help="Absolute file path")
    parser_open_file.add_argument(
        "--project-key", help="Project key from listOpenProjects"
    )
    parser_open_file.add_argument(
        "--focus",
        dest="focus",
        action="store_true",
        default=True,
        help="Focus editor after opening (default)",
    )
    parser_open_file.add_argument(
        "--no-focus",
        dest="focus",
        action="store_false",
        help="Open file without focusing editor",
    )
    parser_open_file.add_argument(
        "--connection-file", help="Path to plugin connection file"
    )
    parser_open_file.add_argument("--timeout", type=float, help="Timeout in seconds")
    parser_open_file.set_defaults(func=_cmd_open_file)

    parser_get_text = subparsers.add_parser(
        "get-text", help="Read text for file through IntelliJ"
    )
    parser_get_text.add_argument("--path", required=True, help="Absolute file path")
    parser_get_text.add_argument(
        "--project-key", help="Project key from listOpenProjects"
    )
    parser_get_text.add_argument(
        "--connection-file", help="Path to plugin connection file"
    )
    parser_get_text.add_argument("--timeout", type=float, help="Timeout in seconds")
    parser_get_text.set_defaults(func=_cmd_get_text)

    parser_apply_edits = subparsers.add_parser(
        "apply-edits", help="Apply LSP-style text edits through IntelliJ"
    )
    parser_apply_edits.add_argument("--path", required=True, help="Absolute file path")
    parser_apply_edits.add_argument(
        "--edits-json",
        required=True,
        help="JSON array of text edits with {range:{start,end},text}",
    )
    parser_apply_edits.add_argument(
        "--project-key", help="Project key from listOpenProjects"
    )
    parser_apply_edits.add_argument(
        "--connection-file", help="Path to plugin connection file"
    )
    parser_apply_edits.add_argument("--timeout", type=float, help="Timeout in seconds")
    parser_apply_edits.set_defaults(func=_cmd_apply_edits)

    parser_get_caret = subparsers.add_parser(
        "get-caret", help="Get caret and selection state"
    )
    parser_get_caret.add_argument("--path", help="Optional file path")
    parser_get_caret.add_argument(
        "--project-key", help="Project key from listOpenProjects"
    )
    parser_get_caret.add_argument(
        "--connection-file", help="Path to plugin connection file"
    )
    parser_get_caret.add_argument("--timeout", type=float, help="Timeout in seconds")
    parser_get_caret.set_defaults(func=_cmd_get_caret)

    parser_set_caret = subparsers.add_parser(
        "set-caret", help="Set caret and optional selection state"
    )
    parser_set_caret.add_argument("--path", help="Optional file path")
    parser_set_caret.add_argument(
        "--project-key", help="Project key from listOpenProjects"
    )
    parser_set_caret.add_argument("--offset", type=int, help="Target caret offset")
    parser_set_caret.add_argument("--line", type=int, help="Target caret line")
    parser_set_caret.add_argument("--character", type=int, help="Target caret column")
    parser_set_caret.add_argument(
        "--selection-start", type=int, help="Selection start offset"
    )
    parser_set_caret.add_argument(
        "--selection-end", type=int, help="Selection end offset"
    )
    parser_set_caret.add_argument(
        "--connection-file", help="Path to plugin connection file"
    )
    parser_set_caret.add_argument("--timeout", type=float, help="Timeout in seconds")
    parser_set_caret.set_defaults(func=_cmd_set_caret)

    parser_list_actions = subparsers.add_parser(
        "list-actions", help="List IntelliJ actions"
    )
    parser_list_actions.add_argument(
        "--filter",
        help="Case-insensitive substring filter on action id/text/description",
    )
    parser_list_actions.add_argument(
        "--include-hidden",
        action="store_true",
        help="Include hidden/internal actions",
    )
    parser_list_actions.add_argument(
        "--limit",
        type=int,
        default=500,
        help="Maximum number of actions to return",
    )
    parser_list_actions.add_argument(
        "--connection-file", help="Path to plugin connection file"
    )
    parser_list_actions.add_argument("--timeout", type=float, help="Timeout in seconds")
    parser_list_actions.set_defaults(func=_cmd_list_actions)

    parser_perform_action = subparsers.add_parser(
        "perform-action", help="Perform IntelliJ action by id"
    )
    parser_perform_action.add_argument(
        "--action-id", required=True, help="IntelliJ action id"
    )
    parser_perform_action.add_argument(
        "--project-key", help="Project key from listOpenProjects"
    )
    parser_perform_action.add_argument(
        "--path", help="Optional file path context override"
    )
    parser_perform_action.add_argument(
        "--focus",
        dest="focus",
        action="store_true",
        default=True,
        help="Focus file if --path is provided (default)",
    )
    parser_perform_action.add_argument(
        "--no-focus",
        dest="focus",
        action="store_false",
        help="Do not focus file when opening context path",
    )
    parser_perform_action.add_argument(
        "--connection-file", help="Path to plugin connection file"
    )
    parser_perform_action.add_argument(
        "--timeout", type=float, help="Timeout in seconds"
    )
    parser_perform_action.set_defaults(func=_cmd_perform_action)

    parser_find_in_project = subparsers.add_parser(
        "find-in-project", help="Search text in project files"
    )
    parser_find_in_project.add_argument("--query", required=True, help="Search query")
    parser_find_in_project.add_argument(
        "--project-key", help="Project key from listOpenProjects"
    )
    parser_find_in_project.add_argument(
        "--case-sensitive",
        action="store_true",
        help="Use case-sensitive matching",
    )
    parser_find_in_project.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Maximum number of matches to return",
    )
    parser_find_in_project.add_argument(
        "--connection-file", help="Path to plugin connection file"
    )
    parser_find_in_project.add_argument(
        "--timeout", type=float, help="Timeout in seconds"
    )
    parser_find_in_project.set_defaults(func=_cmd_find_in_project)

    parser_resolve_symbol = subparsers.add_parser(
        "resolve-symbol", help="Resolve symbol at file offset"
    )
    parser_resolve_symbol.add_argument(
        "--path", required=True, help="Absolute file path"
    )
    parser_resolve_symbol.add_argument(
        "--offset", required=True, type=int, help="Offset"
    )
    parser_resolve_symbol.add_argument(
        "--project-key", help="Project key from listOpenProjects"
    )
    parser_resolve_symbol.add_argument(
        "--connection-file", help="Path to plugin connection file"
    )
    parser_resolve_symbol.add_argument(
        "--timeout", type=float, help="Timeout in seconds"
    )
    parser_resolve_symbol.set_defaults(func=_cmd_resolve_symbol)

    parser_rename_symbol = subparsers.add_parser(
        "rename-symbol", help="Rename symbol at file offset"
    )
    parser_rename_symbol.add_argument(
        "--path", required=True, help="Absolute file path"
    )
    parser_rename_symbol.add_argument(
        "--offset", required=True, type=int, help="Offset"
    )
    parser_rename_symbol.add_argument(
        "--new-name", required=True, help="New symbol name"
    )
    parser_rename_symbol.add_argument(
        "--project-key", help="Project key from listOpenProjects"
    )
    parser_rename_symbol.add_argument(
        "--connection-file", help="Path to plugin connection file"
    )
    parser_rename_symbol.add_argument(
        "--timeout", type=float, help="Timeout in seconds"
    )
    parser_rename_symbol.set_defaults(func=_cmd_rename_symbol)

    parser_list_run_configs = subparsers.add_parser(
        "list-run-configs", help="List IntelliJ run configurations"
    )
    parser_list_run_configs.add_argument(
        "--project-key", help="Project key from listOpenProjects"
    )
    parser_list_run_configs.add_argument(
        "--connection-file", help="Path to plugin connection file"
    )
    parser_list_run_configs.add_argument(
        "--timeout", type=float, help="Timeout in seconds"
    )
    parser_list_run_configs.set_defaults(func=_cmd_list_run_configs)

    parser_run_config = subparsers.add_parser(
        "run-config", help="Run configuration by name"
    )
    parser_run_config.add_argument("--name", required=True, help="Configuration name")
    parser_run_config.add_argument(
        "--project-key", help="Project key from listOpenProjects"
    )
    parser_run_config.add_argument(
        "--connection-file", help="Path to plugin connection file"
    )
    parser_run_config.add_argument("--timeout", type=float, help="Timeout in seconds")
    parser_run_config.set_defaults(func=_cmd_run_config)

    parser_run_tests = subparsers.add_parser("run-tests", help="Run test configuration")
    parser_run_tests.add_argument(
        "--configuration-name",
        help="Optional explicit test configuration name",
    )
    parser_run_tests.add_argument(
        "--project-key", help="Project key from listOpenProjects"
    )
    parser_run_tests.add_argument(
        "--connection-file", help="Path to plugin connection file"
    )
    parser_run_tests.add_argument("--timeout", type=float, help="Timeout in seconds")
    parser_run_tests.set_defaults(func=_cmd_run_tests)

    parser_diagnostics = subparsers.add_parser(
        "diagnostics", help="Get IntelliJ diagnostics for a file"
    )
    parser_diagnostics.add_argument("--path", required=True, help="Absolute file path")
    parser_diagnostics.add_argument(
        "--project-key", help="Project key from listOpenProjects"
    )
    parser_diagnostics.add_argument(
        "--severity",
        choices=["error", "warning", "information", "hint", "all"],
        help="Optional severity filter",
    )
    parser_diagnostics.add_argument(
        "--limit", type=int, default=500, help="Maximum diagnostics to return"
    )
    parser_diagnostics.add_argument(
        "--connection-file", help="Path to plugin connection file"
    )
    parser_diagnostics.add_argument("--timeout", type=float, help="Timeout in seconds")
    parser_diagnostics.set_defaults(func=_cmd_diagnostics)

    parser_completions = subparsers.add_parser(
        "completions", help="Get code completions at file offset"
    )
    parser_completions.add_argument("--path", required=True, help="Absolute file path")
    parser_completions.add_argument(
        "--offset", required=True, type=int, help="Caret offset"
    )
    parser_completions.add_argument(
        "--project-key", help="Project key from listOpenProjects"
    )
    parser_completions.add_argument(
        "--limit", type=int, default=200, help="Maximum completion items"
    )
    parser_completions.add_argument(
        "--connection-file", help="Path to plugin connection file"
    )
    parser_completions.add_argument("--timeout", type=float, help="Timeout in seconds")
    parser_completions.set_defaults(func=_cmd_completions)

    parser_hover = subparsers.add_parser(
        "hover", help="Get hover documentation at offset"
    )
    parser_hover.add_argument("--path", required=True, help="Absolute file path")
    parser_hover.add_argument("--offset", required=True, type=int, help="Caret offset")
    parser_hover.add_argument("--project-key", help="Project key from listOpenProjects")
    parser_hover.add_argument(
        "--connection-file", help="Path to plugin connection file"
    )
    parser_hover.add_argument("--timeout", type=float, help="Timeout in seconds")
    parser_hover.set_defaults(func=_cmd_hover)

    parser_signature_help = subparsers.add_parser(
        "signature-help", help="Get method signature info at offset"
    )
    parser_signature_help.add_argument(
        "--path", required=True, help="Absolute file path"
    )
    parser_signature_help.add_argument(
        "--offset", required=True, type=int, help="Caret offset"
    )
    parser_signature_help.add_argument(
        "--project-key", help="Project key from listOpenProjects"
    )
    parser_signature_help.add_argument(
        "--connection-file", help="Path to plugin connection file"
    )
    parser_signature_help.add_argument(
        "--timeout", type=float, help="Timeout in seconds"
    )
    parser_signature_help.set_defaults(func=_cmd_signature_help)

    parser_code_actions = subparsers.add_parser(
        "code-actions", help="List available code actions at offset"
    )
    parser_code_actions.add_argument("--path", required=True, help="Absolute file path")
    parser_code_actions.add_argument(
        "--offset", required=True, type=int, help="Caret offset"
    )
    parser_code_actions.add_argument(
        "--project-key", help="Project key from listOpenProjects"
    )
    parser_code_actions.add_argument(
        "--limit", type=int, default=100, help="Maximum code actions"
    )
    parser_code_actions.add_argument(
        "--connection-file", help="Path to plugin connection file"
    )
    parser_code_actions.add_argument("--timeout", type=float, help="Timeout in seconds")
    parser_code_actions.set_defaults(func=_cmd_code_actions)

    parser_apply_code_action = subparsers.add_parser(
        "apply-code-action", help="Apply a code action by id"
    )
    parser_apply_code_action.add_argument(
        "--action-id", required=True, help="Code action id"
    )
    parser_apply_code_action.add_argument(
        "--path", required=True, help="Absolute file path"
    )
    parser_apply_code_action.add_argument(
        "--offset", required=True, type=int, help="Caret offset"
    )
    parser_apply_code_action.add_argument(
        "--project-key", help="Project key from listOpenProjects"
    )
    parser_apply_code_action.add_argument(
        "--connection-file", help="Path to plugin connection file"
    )
    parser_apply_code_action.add_argument(
        "--timeout", type=float, help="Timeout in seconds"
    )
    parser_apply_code_action.set_defaults(func=_cmd_apply_code_action)

    parser_format_file = subparsers.add_parser(
        "format-file", help="Format full file using IntelliJ formatter"
    )
    parser_format_file.add_argument("--path", required=True, help="Absolute file path")
    parser_format_file.add_argument(
        "--project-key", help="Project key from listOpenProjects"
    )
    parser_format_file.add_argument(
        "--connection-file", help="Path to plugin connection file"
    )
    parser_format_file.add_argument("--timeout", type=float, help="Timeout in seconds")
    parser_format_file.set_defaults(func=_cmd_format_file)

    parser_format_range = subparsers.add_parser(
        "format-range", help="Format range using IntelliJ formatter"
    )
    parser_format_range.add_argument("--path", required=True, help="Absolute file path")
    parser_format_range.add_argument(
        "--start-line", required=True, type=int, help="Range start line"
    )
    parser_format_range.add_argument(
        "--start-character", required=True, type=int, help="Range start character"
    )
    parser_format_range.add_argument(
        "--end-line", required=True, type=int, help="Range end line"
    )
    parser_format_range.add_argument(
        "--end-character", required=True, type=int, help="Range end character"
    )
    parser_format_range.add_argument(
        "--project-key", help="Project key from listOpenProjects"
    )
    parser_format_range.add_argument(
        "--connection-file", help="Path to plugin connection file"
    )
    parser_format_range.add_argument("--timeout", type=float, help="Timeout in seconds")
    parser_format_range.set_defaults(func=_cmd_format_range)

    parser_optimize_imports = subparsers.add_parser(
        "optimize-imports", help="Optimize imports for file"
    )
    parser_optimize_imports.add_argument(
        "--path", required=True, help="Absolute file path"
    )
    parser_optimize_imports.add_argument(
        "--project-key", help="Project key from listOpenProjects"
    )
    parser_optimize_imports.add_argument(
        "--connection-file", help="Path to plugin connection file"
    )
    parser_optimize_imports.add_argument(
        "--timeout", type=float, help="Timeout in seconds"
    )
    parser_optimize_imports.set_defaults(func=_cmd_optimize_imports)

    parser_unsafe_status = subparsers.add_parser(
        "unsafe-status", help="Get unsafe API policy and status"
    )
    parser_unsafe_status.add_argument(
        "--connection-file", help="Path to plugin connection file"
    )
    parser_unsafe_status.add_argument(
        "--timeout", type=float, help="Timeout in seconds"
    )
    parser_unsafe_status.set_defaults(func=_cmd_unsafe_status)

    parser_unsafe_invoke = subparsers.add_parser(
        "unsafe-invoke", help="Invoke unsafe reflection RPC (disabled by default)"
    )
    target_group = parser_unsafe_invoke.add_mutually_exclusive_group(required=True)
    target_group.add_argument(
        "--target-class",
        help="Target class name (must be com.intellij.*)",
    )
    target_group.add_argument(
        "--target-handle",
        help="Opaque handle from previous unsafe result",
    )
    parser_unsafe_invoke.add_argument("--method", required=True, help="Method name")
    parser_unsafe_invoke.add_argument(
        "--args-json",
        default="[]",
        help="JSON array of arguments; handle references use {'handle':'<id>'}",
    )
    parser_unsafe_invoke.add_argument(
        "--return-handle",
        dest="return_handle",
        action="store_true",
        default=True,
        help="Return object results as handles when allowed (default)",
    )
    parser_unsafe_invoke.add_argument(
        "--no-return-handle",
        dest="return_handle",
        action="store_false",
        help="Return summary for object results",
    )
    parser_unsafe_invoke.add_argument(
        "--capability-token",
        action="append",
        default=[],
        help="Extra capability tokens",
    )
    parser_unsafe_invoke.add_argument(
        "--connection-file", help="Path to plugin connection file"
    )
    parser_unsafe_invoke.add_argument(
        "--timeout", type=float, help="Timeout in seconds"
    )
    parser_unsafe_invoke.set_defaults(func=_cmd_unsafe_invoke)

    parser_call = subparsers.add_parser("call", help="Send raw JSON-RPC call")
    parser_call.add_argument(
        "--json", required=True, help="JSON object containing method and params"
    )
    parser_call.add_argument("--connection-file", help="Path to plugin connection file")
    parser_call.add_argument("--daemon-socket", help="Path to daemon Unix socket")
    parser_call.add_argument(
        "--no-daemon",
        action="store_true",
        help="Disable daemon transport and call plugin directly",
    )
    parser_call.add_argument(
        "--no-direct-fallback",
        action="store_true",
        help="Fail if daemon call fails instead of falling back",
    )
    parser_call.add_argument("--timeout", type=float, help="Timeout in seconds")
    parser_call.set_defaults(func=_cmd_call)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
