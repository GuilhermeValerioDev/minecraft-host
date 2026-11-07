"""Shared exception types."""


class MchostError(Exception):
    """An error that should be shown to the user without a traceback."""


class ConfigError(MchostError):
    """An instance configuration is missing or invalid."""


class EngineError(MchostError):
    """The container engine or compose provider could not be used."""
