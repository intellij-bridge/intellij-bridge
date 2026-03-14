from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ConnectionInfo:
    port: int
    token: str
    ide_build: str | None = None
    instance_id: str | None = None
    plugin_version: str | None = None
    api_version: str | None = None

    def to_dict(self) -> dict[str, int | str | None]:
        return {
            "port": self.port,
            "token": self.token,
            "ideBuild": self.ide_build,
            "instanceId": self.instance_id,
            "pluginVersion": self.plugin_version,
            "apiVersion": self.api_version,
        }


def read_connection_file(path: Path) -> ConnectionInfo:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    if not isinstance(payload, dict):
        raise ValueError(f"Connection file must contain JSON object: {path}")

    port = payload.get("port")
    token = payload.get("token")
    if not isinstance(port, int):
        raise ValueError(f"Connection file missing integer port: {path}")
    if not isinstance(token, str) or not token:
        raise ValueError(f"Connection file missing token: {path}")

    return ConnectionInfo(
        port=port,
        token=token,
        ide_build=payload.get("ideBuild")
        if isinstance(payload.get("ideBuild"), str)
        else None,
        instance_id=payload.get("instanceId")
        if isinstance(payload.get("instanceId"), str)
        else None,
        plugin_version=payload.get("pluginVersion")
        if isinstance(payload.get("pluginVersion"), str)
        else None,
        api_version=payload.get("apiVersion")
        if isinstance(payload.get("apiVersion"), str)
        else None,
    )


def _is_same_connection(a: ConnectionInfo, b: ConnectionInfo) -> bool:
    if a.port != b.port:
        return False
    if a.token != b.token:
        return False
    if a.instance_id and b.instance_id:
        return a.instance_id == b.instance_id
    return True


def wait_for_connection_file(
    path: Path,
    timeout_seconds: float,
    poll_interval_seconds: float = 0.25,
    *,
    min_mtime_seconds: float | None = None,
    different_from: ConnectionInfo | None = None,
) -> ConnectionInfo:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None

    while True:
        if path.exists():
            try:
                if min_mtime_seconds is not None:
                    modified_time = path.stat().st_mtime
                    if modified_time < min_mtime_seconds:
                        if time.monotonic() >= deadline:
                            raise TimeoutError(
                                "Timed out waiting for fresh connection file update: "
                                f"{path}"
                            )
                        time.sleep(poll_interval_seconds)
                        continue

                connection = read_connection_file(path)
                if different_from is not None and _is_same_connection(
                    connection, different_from
                ):
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"Timed out waiting for connection endpoint refresh: {path}"
                        )
                    time.sleep(poll_interval_seconds)
                    continue

                return connection
            except (json.JSONDecodeError, OSError, ValueError) as exc:
                last_error = exc

        if time.monotonic() >= deadline:
            if last_error is not None:
                raise TimeoutError(
                    f"Timed out waiting for connection file: {path}: {last_error}"
                ) from last_error
            raise TimeoutError(f"Timed out waiting for connection file: {path}")

        time.sleep(poll_interval_seconds)
