from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


APP_DIR_NAME = "intellibridge"
LEGACY_APP_DIR_NAME = "opencode/intellij-bridge"

DEFAULT_CONNECTION_FILE = Path.home() / ".cache" / APP_DIR_NAME / "connection.json"
DEFAULT_DAEMON_SOCKET = Path.home() / ".cache" / APP_DIR_NAME / "daemon.sock"


def _env_first(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


@dataclass(frozen=True)
class BridgeConfig:
    intellij_app_path: str | None = None
    plugins_path: str | None = None
    connection_file: str | None = None
    daemon_socket_path: str | None = None
    request_timeout_seconds: float = 10.0


def _read_json_file(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Config file must contain a JSON object: {path}")
    return data


def _candidate_config_paths(cwd: Path | None) -> list[Path]:
    env_override = _env_first(
        "INTELLIJ_BRIDGE_CONFIG",
        "OPENCODE_INTELLIJ_BRIDGE_CONFIG",
    )
    if env_override:
        return [Path(env_override).expanduser()]

    resolved_cwd = cwd if cwd is not None else Path.cwd()
    return [
        resolved_cwd / ".intellibridge" / "config.json",
        resolved_cwd / ".opencode" / "intellij-bridge.json",
        Path.home() / ".config" / APP_DIR_NAME / "config.json",
        Path.home() / ".config" / "opencode" / "intellij-bridge.json",
    ]


def load_bridge_config(cwd: Path | None = None) -> BridgeConfig:
    config_data: dict[str, Any] = {}
    for candidate in _candidate_config_paths(cwd):
        if candidate.exists():
            config_data = _read_json_file(candidate)
            break

    app_path = _env_first(
        "INTELLIJ_BRIDGE_APP_PATH", "OPENCODE_IDEA_APP_PATH"
    ) or config_data.get("intellijAppPath")
    plugins_path = _env_first(
        "INTELLIJ_BRIDGE_PLUGINS_PATH",
        "OPENCODE_IDEA_PLUGINS_PATH",
    ) or config_data.get("pluginsPath")
    connection_file = _env_first(
        "INTELLIJ_BRIDGE_CONNECTION_FILE",
        "OPENCODE_IDEA_CONNECTION_FILE",
    ) or config_data.get("connectionFile")
    daemon_socket_path = _env_first(
        "INTELLIJ_BRIDGE_DAEMON_SOCKET",
        "OPENCODE_IDEA_DAEMON_SOCKET",
    ) or config_data.get("daemonSocketPath")

    timeout_value: Any = config_data.get("requestTimeoutSeconds", 10.0)
    env_timeout = _env_first(
        "INTELLIJ_BRIDGE_REQUEST_TIMEOUT",
        "OPENCODE_IDEA_REQUEST_TIMEOUT",
    )
    if env_timeout:
        timeout_value = env_timeout

    try:
        timeout_seconds = float(timeout_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("requestTimeoutSeconds must be a number") from exc

    return BridgeConfig(
        intellij_app_path=app_path,
        plugins_path=plugins_path,
        connection_file=connection_file,
        daemon_socket_path=daemon_socket_path,
        request_timeout_seconds=timeout_seconds,
    )


def get_connection_file_path(config: BridgeConfig) -> Path:
    if config.connection_file:
        return Path(config.connection_file).expanduser()
    legacy_path = Path.home() / ".cache" / LEGACY_APP_DIR_NAME / "connection.json"
    if legacy_path.exists():
        return legacy_path
    return DEFAULT_CONNECTION_FILE


def get_plugins_path(config: BridgeConfig) -> Path | None:
    if config.plugins_path:
        return Path(config.plugins_path).expanduser()
    return None


def get_daemon_socket_path(config: BridgeConfig) -> Path:
    if config.daemon_socket_path:
        return Path(config.daemon_socket_path).expanduser()
    legacy_path = Path.home() / ".cache" / LEGACY_APP_DIR_NAME / "daemon.sock"
    if legacy_path.exists():
        return legacy_path
    return DEFAULT_DAEMON_SOCKET
