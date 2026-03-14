from __future__ import annotations

import platform

from .model import IntelliJInstall


def discover_intellij(explicit_app_path: str | None = None) -> list[IntelliJInstall]:
    system = platform.system()
    if system == "Darwin":
        from .macos import discover_intellij as discover_macos_intellij

        return discover_macos_intellij(explicit_app_path=explicit_app_path)

    if explicit_app_path is None:
        return []

    from pathlib import Path

    candidate = Path(explicit_app_path).expanduser().resolve()
    if not candidate.exists():
        return []

    return [
        IntelliJInstall(
            app_path=str(candidate),
            product_name=candidate.stem,
            product_code=None,
            version="unknown",
            build_number=None,
            data_directory_name=None,
            config_dir=None,
            plugins_dir=None,
            product_info_path=None,
            source="explicit",
        )
    ]


__all__ = ["IntelliJInstall", "discover_intellij"]
