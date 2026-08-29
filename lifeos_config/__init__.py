"""Private configuration and capability reporting for LifeOS."""

from .core import ConfigError, LifeOSConfig, load_config, resolve_config_path

__all__ = ["ConfigError", "LifeOSConfig", "load_config", "resolve_config_path"]
