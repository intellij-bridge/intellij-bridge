from __future__ import annotations

import argparse
import sys
import traceback
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from ..bootstrap import ensure_bootstrapped
from ..config import get_connection_file_path, load_bridge_config
from ..daemon import DaemonError
from ..rpc import IntelliJRpcClient, RpcError
from ..version import PACKAGE_VERSION
from .protocol import JsonRpcProtocolError, log_stderr, read_message, write_message


JSON_RPC_VERSION = "2.0"
LSP_SERVER_NAME = "intellibridge"
LSP_SERVER_VERSION = PACKAGE_VERSION


def _to_uri(path: str) -> str:
    return Path(path).resolve().as_uri()


def _from_uri(uri: str) -> str:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise JsonRpcProtocolError(-32602, f"Unsupported URI: {uri}")

    if parsed.netloc and parsed.netloc not in ("", "localhost"):
        raise JsonRpcProtocolError(-32602, f"Unsupported file URI host: {uri}")

    return str(Path(url2pathname(unquote(parsed.path))).resolve())


def _utf16_code_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _index_from_utf16_units(text: str, units: int, context: str) -> int:
    if units < 0:
        raise JsonRpcProtocolError(-32602, f"{context} cannot be negative")

    consumed = 0
    for index, char in enumerate(text):
        char_units = 2 if ord(char) > 0xFFFF else 1
        next_consumed = consumed + char_units
        if next_consumed > units:
            raise JsonRpcProtocolError(
                -32602,
                f"{context} splits a UTF-16 surrogate pair",
            )
        if next_consumed == units:
            return index + 1
        consumed = next_consumed

    if consumed == units:
        return len(text)

    raise JsonRpcProtocolError(-32602, f"{context} out of range")


def _normalize_line_text(line_text: str) -> str:
    if line_text.endswith("\r\n"):
        return line_text[:-2]
    if line_text.endswith("\n") or line_text.endswith("\r"):
        return line_text[:-1]
    return line_text


def _line_offsets(text: str) -> tuple[int, ...]:
    lines = text.splitlines(keepends=True)
    if not lines:
        return (0,)

    offsets = [0]
    total = 0
    for chunk in lines[:-1]:
        total += len(chunk)
        offsets.append(total)
    return tuple(offsets)


def _position_to_offset(text: str, line: int, character: int) -> int:
    if line < 0 or character < 0:
        raise JsonRpcProtocolError(-32602, "Position values cannot be negative")

    lines = text.splitlines(keepends=True)
    if not lines:
        lines = [""]

    if line >= len(lines):
        if line == len(lines) and character == 0:
            return len(text)
        raise JsonRpcProtocolError(-32602, f"Line {line} out of range")

    offset = sum(len(chunk) for chunk in lines[:line])
    line_text = _normalize_line_text(lines[line])

    line_index = _index_from_utf16_units(
        line_text,
        character,
        f"Character {character} out of range for line {line}",
    )
    return offset + line_index


