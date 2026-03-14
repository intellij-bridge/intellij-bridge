"""ijbridge package."""

from .config import (
    BridgeConfig,
    get_connection_file_path,
    get_daemon_socket_path,
    get_plugins_path,
    load_bridge_config,
)
from .version import PACKAGE_VERSION

__version__ = PACKAGE_VERSION

__all__ = [
    "BridgeConfig",
    "PACKAGE_VERSION",
    "__version__",
    "get_connection_file_path",
    "get_daemon_socket_path",
    "get_plugins_path",
    "load_bridge_config",
]
