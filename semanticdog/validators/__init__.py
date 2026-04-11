"""Validator registry — maps file extensions to validator classes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Type

if TYPE_CHECKING:
    from .base import BaseValidator

_registry: dict[str, Type["BaseValidator"]] = {}


def register(validator_class: Type["BaseValidator"]) -> Type["BaseValidator"]:
    """Register a validator class for all its declared extensions."""
    for ext in validator_class.extensions:
        _registry[ext.lower()] = validator_class
    return validator_class


def get_validator(ext: str) -> Type["BaseValidator"] | None:
    """Return the validator class for the given extension, or None."""
    return _registry.get(ext.lower())


def all_extensions() -> frozenset[str]:
    """Return all registered extensions."""
    return frozenset(_registry.keys())


def all_validators() -> list[Type["BaseValidator"]]:
    """Return deduplicated list of registered validator classes."""
    return list({id(v): v for v in _registry.values()}.values())


def _load_all() -> None:
    """Import all validator modules to trigger @register decorators."""
    from . import images, raw, documents, media  # noqa: F401


_load_all()
