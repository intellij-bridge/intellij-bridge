from __future__ import annotations

import json
import os
import socketserver
import threading
import time
import urllib.error
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, cast

from ..rpc import IntelliJRpcClient, RpcError
from ..rpc.connection import ConnectionInfo, wait_for_connection_file
from .client import DaemonError, daemon_ping


def _error(
    code: int, message: str, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
        },
    }
    if data is not None:
        payload["error"]["data"] = data
    return payload


class _LaneScheduler:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._executors: dict[str, ThreadPoolExecutor] = {}

    def _get_executor(self, lane: str) -> ThreadPoolExecutor:
        with self._lock:
            existing = self._executors.get(lane)
            if existing is not None:
                return existing

            executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix=f"ijbridge-{lane}"
            )
            self._executors[lane] = executor
            return executor

    def execute(self, lane: str, timeout_seconds: float, fn: Callable[[], Any]) -> Any:
        executor = self._get_executor(lane)
        future: Future[Any] = executor.submit(fn)
        return future.result(timeout=timeout_seconds)

    def shutdown(self) -> None:
        with self._lock:
            executors = list(self._executors.values())
            self._executors.clear()

        for executor in executors:
            executor.shutdown(wait=False, cancel_futures=True)


@dataclass
class _ClientEntry:
    client: IntelliJRpcClient
    token: str
    port: int
    instance_id: str | None


class _ClientPool:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, _ClientEntry] = {}
        self._stale_entries: dict[str, _ClientEntry] = {}

    @staticmethod
    def _to_connection_info(entry: _ClientEntry) -> ConnectionInfo:
        return ConnectionInfo(
            port=entry.port,
            token=entry.token,
            instance_id=entry.instance_id,
        )

    def get_client(
        self, connection_file: Path, timeout_seconds: float
    ) -> IntelliJRpcClient:
        key = str(connection_file)

        with self._lock:
            existing = self._entries.get(key)
            stale = self._stale_entries.get(key)

        if existing is not None:
            existing.client.timeout_seconds = timeout_seconds
            return existing.client

        wait_timeout_seconds = timeout_seconds
        if stale is not None:
            wait_timeout_seconds = min(timeout_seconds, 1.5)

        try:
            return self.reload_client(
                connection_file,
                timeout_seconds,
                wait_timeout_seconds=wait_timeout_seconds,
                different_from=stale,
            )
        except TimeoutError:
            if stale is None:
                raise

            fallback_client = IntelliJRpcClient(
                host="127.0.0.1",
                port=stale.port,
                token=stale.token,
                timeout_seconds=timeout_seconds,
            )
            fallback_entry = _ClientEntry(
                client=fallback_client,
                token=stale.token,
                port=stale.port,
                instance_id=stale.instance_id,
            )

            with self._lock:
                self._entries[key] = fallback_entry
                self._stale_entries.pop(key, None)

            return fallback_client

    def reload_client(
        self,
        connection_file: Path,
        timeout_seconds: float,
        *,
        wait_timeout_seconds: float | None = None,
        different_from: _ClientEntry | None = None,
    ) -> IntelliJRpcClient:
        effective_wait_timeout = (
            wait_timeout_seconds
            if wait_timeout_seconds is not None
            else timeout_seconds
        )

        previous_connection: ConnectionInfo | None = None
        if different_from is not None:
            previous_connection = self._to_connection_info(different_from)

        info = wait_for_connection_file(
            connection_file,
            timeout_seconds=effective_wait_timeout,
            different_from=previous_connection,
        )
        client = IntelliJRpcClient(
            host="127.0.0.1",
            port=info.port,
            token=info.token,
            timeout_seconds=timeout_seconds,
        )
        entry = _ClientEntry(
            client=client,
            token=info.token,
            port=info.port,
            instance_id=info.instance_id,
        )
        key = str(connection_file)

        with self._lock:
            self._entries[key] = entry
            self._stale_entries.pop(key, None)

        return client

    def invalidate(self, connection_file: Path) -> _ClientEntry | None:
        key = str(connection_file)
        with self._lock:
            previous = self._entries.pop(key, None)
            if previous is not None:
                self._stale_entries[key] = previous
                return previous
            return self._stale_entries.get(key)


