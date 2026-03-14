from __future__ import annotations

import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
import re

from ijbridge.discovery import discover_intellij


@dataclass(frozen=True)
class PluginInstallResult:
    plugin_zip: str
    plugins_path: str
    installed_path: str
    replaced_existing: bool
    skipped_existing: bool
    plugin_version: str | None

    def to_dict(self) -> dict[str, str | bool | None]:
        return {
            "pluginZip": self.plugin_zip,
            "pluginsPath": self.plugins_path,
            "installedPath": self.installed_path,
            "replacedExisting": self.replaced_existing,
            "skippedExisting": self.skipped_existing,
            "pluginVersion": self.plugin_version,
        }


@dataclass(frozen=True)
class PluginArchiveMetadata:
    root_name: str
    version: str | None


def _resolve_plugins_path(
    *,
    plugins_path: str | None,
    app_path: str | None,
) -> Path:
    if plugins_path:
        return Path(plugins_path).expanduser().resolve()

    installs = discover_intellij(explicit_app_path=app_path)
    if not installs:
        raise RuntimeError(
            "No IntelliJ installations available to resolve plugins path"
        )

    if app_path is not None:
        normalized = str(Path(app_path).expanduser().resolve())
        for install in installs:
            if install.app_path == normalized and install.plugins_dir:
                return Path(install.plugins_dir).expanduser().resolve()
        raise RuntimeError(f"Could not resolve plugins path for app: {normalized}")

    first_with_plugins = next(
        (install for install in installs if install.plugins_dir), None
    )
    if first_with_plugins is None:
        raise RuntimeError(
            "Discovered IntelliJ installs, but none provided plugins_dir"
        )

    plugins_dir = first_with_plugins.plugins_dir
    if plugins_dir is None:
        raise RuntimeError("Discovered install has null plugins_dir")

    return Path(plugins_dir).expanduser().resolve()


def _extract_single_root(plugin_zip: Path, destination: Path) -> tuple[Path, bool]:
    with tempfile.TemporaryDirectory(prefix="ijbridge-plugin-") as temp_dir:
        temp_root = Path(temp_dir)
        with zipfile.ZipFile(plugin_zip, "r") as archive:
            _extract_archive_safely(archive, temp_root)

        roots = [path for path in temp_root.iterdir() if path.is_dir()]
        if len(roots) != 1:
            raise RuntimeError(
                "Expected plugin zip to contain exactly one top-level directory; "
                f"found {len(roots)}"
            )

        extracted_root = roots[0]
        target = destination / extracted_root.name
        replaced = target.exists()
        if replaced:
            shutil.rmtree(target)

        shutil.copytree(extracted_root, target)
        return target, replaced


def _read_archive_metadata(plugin_zip: Path) -> PluginArchiveMetadata:
    with zipfile.ZipFile(plugin_zip, "r") as archive:
        roots = {
            Path(member.filename).parts[0]
            for member in archive.infolist()
            if member.filename and not member.filename.startswith("__MACOSX/")
        }

        roots.discard("")
        if len(roots) != 1:
            raise RuntimeError(
                "Expected plugin zip to contain exactly one top-level directory; "
                f"found {len(roots)}"
            )

        root_name = next(iter(roots))
        version: str | None = None
        jar_pattern = re.compile(
            rf"^{re.escape(root_name)}/lib/{re.escape(root_name)}-(.+)\.jar$"
        )

        for member in archive.infolist():
            match = jar_pattern.match(member.filename)
            if match is None:
                continue

            candidate = match.group(1)
            if candidate.endswith("-searchableOptions"):
                continue

            version = candidate
            break

    return PluginArchiveMetadata(root_name=root_name, version=version)


def _installed_plugin_version(installed_path: Path, root_name: str) -> str | None:
    lib_dir = installed_path / "lib"
    if not lib_dir.exists() or not lib_dir.is_dir():
        return None

    prefix = f"{root_name}-"
    for jar_path in sorted(lib_dir.glob("*.jar")):
        stem = jar_path.stem
        if not stem.startswith(prefix):
            continue

        version = stem[len(prefix) :]
        if version.endswith("-searchableOptions"):
            continue

        return version or None

    return None


def _extract_archive_safely(archive: zipfile.ZipFile, destination: Path) -> None:
    destination_root = destination.resolve()

    for member in archive.infolist():
        member_path = Path(member.filename)
        if member_path.is_absolute():
            raise RuntimeError(
                f"Plugin zip contains an absolute path entry: {member.filename}"
            )

        if any(part == ".." for part in member_path.parts):
            raise RuntimeError(
                f"Plugin zip contains an unsafe path entry: {member.filename}"
            )

        target_path = (destination_root / member_path).resolve()
        if (
            target_path != destination_root
            and destination_root not in target_path.parents
        ):
            raise RuntimeError(
                f"Plugin zip entry escapes extraction directory: {member.filename}"
            )

        if member.is_dir():
            target_path.mkdir(parents=True, exist_ok=True)
            continue

        target_path.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(member, "r") as source, target_path.open("wb") as target:
            shutil.copyfileobj(source, target)


def ensure_plugin_installed(
    *,
    plugin_zip: str,
    plugins_path: str | None = None,
    app_path: str | None = None,
) -> PluginInstallResult:
    plugin_zip_path = Path(plugin_zip).expanduser().resolve()
    if not plugin_zip_path.exists():
        raise FileNotFoundError(f"Plugin zip not found: {plugin_zip_path}")

    archive_metadata = _read_archive_metadata(plugin_zip_path)

    resolved_plugins_path = _resolve_plugins_path(
        plugins_path=plugins_path,
        app_path=app_path,
    )
    resolved_plugins_path.mkdir(parents=True, exist_ok=True)

    existing_path = resolved_plugins_path / archive_metadata.root_name
    if existing_path.exists() and existing_path.is_dir():
        installed_version = _installed_plugin_version(
            existing_path, archive_metadata.root_name
        )
        if (
            installed_version is not None
            and installed_version == archive_metadata.version
        ):
            return PluginInstallResult(
                plugin_zip=str(plugin_zip_path),
                plugins_path=str(resolved_plugins_path),
                installed_path=str(existing_path),
                replaced_existing=False,
                skipped_existing=True,
                plugin_version=installed_version,
            )

    installed_path, replaced_existing = _extract_single_root(
        plugin_zip_path,
        resolved_plugins_path,
    )

    return PluginInstallResult(
        plugin_zip=str(plugin_zip_path),
        plugins_path=str(resolved_plugins_path),
        installed_path=str(installed_path),
        replaced_existing=replaced_existing,
        skipped_existing=False,
        plugin_version=archive_metadata.version,
    )
