from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ijbridge.installer.plugin import _extract_single_root


def _write_zip(zip_path: Path, entries: dict[str, bytes]) -> None:
    with zipfile.ZipFile(zip_path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)


class PluginInstallerTests(unittest.TestCase):
    def test_extract_single_root_rejects_parent_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plugin_zip = root / "plugin.zip"
            _write_zip(
                plugin_zip,
                {
                    "plugin-root/lib/plugin.txt": b"ok",
                    "../escape.txt": b"bad",
                },
            )

            destination = root / "plugins"
            destination.mkdir()

            with self.assertRaisesRegex(RuntimeError, "unsafe path entry"):
                _extract_single_root(plugin_zip, destination)

    def test_extract_single_root_rejects_absolute_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plugin_zip = root / "plugin.zip"
            _write_zip(
                plugin_zip,
                {
                    "plugin-root/lib/plugin.txt": b"ok",
                    "/absolute.txt": b"bad",
                },
            )

            destination = root / "plugins"
            destination.mkdir()

            with self.assertRaisesRegex(RuntimeError, "absolute path entry"):
                _extract_single_root(plugin_zip, destination)

    def test_extract_single_root_installs_expected_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plugin_zip = root / "plugin.zip"
            _write_zip(
                plugin_zip,
                {
                    "intellij-bridge/plugin.xml": b"<idea-plugin/>",
                    "intellij-bridge/lib/bridge.jar": b"jar",
                },
            )

            destination = root / "plugins"
            destination.mkdir()

            installed_path, replaced_existing = _extract_single_root(
                plugin_zip,
                destination,
            )

            self.assertEqual(installed_path, destination / "intellij-bridge")
            self.assertFalse(replaced_existing)
            self.assertEqual(
                (installed_path / "plugin.xml").read_text(encoding="utf-8"),
                "<idea-plugin/>",
            )
            self.assertEqual(
                (installed_path / "lib" / "bridge.jar").read_bytes(), b"jar"
            )


if __name__ == "__main__":
    unittest.main()