class _BridgeDaemon:
    def __init__(self) -> None:
        self._scheduler = _LaneScheduler()
        self._clients = _ClientPool()

    def shutdown(self) -> None:
        self._scheduler.shutdown()

    def handle_request(self, request: dict[str, Any]) -> dict[str, Any]:
        op = request.get("op")
        if op == "ping":
            return {
                "ok": True,
                "result": {
                    "status": "ok",
                    "pid": os.getpid(),
                },
            }

        if op != "rpc":
            return _error(-32601, f"Unsupported daemon op: {op}")

        payload = request.get("payload")
        if not isinstance(payload, dict):
            return _error(-32602, "rpc payload must be an object")

        connection_file_value = request.get("connectionFile")
        if not isinstance(connection_file_value, str) or not connection_file_value:
            return _error(-32602, "connectionFile must be a non-empty string")
        connection_file = Path(connection_file_value).expanduser()

        timeout_raw = request.get("timeout", 10.0)
        try:
            timeout_seconds = max(0.1, float(timeout_raw))
        except (TypeError, ValueError):
            return _error(-32602, "timeout must be a number")

        lane_value = payload.get("projectKey")
        lane = (
            lane_value if isinstance(lane_value, str) and lane_value else "__default__"
        )

        try:
            result = self._scheduler.execute(
                lane=lane,
                timeout_seconds=timeout_seconds,
                fn=lambda: self._execute_rpc_with_retries(
                    payload=payload,
                    connection_file=connection_file,
                    timeout_seconds=timeout_seconds,
                ),
            )
            return {"ok": True, "result": result}
        except FutureTimeout:
            return _error(-32095, "Daemon lane timed out")
        except RpcError as exc:
            return _error(exc.code, exc.message, exc.data)
        except Exception as exc:
            return _error(-32096, f"Daemon transport failed: {exc}")

    def _execute_rpc_with_retries(
        self,
        *,
        payload: dict[str, Any],
        connection_file: Path,
        timeout_seconds: float,
    ) -> Any:
        method = payload.get("method")
        if not isinstance(method, str) or not method:
            raise ValueError("RPC method must be a non-empty string")

        params = payload.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ValueError("RPC params must be an object")

        request_id = payload.get("id")
        if request_id is not None and not isinstance(request_id, (str, int)):
            raise ValueError("RPC id must be string, int, or null")

        project_key = payload.get("projectKey")
        if not isinstance(project_key, str):
            project_key = None

        editor_context = payload.get("editorContext")
        if not isinstance(editor_context, dict):
            editor_context = None

        capability_tokens = payload.get("capabilityTokens")
        if not isinstance(capability_tokens, list) or not all(
            isinstance(token, str) for token in capability_tokens
        ):
            capability_tokens = None

        api_version = payload.get("apiVersion")
        if not isinstance(api_version, str):
            api_version = "0.1"

        attempt = 0
        max_attempts = 3
        backoff_seconds = 0.1

        while True:
            attempt += 1
            client: IntelliJRpcClient | None = None
            try:
                client = self._clients.get_client(connection_file, timeout_seconds)
                return client.call(
                    method=method,
                    params=params,
                    request_id=request_id,
                    project_key=project_key,
                    editor_context=editor_context,
                    capability_tokens=capability_tokens,
                    api_version=api_version,
                )
            except RpcError:
                raise
            except Exception as exc:
                if self._should_invalidate_client(exc):
                    self._clients.invalidate(connection_file)
                elif client is not None:
                    self._clients.invalidate(connection_file)

                if attempt >= max_attempts or not self._is_retryable_transport_error(
                    exc
                ):
                    raise

                time.sleep(backoff_seconds)
                backoff_seconds *= 3

    def _should_invalidate_client(self, exc: Exception) -> bool:
        if isinstance(exc, urllib.error.URLError):
            return True
        if isinstance(exc, TimeoutError):
            return True
        if isinstance(exc, OSError):
            return True
        if isinstance(exc, RuntimeError):
            message = str(exc).lower()
            return "http 401" in message or "http 403" in message
        return False

    def _is_retryable_transport_error(self, exc: Exception) -> bool:
        if isinstance(exc, urllib.error.URLError):
            return True
        if isinstance(exc, TimeoutError):
            return True
        if isinstance(exc, OSError):
            return True
        if isinstance(exc, RuntimeError):
            message = str(exc).lower()
            if "http 5" in message:
                return True
            if "http 401" in message or "http 403" in message:
                return True
            if "timed out" in message:
                return True
            if "connection refused" in message:
                return True
            if "connection reset" in message:
                return True
        return False


class _DaemonSocketServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, socket_path: str, bridge: _BridgeDaemon) -> None:
        self.bridge = bridge
        super().__init__(socket_path, _DaemonRequestHandler)


class _DaemonRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            raw_line = self.rfile.readline()
            if not raw_line:
                return

            decoded = json.loads(raw_line.decode("utf-8"))
            if not isinstance(decoded, dict):
                response = _error(-32600, "Daemon request must be a JSON object")
            else:
                bridge = cast(_BridgeDaemon, getattr(self.server, "bridge"))
                response = bridge.handle_request(decoded)
        except json.JSONDecodeError:
            response = _error(-32700, "Daemon request parse error")
        except Exception as exc:
            response = _error(-32097, f"Daemon handler failed: {exc}")

        self.wfile.write((json.dumps(response) + "\n").encode("utf-8"))


def _cleanup_socket_path(socket_path: Path) -> None:
    if not socket_path.exists():
        return

    if socket_path.is_dir():
        raise RuntimeError(f"Daemon socket path points to a directory: {socket_path}")

    try:
        daemon_ping(socket_path=socket_path, timeout_seconds=0.2)
        raise RuntimeError(f"Daemon already running on socket: {socket_path}")
    except DaemonError:
        socket_path.unlink(missing_ok=True)
    except OSError:
        socket_path.unlink(missing_ok=True)


def run_daemon_server(socket_path: Path) -> None:
    resolved_socket_path = socket_path.expanduser()
    resolved_socket_path.parent.mkdir(parents=True, exist_ok=True)

    _cleanup_socket_path(resolved_socket_path)

    bridge = _BridgeDaemon()
    server = _DaemonSocketServer(str(resolved_socket_path), bridge)
    os.chmod(resolved_socket_path, 0o600)

    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        bridge.shutdown()
        resolved_socket_path.unlink(missing_ok=True)
