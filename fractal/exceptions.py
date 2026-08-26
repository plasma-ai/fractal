"""Exceptions for ``fractal``."""

from __future__ import annotations

from typing import Union

__all__ = [
    'AbstractMethodError',
    'AgentStreamError',
    'DirtyWorktreeError',
]


class AbstractMethodError(NotImplementedError):
    """Subclass of ``NotImplementedError`` for abstract methods.

    To be used when not using the ``abc`` module.
    (adapted from ``pandas.errors``).
    """

    def __init__(
        self: AbstractMethodError,
        class_or_instance: Union[type, object],
        /,
        methodtype: str = 'method',
    ) -> None:
        """Initialize ``AbstractMethodError``, validating ``methodtype``."""
        types = {'method', 'classmethod', 'staticmethod', 'property'}
        if methodtype not in types:
            raise ValueError(
                f'Method type must be one of {types}, got {methodtype} instead.'
            )
        self.methodtype = methodtype
        self.class_or_instance = class_or_instance

    def __str__(self: AbstractMethodError) -> str:
        """String representation of error."""
        if self.methodtype == 'classmethod':
            name = self.class_or_instance.__name__
        else:
            name = type(self.class_or_instance).__name__
        return f'This {self.methodtype} must be defined in the concrete class {name}.'


class AgentStreamError(RuntimeError):
    """Raised when an agent reports a failure on its own JSON stream.

    Distinct from a fractal-side stream-consumer failure: the agent drained
    cleanly (exit 0) but its events named an error, so the loop books the
    step as an agent error, not a ``stream error``.
    """


class DirtyWorktreeError(RuntimeError):
    """Raised by a ``--check`` commit when uncommitted changes remain.

    Distinct from every other commit failure: a dirty tree is the check's
    own nonzero outcome (the CLI's reserved exit 1), so only this type
    converts there while the rest surface as ``Error:`` exit 2.
    """


class _Abort(Exception):
    """Raised by the loop when a preflight or adoption abort ends the run (exit 1)."""
