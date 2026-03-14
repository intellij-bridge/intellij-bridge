from __future__ import annotations

import json
import re
from pathlib import Path


def product_info_path_for_app(app_path: Path) -> Path:
    if app_path.suffix == ".app":
        return app_path / "Contents" / "Resources" / "product-info.json"
    return app_path / "product-info.json"


def load_product_info(app_path: Path) -> dict[str, object] | None:
    product_info_path = product_info_path_for_app(app_path)
    if not product_info_path.exists():
        return None

    try:
        content = product_info_path.read_text(encoding="utf-8")
        parsed = json.loads(content)
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(parsed, dict):
        return None
    return parsed


def is_intellij_install(name: str, app_name: str) -> bool:
    merged = f"{name} {app_name}".lower()
    return "intellij idea" in merged


def to_str(value: object | None) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def version_key(version: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", version)
    if not numbers:
        return (0,)
    return tuple(int(value) for value in numbers[:4])