def _ensure_dict(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise JsonRpcProtocolError(-32602, f"{context} must be an object")
    return value


def _ensure_list(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise JsonRpcProtocolError(-32602, f"{context} must be an array")
    return value


def _ensure_str(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise JsonRpcProtocolError(-32602, f"{context} must be a non-empty string")
    return value


@dataclass
class DocumentState:
    uri: str
    path: str
    text: str
    version: int | None
    language_id: str | None
    line_offsets: tuple[int, ...]
    synced_version: int | None = None
    synced_text: str | None = None
    diagnostics: list[dict[str, Any]] | None = None


class BridgeTransport:
    def __init__(self, timeout_seconds: float, connection_file: Path) -> None:
        self.timeout_seconds = timeout_seconds
        self.connection_file = connection_file
        self._client: IntelliJRpcClient | None = None

    def _client_instance(self) -> IntelliJRpcClient:
        if self._client is None:
            self._client = IntelliJRpcClient.from_connection_file(
                self.connection_file,
                wait_seconds=self.timeout_seconds,
                timeout_seconds=self.timeout_seconds,
            )
        return self._client

    def call(self, method: str, params: dict[str, Any], project_key: str | None) -> Any:
        return self._client_instance().call(
            method=method,
            params=params,
            project_key=project_key,
        )


class LspSession:
    def __init__(self, timeout_seconds: float, connection_file: Path) -> None:
        self.timeout_seconds = timeout_seconds
        self.connection_file = connection_file
        self.transport = BridgeTransport(timeout_seconds, connection_file)
        self.documents: dict[str, DocumentState] = {}
        self.project_key: str | None = None
        self.shutdown_requested = False
        self._diagnostics_dirty: set[str] = set()

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method")
        if not isinstance(method, str) or not method:
            raise JsonRpcProtocolError(-32600, "Missing JSON-RPC method")

        request_id = request.get("id")
        params = request.get("params")
        if params is None:
            params = {}
        params_dict = _ensure_dict(params, "params")

        if method == "initialize":
            return self._response(request_id, self._handle_initialize(params_dict))
        if method == "initialized":
            return None
        if method == "shutdown":
            self.shutdown_requested = True
            return self._response(request_id, None)
        if method == "exit":
            raise SystemExit(0 if self.shutdown_requested else 1)
        if method == "workspace/didChangeConfiguration":
            return None
        if method == "textDocument/didOpen":
            self._handle_did_open(params_dict)
            return None
        if method == "textDocument/didChange":
            self._handle_did_change(params_dict)
            return None
        if method == "textDocument/didClose":
            self._handle_did_close(params_dict)
            return None
        if method == "textDocument/didSave":
            self._handle_did_save(params_dict)
            return None
        if method == "textDocument/diagnostic":
            return self._response(
                request_id, self._handle_document_diagnostic(params_dict)
            )
        if method == "textDocument/hover":
            return self._response(request_id, self._handle_hover(params_dict))
        if method == "textDocument/completion":
            return self._response(request_id, self._handle_completion(params_dict))
        if method == "textDocument/definition":
            return self._response(request_id, self._handle_definition(params_dict))
        if method == "textDocument/references":
            return self._response(request_id, self._handle_references(params_dict))
        if method == "textDocument/prepareRename":
            return self._response(request_id, self._handle_prepare_rename(params_dict))
        if method == "textDocument/rename":
            return self._response(request_id, self._handle_rename(params_dict))
        if method == "textDocument/codeAction":
            return self._response(request_id, self._handle_code_action(params_dict))
        if method == "workspace/executeCommand":
            return self._response(request_id, self._handle_execute_command(params_dict))
        if method == "textDocument/formatting":
            return self._response(request_id, self._handle_formatting(params_dict))
        if method == "textDocument/rangeFormatting":
            return self._response(
                request_id, self._handle_range_formatting(params_dict)
            )

        raise JsonRpcProtocolError(-32601, f"Method not found: {method}")

    def _response(self, request_id: Any, result: Any) -> dict[str, Any]:
        return {
            "jsonrpc": JSON_RPC_VERSION,
            "id": request_id,
            "result": result,
        }

    def _request(self, method: str, params: dict[str, Any]) -> Any:
        try:
            return self.transport.call(method, params, self.project_key)
        except RpcError as exc:
            raise JsonRpcProtocolError(exc.code, exc.message, exc.data) from exc
        except (DaemonError, TimeoutError, RuntimeError) as exc:
            raise JsonRpcProtocolError(-32603, str(exc)) from exc

    def _document(self, uri: str) -> DocumentState:
        state = self.documents.get(uri)
        if state is None:
            raise JsonRpcProtocolError(-32602, f"Document is not open: {uri}")
        return state

    def _resolve_document_and_offset(
        self,
        text_document: dict[str, Any],
        position: dict[str, Any],
    ) -> tuple[DocumentState, int]:
        uri = _ensure_str(text_document.get("uri"), "textDocument.uri")
        state = self._document(uri)
        line = position.get("line")
        character = position.get("character")
        if not isinstance(line, int) or not isinstance(character, int):
            raise JsonRpcProtocolError(
                -32602, "Position must contain integer line and character"
            )
        return state, self._position_to_offset_for_state(state, line, character)

    def _resolve_flushed_document_and_offset(
        self,
        text_document: dict[str, Any],
        position: dict[str, Any],
    ) -> tuple[DocumentState, int]:
        state, offset = self._resolve_document_and_offset(text_document, position)
        self._flush_before_interactive_request(state)
        return state, offset

    def _position_to_offset_for_state(
        self, state: DocumentState, line: int, character: int
    ) -> int:
        if line < 0 or character < 0:
            raise JsonRpcProtocolError(-32602, "Position values cannot be negative")

        if line >= len(state.line_offsets):
            if line == len(state.line_offsets) and character == 0:
                return len(state.text)
            raise JsonRpcProtocolError(-32602, f"Line {line} out of range")

        start_offset = state.line_offsets[line]
        end_offset = (
            state.line_offsets[line + 1]
            if line + 1 < len(state.line_offsets)
            else len(state.text)
        )
        line_text = _normalize_line_text(state.text[start_offset:end_offset])
        line_index = _index_from_utf16_units(
            line_text,
            character,
            f"Character {character} out of range for line {line}",
        )
        return start_offset + line_index

    def _pull_diagnostics(self, state: DocumentState) -> None:
        diagnostics = self._load_diagnostics(state.path)

        if state.diagnostics == diagnostics:
            self._diagnostics_dirty.discard(state.uri)
            return

        state.diagnostics = diagnostics
        self._diagnostics_dirty.discard(state.uri)

        write_message(
            sys.stdout.buffer,
            {
                "jsonrpc": JSON_RPC_VERSION,
                "method": "textDocument/publishDiagnostics",
                "params": {
                    "uri": state.uri,
                    "diagnostics": diagnostics,
                },
            },
            flush=False,
        )

    def _bridge_text(self, path: str, context: str) -> str:
        result = self._request("getDocumentText", {"path": path})
        if not isinstance(result, dict):
            raise JsonRpcProtocolError(-32603, f"{context} response was malformed")
        text = result.get("text")
        if not isinstance(text, str):
            raise JsonRpcProtocolError(-32603, f"{context} text payload was malformed")
        return text

    def _load_diagnostics(self, path: str) -> list[dict[str, Any]]:
        try:
            result = self._request("getFileProblems", {"path": path, "limit": 500})
        except JsonRpcProtocolError:
            result = self._request("getDiagnostics", {"path": path, "limit": 500})

        if not isinstance(result, dict):
            return []

        raw_items = result.get("diagnostics")
        if not isinstance(raw_items, list):
            return []

        return [
            self._to_lsp_diagnostic(item)
            for item in raw_items
            if isinstance(item, dict)
        ]

    def _refresh_document_from_bridge(self, state: DocumentState, context: str) -> None:
        state.text = self._bridge_text(state.path, context)
        state.line_offsets = _line_offsets(state.text)
        state.synced_text = state.text
        state.synced_version = state.version

    def _refresh_document_from_result(
        self, result: Any, state: DocumentState, context: str
    ) -> None:
        if isinstance(result, dict):
            text = result.get("text")
            if isinstance(text, str):
                state.text = text
                state.line_offsets = _line_offsets(text)
                state.synced_text = text
                state.synced_version = state.version
                return
        self._refresh_document_from_bridge(state, context)

    def _finalize_document_mutation(
        self,
        state: DocumentState,
        context: str,
        *,
        result: Any | None = None,
    ) -> None:
        if result is None:
            self._refresh_document_from_bridge(state, context)
        else:
            self._refresh_document_from_result(result, state, context)
        self._mark_diagnostics_dirty(state)
        self._pull_diagnostics(state)

    def _mark_diagnostics_dirty(self, state: DocumentState) -> None:
        self._diagnostics_dirty.add(state.uri)

    def _publish_empty_diagnostics(self, uri: str) -> None:
        write_message(
            sys.stdout.buffer,
            {
                "jsonrpc": JSON_RPC_VERSION,
                "method": "textDocument/publishDiagnostics",
                "params": {"uri": uri, "diagnostics": []},
            },
            flush=False,
        )

    def _document_needs_sync(self, state: DocumentState) -> bool:
        return state.synced_version != state.version or state.synced_text != state.text

    def _ensure_document_synced(self, state: DocumentState) -> None:
        if not self._document_needs_sync(state):
            return
        self._push_document(state)

    def _flush_document_state(self, state: DocumentState) -> None:
        self._ensure_document_synced(state)
        if state.uri in self._diagnostics_dirty:
            self._pull_diagnostics(state)

    def _flush_before_interactive_request(self, state: DocumentState) -> None:
        self._flush_document_state(state)

    def _flush_all_pending_documents(self) -> None:
        for state in self.documents.values():
            self._flush_document_state(state)

    def _to_lsp_diagnostic(self, item: dict[str, Any]) -> dict[str, Any]:
        severity_map = {
            "error": 1,
            "warning": 2,
            "information": 3,
            "hint": 4,
        }
        severity_name = item.get("severity")
        diagnostic: dict[str, Any] = {
            "range": item.get("range", {}),
            "message": item.get("message", "Problem"),
            "severity": severity_map.get(severity_name, 3)
            if isinstance(severity_name, str)
            else 3,
            "source": LSP_SERVER_NAME,
        }
        return diagnostic

    def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        initialization_options = params.get("initializationOptions")
        if isinstance(initialization_options, dict):
            project_key = initialization_options.get("projectKey")
            if isinstance(project_key, str) and project_key:
                self.project_key = project_key

        server_info = self._request("getIdeInfo", {})
        if self.project_key is None:
            projects = self._request("listOpenProjects", {})
            if isinstance(projects, dict):
                project_entries = projects.get("projects")
                if isinstance(project_entries, list) and len(project_entries) == 1:
                    only = project_entries[0]
                    if isinstance(only, dict):
                        only_key = only.get("projectKey")
                        if isinstance(only_key, str) and only_key:
                            self.project_key = only_key

        server_name = LSP_SERVER_NAME
        if isinstance(server_info, dict):
            product_name = server_info.get("productName")
            if isinstance(product_name, str) and product_name:
                server_name = f"{product_name} bridge"

        return {
            "capabilities": {
                "positionEncoding": "utf-16",
                "textDocumentSync": {
                    "openClose": True,
                    "change": 1,
                    "save": {"includeText": False},
                },
                "hoverProvider": True,
                "completionProvider": {
                    "resolveProvider": False,
                    "triggerCharacters": [".", ":", ">", "(", ","],
                },
                "definitionProvider": True,
                "referencesProvider": True,
                "diagnosticProvider": {
                    "interFileDependencies": False,
                    "workspaceDiagnostics": False,
                },
                "renameProvider": {"prepareProvider": True},
                "codeActionProvider": True,
                "documentFormattingProvider": True,
                "documentRangeFormattingProvider": True,
                "executeCommandProvider": {
                    "commands": ["intellibridge.applyCodeAction"],
                },
            },
            "serverInfo": {
                "name": server_name,
                "version": LSP_SERVER_VERSION,
            },
        }

    def _handle_did_open(self, params: dict[str, Any]) -> None:
        text_document = _ensure_dict(params.get("textDocument"), "textDocument")
        uri = _ensure_str(text_document.get("uri"), "textDocument.uri")
        text = text_document.get("text")
        if not isinstance(text, str):
            raise JsonRpcProtocolError(-32602, "textDocument.text must be a string")

        self.documents[uri] = DocumentState(
            uri=uri,
            path=_from_uri(uri),
            text=text,
            version=text_document.get("version")
            if isinstance(text_document.get("version"), int)
            else None,
            language_id=text_document.get("languageId")
            if isinstance(text_document.get("languageId"), str)
            else None,
            line_offsets=_line_offsets(text),
        )
        state = self.documents[uri]
        self._mark_diagnostics_dirty(state)
        self._flush_document_state(state)

    def _handle_did_change(self, params: dict[str, Any]) -> None:
        text_document = _ensure_dict(params.get("textDocument"), "textDocument")
        uri = _ensure_str(text_document.get("uri"), "textDocument.uri")
        state = self._document(uri)
        changes = _ensure_list(params.get("contentChanges"), "contentChanges")
        if len(changes) != 1:
            raise JsonRpcProtocolError(-32602, "Only full document sync is supported")
        change = _ensure_dict(changes[0], "contentChanges[0]")
        text = change.get("text")
        if not isinstance(text, str):
            raise JsonRpcProtocolError(
                -32602, "contentChanges[0].text must be a string"
            )

        state.text = text
        state.line_offsets = _line_offsets(text)
        version = text_document.get("version")
        if isinstance(version, int):
            state.version = version
        self._mark_diagnostics_dirty(state)

    def _handle_did_close(self, params: dict[str, Any]) -> None:
        text_document = _ensure_dict(params.get("textDocument"), "textDocument")
        uri = _ensure_str(text_document.get("uri"), "textDocument.uri")
        self.documents.pop(uri, None)
        self._diagnostics_dirty.discard(uri)
        try:
            self._request("closeDocument", {"path": _from_uri(uri), "revert": True})
        except JsonRpcProtocolError:
            pass
        self._publish_empty_diagnostics(uri)

    def _handle_did_save(self, params: dict[str, Any]) -> None:
        text_document = _ensure_dict(params.get("textDocument"), "textDocument")
        uri = _ensure_str(text_document.get("uri"), "textDocument.uri")
        state = self._document(uri)
        self._mark_diagnostics_dirty(state)
        self._flush_document_state(state)

    def _push_document(self, state: DocumentState) -> None:
        self._request(
            "syncDocument",
            {
                "path": state.path,
                "text": state.text,
                **({"version": state.version} if state.version is not None else {}),
            },
        )
        state.synced_text = state.text
        state.synced_version = state.version

    def _handle_hover(self, params: dict[str, Any]) -> dict[str, Any] | None:
        state, offset = self._resolve_flushed_document_and_offset(
            _ensure_dict(params.get("textDocument"), "textDocument"),
            _ensure_dict(params.get("position"), "position"),
        )
        result = self._request("getHover", {"path": state.path, "offset": offset})
        if not isinstance(result, dict) or not result.get("resolved"):
            return None
        documentation = result.get("documentation")
        if not isinstance(documentation, str) or not documentation:
            return None
        return {"contents": {"kind": "markdown", "value": documentation}}

    def _handle_document_diagnostic(self, params: dict[str, Any]) -> dict[str, Any]:
        text_document = _ensure_dict(params.get("textDocument"), "textDocument")
        uri = _ensure_str(text_document.get("uri"), "textDocument.uri")
        path = _from_uri(uri)
        state = self.documents.get(uri)
        if state is not None:
            self._flush_document_state(state)
            diagnostics = state.diagnostics or []
        else:
            diagnostics = self._load_diagnostics(path)
        return {"kind": "full", "items": diagnostics}

    def _handle_completion(self, params: dict[str, Any]) -> dict[str, Any]:
        state, offset = self._resolve_flushed_document_and_offset(
            _ensure_dict(params.get("textDocument"), "textDocument"),
            _ensure_dict(params.get("position"), "position"),
        )
        result = self._request(
            "getCompletions", {"path": state.path, "offset": offset, "limit": 200}
        )
        if isinstance(result, dict) and result.get("status") == "not_ready":
            return {"isIncomplete": True, "items": []}
        items: list[dict[str, Any]] = []
        if isinstance(result, dict):
            raw_items = result.get("items")
            if isinstance(raw_items, list):
                for item in raw_items:
                    if not isinstance(item, dict):
                        continue
                    label = item.get("label") or item.get("lookupString")
                    if not isinstance(label, str) or not label:
                        continue
                    completion_item: dict[str, Any] = {"label": label}
                    detail = item.get("typeText")
                    if isinstance(detail, str) and detail:
                        completion_item["detail"] = detail
                    documentation = item.get("tailText")
                    if isinstance(documentation, str) and documentation:
                        completion_item["documentation"] = documentation
                    items.append(completion_item)
        return {"isIncomplete": False, "items": items}

    def _handle_definition(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        state, offset = self._resolve_flushed_document_and_offset(
            _ensure_dict(params.get("textDocument"), "textDocument"),
            _ensure_dict(params.get("position"), "position"),
        )
        result = self._request("getDefinitions", {"path": state.path, "offset": offset})
        if not isinstance(result, dict):
            return []
        raw_definitions = result.get("definitions")
        if not isinstance(raw_definitions, list):
            return []
        locations: list[dict[str, Any]] = []
        for item in raw_definitions:
            if not isinstance(item, dict):
                continue
            target_path = item.get("path")
            target_range = item.get("range")
            if not isinstance(target_path, str) or not isinstance(target_range, dict):
                continue
            locations.append({"uri": _to_uri(target_path), "range": target_range})
        return locations

    def _handle_references(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        state, offset = self._resolve_flushed_document_and_offset(
            _ensure_dict(params.get("textDocument"), "textDocument"),
            _ensure_dict(params.get("position"), "position"),
        )
        context = params.get("context")
        include_declaration = False
        if isinstance(context, dict):
            include_declaration = bool(context.get("includeDeclaration"))

        result = self._request(
            "findReferences",
            {"path": state.path, "offset": offset, "limit": 5000},
        )
        if not isinstance(result, dict):
            return []
        raw_references = result.get("references")
        if not isinstance(raw_references, list):
            return []

        locations: list[dict[str, Any]] = []
        seen: set[tuple[str, int, int]] = set()
        if not include_declaration:
            declaration = result.get("declaration")
            if isinstance(declaration, dict):
                declaration_path = declaration.get("path")
                declaration_range = declaration.get("range")
                if isinstance(declaration_path, str) and isinstance(
                    declaration_range, dict
                ):
                    start = declaration_range.get("start")
                    if isinstance(start, dict):
                        seen.add(
                            (
                                _to_uri(declaration_path),
                                int(start.get("line", -1)),
                                int(start.get("character", -1)),
                            )
                        )

        for item in raw_references:
            if not isinstance(item, dict):
                continue
            target_path = item.get("path")
            target_range = item.get("range")
            if not isinstance(target_path, str) or not isinstance(target_range, dict):
                continue
            start = target_range.get("start")
            if not isinstance(start, dict):
                continue
            uri = _to_uri(target_path)
            key = (
                uri,
                int(start.get("line", -1)),
                int(start.get("character", -1)),
            )
            if key in seen:
                continue
            locations.append({"uri": uri, "range": target_range})
        return locations

    def _handle_prepare_rename(self, params: dict[str, Any]) -> dict[str, Any] | None:
        state, offset = self._resolve_flushed_document_and_offset(
            _ensure_dict(params.get("textDocument"), "textDocument"),
            _ensure_dict(params.get("position"), "position"),
        )
        result = self._request("prepareRename", {"path": state.path, "offset": offset})
        if not isinstance(result, dict):
            return None
        range_value = result.get("range")
        placeholder = result.get("placeholder")
        if not isinstance(range_value, dict):
            return None
        payload: dict[str, Any] = {"range": range_value}
        if isinstance(placeholder, str) and placeholder:
            payload["placeholder"] = placeholder
        return payload

    def _handle_rename(self, params: dict[str, Any]) -> dict[str, Any]:
        state, offset = self._resolve_flushed_document_and_offset(
            _ensure_dict(params.get("textDocument"), "textDocument"),
            _ensure_dict(params.get("position"), "position"),
        )
        new_name = _ensure_str(params.get("newName"), "newName")
        before_text = state.text
        self._request(
            "renameSymbol",
            {"symbolRef": {"path": state.path, "offset": offset}, "newName": new_name},
        )
        self._finalize_document_mutation(state, "Rename")
        return {
            "documentChanges": [
                {
                    "textDocument": {
                        "uri": state.uri,
                        "version": state.version,
                    },
                    "edits": [
                        {
                            "range": self._full_range(before_text),
                            "newText": state.text,
                        }
                    ],
                }
            ]
        }

    def _handle_code_action(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        state, offset = self._resolve_flushed_document_and_offset(
            _ensure_dict(params.get("textDocument"), "textDocument"),
            _ensure_dict(
                params.get("range", {}).get("start")
                if isinstance(params.get("range"), dict)
                else params.get("position") or {},
                "position",
            ),
        )
        result = self._request(
            "getCodeActions", {"path": state.path, "offset": offset, "limit": 100}
        )
        actions: list[dict[str, Any]] = []
        if isinstance(result, dict):
            raw_actions = result.get("actions")
            if isinstance(raw_actions, list):
                for action in raw_actions:
                    if not isinstance(action, dict):
                        continue
                    title = action.get("title")
                    action_id = action.get("actionId")
                    if not isinstance(title, str) or not isinstance(action_id, str):
                        continue
                    actions.append(
                        {
                            "title": title,
                            "kind": action.get("kind") or "quickfix",
                            "command": {
                                "title": title,
                                "command": "intellibridge.applyCodeAction",
                                "arguments": [state.uri, offset, action_id],
                            },
                        }
                    )
        return actions

    def _handle_execute_command(self, params: dict[str, Any]) -> dict[str, Any]:
        command = _ensure_str(params.get("command"), "command")
        if command != "intellibridge.applyCodeAction":
            raise JsonRpcProtocolError(-32601, f"Unsupported command: {command}")
        arguments = _ensure_list(params.get("arguments"), "arguments")
        if len(arguments) != 3:
            raise JsonRpcProtocolError(
                -32602, "applyCodeAction expects [uri, offset, actionId]"
            )
        uri = _ensure_str(arguments[0], "arguments[0]")
        offset = arguments[1]
        action_id = _ensure_str(arguments[2], "arguments[2]")
        if not isinstance(offset, int):
            raise JsonRpcProtocolError(-32602, "arguments[1] must be an integer offset")
        state = self._document(uri)
        self._flush_before_interactive_request(state)
        self._request(
            "applyCodeAction",
            {"path": state.path, "offset": offset, "actionId": action_id},
        )
        self._finalize_document_mutation(state, "Code action")
        return {"applied": True}

    def _handle_formatting(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        text_document = _ensure_dict(params.get("textDocument"), "textDocument")
        uri = _ensure_str(text_document.get("uri"), "textDocument.uri")
        state = self._document(uri)
        before = state.text
        self._flush_before_interactive_request(state)
        result = self._request("formatFile", {"path": state.path})
        self._finalize_document_mutation(state, "Formatter", result=result)
        return [{"range": self._full_range(before), "newText": state.text}]

    def _handle_range_formatting(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        text_document = _ensure_dict(params.get("textDocument"), "textDocument")
        uri = _ensure_str(text_document.get("uri"), "textDocument.uri")
        state = self._document(uri)
        before = state.text
        range_value = _ensure_dict(params.get("range"), "range")
        start = _ensure_dict(range_value.get("start"), "range.start")
        end = _ensure_dict(range_value.get("end"), "range.end")
        self._flush_before_interactive_request(state)
        result = self._request(
            "formatRange",
            {
                "path": state.path,
                "range": {"start": start, "end": end},
            },
        )
        self._finalize_document_mutation(state, "Range formatter", result=result)
        return [{"range": self._full_range(before), "newText": state.text}]

    def _offset_to_position(self, text: str, offset: int) -> tuple[int, int]:
        safe_offset = max(0, min(len(text), offset))
        line_offsets = _line_offsets(text)
        line_index = bisect_right(line_offsets, safe_offset) - 1
        line_start = line_offsets[max(line_index, 0)]
        return (max(line_index, 0), _utf16_code_units(text[line_start:safe_offset]))

    def _full_range(self, text: str) -> dict[str, Any]:
        end_line, end_character = self._offset_to_position(text, len(text))
        return {
            "start": {"line": 0, "character": 0},
            "end": {"line": end_line, "character": end_character},
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ijbridge-lsp",
        description="Run the intellibridge stdio LSP server",
    )
    parser.add_argument(
        "--connection-file", help="Path to IntelliJ bridge connection file"
    )
    parser.add_argument(
        "--timeout", type=float, help="Bridge request timeout in seconds"
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = load_bridge_config(Path.cwd())
    timeout_seconds = (
        float(args.timeout)
        if args.timeout is not None
        else config.request_timeout_seconds
    )
    connection_file = (
        Path(args.connection_file).expanduser()
        if args.connection_file
        else get_connection_file_path(config)
    )
    ensure_bootstrapped(
        config=config,
        connection_file=connection_file,
        timeout_seconds=timeout_seconds,
        project_path=Path.cwd(),
    )
    session = LspSession(
        timeout_seconds=timeout_seconds, connection_file=connection_file
    )
    message: dict[str, Any] | None = None

    while True:
        try:
            message = None
            message = read_message(sys.stdin.buffer)
            if message is None:
                session._flush_all_pending_documents()
                return 0
            response = session.handle(message)
            if response is not None:
                write_message(sys.stdout.buffer, response)
        except JsonRpcProtocolError as exc:
            request_id = None
            if "message" in locals() and isinstance(message, dict):
                request_id = message.get("id")
            if request_id is not None:
                write_message(
                    sys.stdout.buffer,
                    {
                        "jsonrpc": JSON_RPC_VERSION,
                        "id": request_id,
                        "error": {
                            "code": exc.code,
                            "message": exc.message,
                            **({"data": exc.data} if exc.data is not None else {}),
                        },
                    },
                )
            else:
                log_stderr(f"Protocol error: {exc.message}")
        except SystemExit as exc:
            return int(exc.code) if isinstance(exc.code, int) else 0
        except Exception as exc:
            log_stderr(f"Unhandled LSP server error: {exc}")
            log_stderr(traceback.format_exc())
            return 1
