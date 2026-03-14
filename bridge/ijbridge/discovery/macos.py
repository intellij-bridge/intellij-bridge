from __future__ import annotations

import re
import shutil
from pathlib import Path

from .common import (
    is_intellij_install,
    load_product_info,
    product_info_path_for_app,
    to_str,
    version_key,
)
from .model import IntelliJInstall


def _candidate_roots() -> list[tuple[Path, str]]:
    home = Path.home()
    return [
        (Path("/Applications"), "system-applications"),
        (home / "Applications", "user-applications"),
        (
            home / "Library" / "Application Support" / "JetBrains" / "Toolbox" / "apps",
            "toolbox",
        ),
    ]


def _iter_candidate_apps() -> list[tuple[Path, str]]:
    seen: set[Path] = set()
    candidates: list[tuple[Path, str]] = []

    for root, source in _candidate_roots():
        if not root.exists():
            continue
        for app_path in root.rglob("*.app"):
            resolved = app_path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            candidates.append((resolved, source))

    script_path = shutil.which("idea")
    if script_path:
        app_from_script = _resolve_app_from_launcher(Path(script_path))
        if app_from_script is not None and app_from_script not in seen:
            candidates.append((app_from_script, "launcher-script"))

    return candidates


def _resolve_app_from_launcher(script_path: Path) -> Path | None:
    resolved = script_path.expanduser().resolve()

    for parent in (resolved, *resolved.parents):
        if parent.name.endswith(".app"):
            return parent

    try:
        script_text = resolved.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None

    match = re.search(r"(/[^\n\"]+?\.app)", script_text)
    if not match:
        return None

    candidate = Path(match.group(1)).expanduser()
    if candidate.exists():
        return candidate.resolve()
    return None


def _parse_install(app_path: Path, source: str) -> IntelliJInstall | None:
    info = load_product_info(app_path)
    app_name = app_path.stem

    if info is None:
        if "intellij" not in app_name.lower():
            return None
        return IntelliJInstall(
            app_path=str(app_path),
            product_name=app_name,
            product_code=None,
            version="unknown",
            build_number=None,
            data_directory_name=None,
            config_dir=None,
            plugins_dir=None,
            product_info_path=None,
            source=source,
        )

    product_name = to_str(info.get("name")) or app_name
    if not is_intellij_install(product_name, app_name):
        return None

    data_directory_name = to_str(info.get("dataDirectoryName"))
    config_dir = None
    plugins_dir = None
    if data_directory_name is not None:
        config_path = (
            Path.home()
            / "Library"
            / "Application Support"
            / "JetBrains"
            / data_directory_name
        )
        config_dir = str(config_path)
        plugins_dir = str(config_path / "plugins")

    return IntelliJInstall(
        app_path=str(app_path),
        product_name=product_name,
        product_code=to_str(info.get("productCode")),
        version=to_str(info.get("version")) or "unknown",
        build_number=to_str(info.get("buildNumber")),
        data_directory_name=data_directory_name,
        config_dir=config_dir,
        plugins_dir=plugins_dir,
        product_info_path=str(product_info_path_for_app(app_path)),
        source=source,
    )


def discover_intellij(explicit_app_path: str | None = None) -> list[IntelliJInstall]:
    installs: dict[str, IntelliJInstall] = {}

    if explicit_app_path:
        explicit = Path(explicit_app_path).expanduser().resolve()
        if explicit.exists():
            parsed = _parse_install(explicit, "explicit")
            if parsed is not None:
                installs[parsed.app_path] = parsed

    for app_path, source in _iter_candidate_apps():
        parsed = _parse_install(app_path, source)
        if parsed is None:
            continue
        installs[parsed.app_path] = parsed

    ordered = sorted(
        installs.values(),
        key=lambda install: (version_key(install.version), install.app_path),
        reverse=True,
    )
    return ordered
