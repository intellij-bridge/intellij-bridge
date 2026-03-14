from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
import os
import sys

from .config import BridgeConfig
from .discovery import IntelliJInstall, discover_intellij
from .installer import PluginInstallResult, ensure_plugin_installed, launch_intellij
from .rpc.client import IntelliJRpcClient
from .rpc.connection import (
    ConnectionInfo,
    read_connection_file,
    wait_for_connection_file,
)
from .version import PACKAGE_VERSION


@dataclass(frozen=True)
class BootstrapResult:
    app_path: str
    connection_file: str
    plugin_install: PluginInstallResult
    launched: bool
    reused_running_bridge: bool
    project_path: str | None

    def to_dict(self) -> dict[str, str | bool | None | dict[str, str | bool | None]]:
        return {
            "appPath": self.app_path,
            "connectionFile": self.connection_file,
            "pluginInstall": self.plugin_install.to_dict(),
            "launched": self.launched,
            "reusedRunningBridge": self.reused_running_bridge,
            "projectPath": self.project_path,
        }


def _resolve_project_path(project_path: str | Path | None) -> Path | None:
    if project_path is None:
        cwd = Path.cwd().resolve()
        return cwd if cwd.exists() else None

    resolved = Path(project_path).expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Project path not found: {resolved}")
    return resolved


def _discover_install(config: BridgeConfig) -> IntelliJInstall:
    installs = discover_intellij(explicit_app_path=config.intellij_app_path)
    if not installs:
        raise RuntimeError("No IntelliJ installation available for bootstrap")

    if config.intellij_app_path:
        normalized = str(Path(config.intellij_app_path).expanduser().resolve())
        for install in installs:
            if install.app_path == normalized:
                return install
        raise RuntimeError(f"Configured IntelliJ app not found: {normalized}")

    return installs[0]


def _candidate_plugin_zips() -> list[Path]:
    candidates: list[Path] = []

    def add(path: Path) -> None:
        resolved = path.expanduser().resolve()
        if resolved not in candidates:
            candidates.append(resolved)

    env_value = os.getenv("INTELLIJ_BRIDGE_PLUGIN_ZIP")
    if env_value:
        add(Path(env_value))

    current_file = Path(__file__).resolve()
    module_root = current_file.parent
    bridge_root = current_file.parents[1]
    repo_root = current_file.parents[2]

    search_roots = [module_root, bridge_root, repo_root, Path.cwd().resolve()]
    for root in search_roots:
        add(root / "assets" / "opencode-intellij-bridge.zip")
        add(root / "assets" / f"opencode-intellij-bridge-{PACKAGE_VERSION}.zip")
        add(root / "opencode-intellij-bridge.zip")
        add(root / f"opencode-intellij-bridge-{PACKAGE_VERSION}.zip")

    add(
        repo_root
        / "intellij-plugin"
        / "build"
        / "distributions"
        / "opencode-intellij-bridge.zip"
    )
    add(
        repo_root
        / "intellij-plugin"
        / "build"
        / "distributions"
        / f"opencode-intellij-bridge-{PACKAGE_VERSION}.zip"
    )

    executable_dir = Path(sys.executable).resolve().parent
    add(executable_dir / "opencode-intellij-bridge.zip")
    add(executable_dir / f"opencode-intellij-bridge-{PACKAGE_VERSION}.zip")
    add(executable_dir / "assets" / "opencode-intellij-bridge.zip")
    add(executable_dir / "assets" / f"opencode-intellij-bridge-{PACKAGE_VERSION}.zip")

    return candidates


def resolve_plugin_zip_path() -> Path:
    for candidate in _candidate_plugin_zips():
        if candidate.exists() and candidate.is_file():
            return candidate

    searched = "\n".join(str(candidate) for candidate in _candidate_plugin_zips())
    raise FileNotFoundError(
        "Could not locate IntelliJ plugin zip for bootstrap. Searched:\n" + searched
    )


def _try_read_connection(connection_file: Path) -> ConnectionInfo | None:
    if not connection_file.exists():
        return None
    try:
        return read_connection_file(connection_file)
    except Exception:
        return None


def _is_bridge_healthy(
    connection: ConnectionInfo | None, timeout_seconds: float
) -> bool:
    if connection is None:
        return False

    try:
        client = IntelliJRpcClient(
            host="127.0.0.1",
            port=connection.port,
            token=connection.token,
            timeout_seconds=timeout_seconds,
        )
        health = client.health()
    except Exception:
        return False

    return isinstance(health, dict) and health.get("status") == "ok"


def _wait_for_healthy_bridge(
    *,
    connection_file: Path,
    timeout_seconds: float,
    min_connection_mtime: float | None,
    different_from: ConnectionInfo | None,
) -> ConnectionInfo:
    deadline = time.monotonic() + timeout_seconds
    last_connection: ConnectionInfo | None = None

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"Timed out waiting for IntelliJ bridge readiness: {connection_file}"
            )

        last_connection = wait_for_connection_file(
            connection_file,
            timeout_seconds=min(remaining, 1.0),
            min_mtime_seconds=min_connection_mtime,
            different_from=different_from,
        )
        if _is_bridge_healthy(last_connection, min(remaining, 5.0)):
            return last_connection

        time.sleep(0.25)


def ensure_bootstrapped(
    *,
    config: BridgeConfig,
    connection_file: Path,
    timeout_seconds: float,
    project_path: str | Path | None = None,
) -> BootstrapResult:
    existing_connection = _try_read_connection(connection_file)
    install = _discover_install(config)
    plugin_zip_path = resolve_plugin_zip_path()

    plugin_install = ensure_plugin_installed(
        plugin_zip=str(plugin_zip_path),
        plugins_path=config.plugins_path,
        app_path=install.app_path,
    )

    resolved_project = _resolve_project_path(project_path)
    if _is_bridge_healthy(existing_connection, timeout_seconds):
        return BootstrapResult(
            app_path=install.app_path,
            connection_file=str(connection_file),
            plugin_install=plugin_install,
            launched=False,
            reused_running_bridge=True,
            project_path=str(resolved_project)
            if resolved_project is not None
            else None,
        )

    previous_mtime = (
        connection_file.stat().st_mtime if connection_file.exists() else None
    )
    launch_intellij(
        install.app_path,
        project_path=resolved_project,
        gui=False,
    )
    _wait_for_healthy_bridge(
        connection_file=connection_file,
        timeout_seconds=timeout_seconds,
        min_connection_mtime=previous_mtime,
        different_from=existing_connection,
    )
    return BootstrapResult(
        app_path=install.app_path,
        connection_file=str(connection_file),
        plugin_install=plugin_install,
        launched=True,
        reused_running_bridge=False,
        project_path=str(resolved_project) if resolved_project is not None else None,
    )
