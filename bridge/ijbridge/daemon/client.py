from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class DaemonError(RuntimeError):
    message: str
    code: int = -1
    data: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        super().__init__(self.message)


def _send_request(
    socket_path: Path,
    payload: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout_seconds)
        sock.connect(str(socket_path))
        with sock.makefile("rwb") as stream:
            body = (json.dumps(payload) + "\n").encode("utf-8")
            stream.write(body)
            stream.flush()

            response_line = stream.readline()

    if not response_line:
        raise DaemonError("Daemon returned empty response", code=-32090)

    decoded = json.loads(response_line.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise DaemonError("Daemon returned malformed response", code=-32091)
    return decoded


def daemon_ping(socket_path: Path, timeout_seconds: float = 1.0) -> dict[str, Any]:
    response = _send_request(
        socket_path=socket_path,
        payload={"op": "ping"},
        timeout_seconds=timeout_seconds,
    )
    if response.get("ok") is True:
        result = response.get("result")
        if isinstance(result, dict):
            return result
        raise DaemonError("Daemon ping response missing result object", code=-32092)

    error = response.get("error")
    if isinstance(error, dict):
        raise DaemonError(
            message=str(error.get("message", "Daemon ping failed")),
            code=int(error.get("code", -32093)),
            data=error.get("data") if isinstance(error.get("data"), dict) else None,
        )
    raise DaemonError("Daemon ping failed", code=-32093)


def daemon_request_call(
    socket_path: Path,
    *,
    rpc_request: dict[str, Any],
    connection_file: Path,
    timeout_seconds: float,
) -> Any:
    wire_timeout_seconds = max(0.1, timeout_seconds + 1.0)

    response = _send_request(
        socket_path=socket_path,
        payload={
            "op": "rpc",
            "payload": rpc_request,
            "connectionFile": str(connection_file),
            "timeout": timeout_seconds,
        },
        timeout_seconds=wire_timeout_seconds,
    )

    if response.get("ok") is True:
        return response.get("result")

    error = response.get("error")
    if isinstance(error, dict):
        raise DaemonError(
            message=str(error.get("message", "Daemon RPC failed")),
            code=int(error.get("code", -32094)),
            data=error.get("data") if isinstance(error.get("data"), dict) else None,
        )

    raise DaemonError("Daemon RPC failed", code=-32094)
