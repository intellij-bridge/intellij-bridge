from __future__ import annotations

import json
import uuid
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .connection import ConnectionInfo, wait_for_connection_file


class RpcError(RuntimeError):
    def __init__(
        self, code: int, message: str, data: dict[str, Any] | None = None
    ) -> None:
        super().__init__(f"RPC error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


def _request_json(
    url: str,
    method: str,
    headers: dict[str, str],
    payload: dict[str, Any] | None,
    timeout_seconds: float,
) -> dict[str, Any]:
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(url=url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {body}") from exc

    if not body:
        return {}

    decoded = json.loads(body)
    if not isinstance(decoded, dict):
        raise RuntimeError(f"Expected object JSON response from {url}")
    return decoded


class IntelliJRpcClient:
    def __init__(
        self, host: str, port: int, token: str, timeout_seconds: float = 10.0
    ) -> None:
        self.host = host
        self.port = port
        self.token = token
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_connection_file(
        cls,
        connection_path: Path,
        wait_seconds: float = 15.0,
        timeout_seconds: float = 10.0,
        *,
        min_connection_mtime: float | None = None,
        different_from_connection: ConnectionInfo | None = None,
    ) -> "IntelliJRpcClient":
        connection = wait_for_connection_file(
            connection_path,
            timeout_seconds=wait_seconds,
            min_mtime_seconds=min_connection_mtime,
            different_from=different_from_connection,
        )
        return cls(
            host="127.0.0.1",
            port=connection.port,
            token=connection.token,
            timeout_seconds=timeout_seconds,
        )

    @property
    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def health(self) -> dict[str, Any]:
        health_url = f"{self.base_url}/health"
        try:
            return _request_json(
                url=health_url,
                method="GET",
                headers=self._auth_headers,
                payload=None,
                timeout_seconds=self.timeout_seconds,
            )
        except Exception:
            return self.call("health", params={})

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        request_id: str | int | None = None,
        project_key: str | None = None,
        editor_context: dict[str, Any] | None = None,
        capability_tokens: list[str] | None = None,
        api_version: str = "0.1",
    ) -> Any:
        rpc_request: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id if request_id is not None else str(uuid.uuid4()),
            "apiVersion": api_version,
            "method": method,
            "params": params if params is not None else {},
        }

        if project_key:
            rpc_request["projectKey"] = project_key
        if editor_context:
            rpc_request["editorContext"] = editor_context
        if capability_tokens:
            rpc_request["capabilityTokens"] = capability_tokens

        response = _request_json(
            url=f"{self.base_url}/rpc",
            method="POST",
            headers=self._auth_headers,
            payload=rpc_request,
            timeout_seconds=self.timeout_seconds,
        )

        if "error" in response:
            error_payload = response["error"]
            if isinstance(error_payload, dict):
                code = error_payload.get("code", -1)
                message = error_payload.get("message", "Unknown RPC error")
                data = error_payload.get("data")
                raise RpcError(
                    int(code), str(message), data if isinstance(data, dict) else None
                )
            raise RpcError(-1, "Malformed RPC error")

        if "result" not in response:
            raise RuntimeError("Malformed RPC response: missing result")

        return response["result"]

    def open_file(
        self,
        path: str,
        *,
        focus: bool = True,
        project_key: str | None = None,
    ) -> dict[str, Any]:
        result = self.call(
            method="openFile",
            params={"path": path, "focus": focus},
            project_key=project_key,
        )
        if not isinstance(result, dict):
            raise RuntimeError("Malformed response for openFile")
        return result

    def get_document_text(
        self,
        path: str,
        *,
        project_key: str | None = None,
    ) -> dict[str, Any]:
        result = self.call(
            method="getDocumentText",
            params={"path": path},
            project_key=project_key,
        )
        if not isinstance(result, dict):
            raise RuntimeError("Malformed response for getDocumentText")
        return result

    def sync_document(
        self,
        path: str,
        text: str,
        *,
        project_key: str | None = None,
        version: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"path": path, "text": text}
        if version is not None:
            params["version"] = version
        result = self.call(
            method="syncDocument",
            params=params,
            project_key=project_key,
        )
        if not isinstance(result, dict):
            raise RuntimeError("Malformed response for syncDocument")
        return result

    def close_document(
        self,
        path: str,
        *,
        project_key: str | None = None,
        revert: bool = True,
    ) -> dict[str, Any]:
        result = self.call(
            method="closeDocument",
            params={"path": path, "revert": revert},
            project_key=project_key,
        )
        if not isinstance(result, dict):
            raise RuntimeError("Malformed response for closeDocument")
        return result

    def apply_text_edits(
        self,
        path: str,
        edits: list[dict[str, Any]],
        *,
        project_key: str | None = None,
    ) -> dict[str, Any]:
        result = self.call(
            method="applyTextEdits",
            params={"path": path, "edits": edits},
            project_key=project_key,
        )
        if not isinstance(result, dict):
            raise RuntimeError("Malformed response for applyTextEdits")
        return result

    def get_caret_state(
        self,
        *,
        path: str | None = None,
        project_key: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if path is not None:
            params["path"] = path

        result = self.call(
            method="getCaretState",
            params=params,
            project_key=project_key,
        )
        if not isinstance(result, dict):
            raise RuntimeError("Malformed response for getCaretState")
        return result

    def set_caret_state(
        self,
        *,
        path: str | None = None,
        project_key: str | None = None,
        offset: int | None = None,
        line: int | None = None,
        character: int | None = None,
        selection_start: int | None = None,
        selection_end: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if path is not None:
            params["path"] = path

        if offset is not None:
            params["offset"] = offset
        if line is not None:
            params["line"] = line
        if character is not None:
            params["character"] = character
        if selection_start is not None:
            params["selectionStart"] = selection_start
        if selection_end is not None:
            params["selectionEnd"] = selection_end

        result = self.call(
            method="setCaretState",
            params=params,
            project_key=project_key,
        )
        if not isinstance(result, dict):
            raise RuntimeError("Malformed response for setCaretState")
        return result

    def list_actions(
        self,
        *,
        filter_text: str | None = None,
        include_hidden: bool = False,
        limit: int = 500,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "includeHidden": include_hidden,
            "limit": limit,
        }
        if filter_text is not None:
            params["filter"] = filter_text

        result = self.call(
            method="listActions",
            params=params,
        )
        if not isinstance(result, dict):
            raise RuntimeError("Malformed response for listActions")
        return result

    def perform_action(
        self,
        action_id: str,
        *,
        project_key: str | None = None,
        path: str | None = None,
        focus: bool = True,
    ) -> dict[str, Any]:
        context_overrides: dict[str, Any] = {"focus": focus}
        if path is not None:
            context_overrides["path"] = path

        result = self.call(
            method="performAction",
            params={
                "actionId": action_id,
                "contextOverrides": context_overrides,
            },
            project_key=project_key,
        )
        if not isinstance(result, dict):
            raise RuntimeError("Malformed response for performAction")
        return result

    def find_in_project(
        self,
        query: str,
        *,
        project_key: str | None = None,
        case_sensitive: bool = False,
        limit: int = 200,
    ) -> dict[str, Any]:
        result = self.call(
            method="findInProject",
            params={
                "query": query,
                "caseSensitive": case_sensitive,
                "limit": limit,
            },
            project_key=project_key,
        )
        if not isinstance(result, dict):
            raise RuntimeError("Malformed response for findInProject")
        return result

    def resolve_symbol_at(
        self,
        path: str,
        offset: int,
        *,
        project_key: str | None = None,
    ) -> dict[str, Any]:
        result = self.call(
            method="resolveSymbolAt",
            params={
                "path": path,
                "offset": offset,
            },
            project_key=project_key,
        )
        if not isinstance(result, dict):
            raise RuntimeError("Malformed response for resolveSymbolAt")
        return result

    def get_definitions(
        self,
        path: str,
        offset: int,
        *,
        project_key: str | None = None,
    ) -> dict[str, Any]:
        result = self.call(
            method="getDefinitions",
            params={"path": path, "offset": offset},
            project_key=project_key,
        )
        if not isinstance(result, dict):
            raise RuntimeError("Malformed response for getDefinitions")
        return result

    def find_references(
        self,
        path: str,
        offset: int,
        *,
        project_key: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        result = self.call(
            method="findReferences",
            params={"path": path, "offset": offset, "limit": limit},
            project_key=project_key,
        )
        if not isinstance(result, dict):
            raise RuntimeError("Malformed response for findReferences")
        return result

    def prepare_rename(
        self,
        path: str,
        offset: int,
        *,
        project_key: str | None = None,
    ) -> dict[str, Any]:
        result = self.call(
            method="prepareRename",
            params={"path": path, "offset": offset},
            project_key=project_key,
        )
        if not isinstance(result, dict):
            raise RuntimeError("Malformed response for prepareRename")
        return result

    def rename_symbol(
        self,
        path: str,
        offset: int,
        new_name: str,
        *,
        project_key: str | None = None,
    ) -> dict[str, Any]:
        result = self.call(
            method="renameSymbol",
            params={
                "symbolRef": {
                    "path": path,
                    "offset": offset,
                },
                "newName": new_name,
            },
            project_key=project_key,
        )
        if not isinstance(result, dict):
            raise RuntimeError("Malformed response for renameSymbol")
        return result

    def list_run_configurations(
        self,
        *,
        project_key: str | None = None,
    ) -> dict[str, Any]:
        result = self.call(
            method="listRunConfigurations",
            params={},
            project_key=project_key,
        )
        if not isinstance(result, dict):
            raise RuntimeError("Malformed response for listRunConfigurations")
        return result

    def run_configuration(
        self,
        name: str,
        *,
        project_key: str | None = None,
    ) -> dict[str, Any]:
        result = self.call(
            method="runConfiguration",
            params={"name": name},
            project_key=project_key,
        )
        if not isinstance(result, dict):
            raise RuntimeError("Malformed response for runConfiguration")
        return result

    def run_tests(
        self,
        *,
        project_key: str | None = None,
        configuration_name: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if configuration_name is not None:
            params["configurationName"] = configuration_name

        result = self.call(
            method="runTests",
            params=params,
            project_key=project_key,
        )
        if not isinstance(result, dict):
            raise RuntimeError("Malformed response for runTests")
        return result

    def get_diagnostics(
        self,
        path: str,
        *,
        project_key: str | None = None,
        severity: str | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "path": path,
            "limit": limit,
        }
        if severity is not None:
            params["severity"] = severity

        result = self.call(
            method="getDiagnostics",
            params=params,
            project_key=project_key,
        )
        if not isinstance(result, dict):
            raise RuntimeError("Malformed response for getDiagnostics")
        return result

    def get_completions(
        self,
        path: str,
        offset: int,
        *,
        project_key: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        result = self.call(
            method="getCompletions",
            params={
                "path": path,
                "offset": offset,
                "limit": limit,
            },
            project_key=project_key,
        )
        if not isinstance(result, dict):
            raise RuntimeError("Malformed response for getCompletions")
        return result

    def get_hover(
        self,
        path: str,
        offset: int,
        *,
        project_key: str | None = None,
    ) -> dict[str, Any]:
        result = self.call(
            method="getHover",
            params={
                "path": path,
                "offset": offset,
            },
            project_key=project_key,
        )
        if not isinstance(result, dict):
            raise RuntimeError("Malformed response for getHover")
        return result

    def get_signature_help(
        self,
        path: str,
        offset: int,
        *,
        project_key: str | None = None,
    ) -> dict[str, Any]:
        result = self.call(
            method="getSignatureHelp",
            params={
                "path": path,
                "offset": offset,
            },
            project_key=project_key,
        )
        if not isinstance(result, dict):
            raise RuntimeError("Malformed response for getSignatureHelp")
        return result

    def get_code_actions(
        self,
        path: str,
        offset: int,
        *,
        project_key: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        result = self.call(
            method="getCodeActions",
            params={
                "path": path,
                "offset": offset,
                "limit": limit,
            },
            project_key=project_key,
        )
        if not isinstance(result, dict):
            raise RuntimeError("Malformed response for getCodeActions")
        return result

    def apply_code_action(
        self,
        action_id: str,
        path: str,
        offset: int,
        *,
        project_key: str | None = None,
    ) -> dict[str, Any]:
        result = self.call(
            method="applyCodeAction",
            params={
                "actionId": action_id,
                "path": path,
                "offset": offset,
            },
            project_key=project_key,
        )
        if not isinstance(result, dict):
            raise RuntimeError("Malformed response for applyCodeAction")
        return result

    def format_file(
        self,
        path: str,
        *,
        project_key: str | None = None,
    ) -> dict[str, Any]:
        result = self.call(
            method="formatFile",
            params={"path": path},
            project_key=project_key,
        )
        if not isinstance(result, dict):
            raise RuntimeError("Malformed response for formatFile")
        return result

    def format_range(
        self,
        path: str,
        start_line: int,
        start_character: int,
        end_line: int,
        end_character: int,
        *,
        project_key: str | None = None,
    ) -> dict[str, Any]:
        result = self.call(
            method="formatRange",
            params={
                "path": path,
                "range": {
                    "start": {
                        "line": start_line,
                        "character": start_character,
                    },
                    "end": {
                        "line": end_line,
                        "character": end_character,
                    },
                },
            },
            project_key=project_key,
        )
        if not isinstance(result, dict):
            raise RuntimeError("Malformed response for formatRange")
        return result

    def optimize_imports(
        self,
        path: str,
        *,
        project_key: str | None = None,
    ) -> dict[str, Any]:
        result = self.call(
            method="optimizeImports",
            params={"path": path},
            project_key=project_key,
        )
        if not isinstance(result, dict):
            raise RuntimeError("Malformed response for optimizeImports")
        return result

    def reformat(
        self,
        path: str,
        *,
        project_key: str | None = None,
    ) -> dict[str, Any]:
        result = self.call(
            method="reformat",
            params={"path": path},
            project_key=project_key,
        )
        if not isinstance(result, dict):
            raise RuntimeError("Malformed response for reformat")
        return result

    def unsafe_get_status(self) -> dict[str, Any]:
        result = self.call(
            method="unsafe.getStatus",
            params={},
        )
        if not isinstance(result, dict):
            raise RuntimeError("Malformed response for unsafe.getStatus")
        return result

    def unsafe_invoke(
        self,
        *,
        target: dict[str, Any],
        method: str,
        args: list[Any] | None = None,
        return_handle: bool = True,
        capability_tokens: list[str] | None = None,
    ) -> dict[str, Any]:
        status = self.unsafe_get_status()
        if not bool(status.get("enabled", False)):
            raise RuntimeError(
                "Unsafe API is disabled in IntelliJ plugin. "
                "Enable it via INTELLIJ_BRIDGE_ENABLE_UNSAFE=true or -Dintellij.bridge.enable.unsafe=true."
            )

        if not isinstance(target, dict):
            raise ValueError("target must be an object")

        class_name = target.get("className")
        if isinstance(class_name, str) and not class_name.startswith("com.intellij."):
            raise ValueError("Unsafe className must start with 'com.intellij.'")

        tokens = set(capability_tokens or [])
        tokens.add("unsafe.invoke")

        result = self.call(
            method="unsafe.invoke",
            params={
                "target": target,
                "method": method,
                "args": args or [],
                "returnHandle": return_handle,
            },
            capability_tokens=sorted(tokens),
        )
        if not isinstance(result, dict):
            raise RuntimeError("Malformed response for unsafe.invoke")
        return result
