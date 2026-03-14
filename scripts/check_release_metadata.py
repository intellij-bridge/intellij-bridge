from __future__ import annotations

import json
import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
VERSION = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()


def _require_contains(path: Path, needle: str) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [] if needle in text else [f"{path}: missing {needle!r}"]


def _require_json_version(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    actual = data.get("version")
    if actual != VERSION:
        return [f"{path}: expected version {VERSION}, found {actual}"]
    return []


def _require_toml_version(path: Path) -> list[str]:
    match = re.search(
        r'^version\s*=\s*"([^"]+)"', path.read_text(encoding="utf-8"), re.MULTILINE
    )
    if not match:
        return [f"{path}: missing version field"]
    actual = match.group(1)
    if actual != VERSION:
        return [f"{path}: expected version {VERSION}, found {actual}"]
    return []


def main() -> int:
    errors: list[str] = []
    gradle_text = (REPO_ROOT / "intellij-plugin" / "build.gradle.kts").read_text(
        encoding="utf-8"
    )
    errors.extend(_require_toml_version(REPO_ROOT / "bridge" / "pyproject.toml"))
    errors.extend(
        _require_json_version(
            REPO_ROOT / "opencode-intellij-lsp-plugin" / "package.json"
        )
    )
    errors.extend(
        _require_json_version(
            REPO_ROOT / "claude-code-plugin" / ".claude-plugin" / "plugin.json"
        )
    )
    if (
        'val repoVersion = Files.readString(rootProject.projectDir.toPath().resolve("..")'
        not in gradle_text
    ):
        errors.append(
            "intellij-plugin/build.gradle.kts: missing VERSION-file based repoVersion"
        )
    if "version = repoVersion" not in gradle_text:
        errors.append(
            "intellij-plugin/build.gradle.kts: missing 'version = repoVersion'"
        )
    errors.extend(
        _require_contains(
            REPO_ROOT / "bridge" / "ijbridge" / "lsp" / "server.py",
            "LSP_SERVER_VERSION = PACKAGE_VERSION",
        )
    )

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    print(f"release metadata aligned to {VERSION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
