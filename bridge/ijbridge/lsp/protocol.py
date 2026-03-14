from __future__ import annotations

import json
import sys
from typing import Any


class JsonRpcProtocolError(RuntimeError):
    def __init__(
        self, code: int, message: str, data: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def read_message(input_stream: Any) -> dict[str, Any] | None:
    headers: dict[str, str] = {}

    while True:
        line = input_stream.readline()
        if not line:
            if headers:
                raise JsonRpcProtocolError(
                    -32700, "Unexpected EOF while reading headers"
                )
            return None

        if line in {b"\r\n", b"\n"}:
            break

        try:
            decoded = line.decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise JsonRpcProtocolError(-32700, "Header is not valid ASCII") from exc

        if ":" not in decoded:
            raise JsonRpcProtocolError(-32700, f"Malformed header line: {decoded}")

        name, value = decoded.split(":", 1)
        headers[name.strip().lower()] = value.strip()

    content_length_raw = headers.get("content-length")
    if content_length_raw is None:
        raise JsonRpcProtocolError(-32700, "Missing Content-Length header")

    try:
        content_length = int(content_length_raw)
    except ValueError as exc:
        raise JsonRpcProtocolError(-32700, "Invalid Content-Length header") from exc

    if content_length < 0:
        raise JsonRpcProtocolError(-32700, "Content-Length cannot be negative")

    body = input_stream.read(content_length)
    if len(body) != content_length:
        raise JsonRpcProtocolError(-32700, "Unexpected EOF while reading body")

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JsonRpcProtocolError(-32700, "Request body is not valid JSON") from exc

    if not isinstance(payload, dict):
        raise JsonRpcProtocolError(-32600, "JSON-RPC payload must be an object")

    return payload


def write_message(
    output_stream: Any, payload: dict[str, Any], *, flush: bool = True
) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    output_stream.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii"))
    output_stream.write(encoded)
    if flush:
        output_stream.flush()


def log_stderr(message: str) -> None:
    print(message, file=sys.stderr, flush=True)
