"""Abstract base validator and shared data types."""

from __future__ import annotations

import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar


VALID_STATUSES = frozenset({"ok", "corrupt", "unsupported", "unreadable", "error"})


@dataclass
class ValidationResult:
    """Result of validating a single file."""

    status: str  # 'ok' | 'corrupt' | 'unsupported' | 'unreadable' | 'error'
    error: str | None = None
    suggested_action: str | None = None

    def __post_init__(self) -> None:
        if self.status not in VALID_STATUSES:
            raise ValueError(f"Invalid status: {self.status!r}. Must be one of {VALID_STATUSES}")

    @property
    def ok(self) -> bool:
        """Derived from status — never out of sync."""
        return self.status == "ok"


@dataclass
class DependencyReport:
    """Availability report for a single external tool."""

    name: str
    available: bool
    version: str | None = None
    required: bool = False


def _cli_version(cmd: str) -> str | None:
    """Return version string for a CLI tool, or None if not found."""
    try:
        result = subprocess.run(
            [cmd, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return (result.stdout or result.stderr).strip().splitlines()[0]
    except Exception:
        return None


class BaseValidator(ABC):
    """Abstract base for all format validators."""

    extensions: ClassVar[frozenset[str]]
    requires_cli: ClassVar[list[str]] = []
    optional_cli: ClassVar[list[str]] = []
    memory_category: ClassVar[str] = "low"  # 'low' | 'medium' | 'high'

    @abstractmethod
    def validate(self, path: str) -> ValidationResult:
        """Validate a single file. Must not modify the file."""
        ...

    def check_dependencies(self) -> list[DependencyReport]:
        """Check availability of all CLI tools this validator uses."""
        reports: list[DependencyReport] = []
        for tool in self.requires_cli:
            available = shutil.which(tool) is not None
            reports.append(DependencyReport(
                name=tool,
                available=available,
                version=_cli_version(tool) if available else None,
                required=True,
            ))
        for tool in self.optional_cli:
            available = shutil.which(tool) is not None
            reports.append(DependencyReport(
                name=tool,
                available=available,
                version=_cli_version(tool) if available else None,
                required=False,
            ))
        return reports
