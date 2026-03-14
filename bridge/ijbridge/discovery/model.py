from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IntelliJInstall:
    app_path: str
    product_name: str
    product_code: str | None
    version: str
    build_number: str | None
    data_directory_name: str | None
    config_dir: str | None
    plugins_dir: str | None
    product_info_path: str | None
    source: str

    def to_dict(self) -> dict[str, str | None]:
        return {
            "appPath": self.app_path,
            "productName": self.product_name,
            "productCode": self.product_code,
            "version": self.version,
            "buildNumber": self.build_number,
            "dataDirectoryName": self.data_directory_name,
            "configDir": self.config_dir,
            "pluginsDir": self.plugins_dir,
            "productInfoPath": self.product_info_path,
            "source": self.source,
        }
