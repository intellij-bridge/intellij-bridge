from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path


def _read_repo_version() -> str | None:
    current = Path(__file__).resolve()
    for parent in current.parents:
        candidate = parent / "VERSION"
        if candidate.exists():
            return candidate.read_text(encoding="utf-8").strip()
    return None


def resolve_version() -> str:
    try:
        return package_version("ijbridge")
    except PackageNotFoundError:
        repo_version = _read_repo_version()
        if repo_version:
            return repo_version
        return "0.1.0"


PACKAGE_VERSION = resolve_version()
