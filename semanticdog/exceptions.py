"""Base exceptions for SemanticDog."""


class SdogError(Exception):
    """Base exception for all SemanticDog errors."""


class ConfigError(SdogError):
    """Invalid or missing configuration."""


class DatabaseError(SdogError):
    """SQLite state DB error."""


class ScanError(SdogError):
    """Error during a scan operation."""


class DependencyError(SdogError):
    """Required dependency missing or incompatible."""


class LockError(SdogError):
    """Another sdog instance is already running."""
