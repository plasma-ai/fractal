"""Base classes for events."""

from __future__ import annotations

import copy
import functools
import re
import time
import typing
from typing import Any, Optional

__all__ = [
    'Event',
    'InitialEvent',
    'TerminalEvent',
    'FailureEvent',
]


class Event:
    """Point-in-time observability record with snapshot semantics.

    Payload fields are bare class annotations on subclasses;
    ``__init__`` extracts matching kwargs via ``typing.get_type_hints``,
    deep-copies the values, and shallow-copies the emitting source.
    """

    info: Optional[str] = None

    def __init__(
        self: Event,
        source: Any,
        message: Optional[str | list[str]] = None,
        **kwargs: Any,
    ) -> None:
        """Initialize ``Event``.

        Args:
            source: The object that emitted this event.
            message: Optional caller-supplied free-form
                context to fold into the event description.

        """
        # extract annotated event attributes
        attrs = {}
        for name in _hints(self.__class__):
            if name in kwargs:
                attrs[name] = kwargs.pop(name)
        # reject leftovers -- a typo'd field would otherwise vanish silently
        if kwargs:
            raise TypeError(f'Unexpected event fields: {sorted(kwargs)}')
        # bind source copy
        self.source = copy.copy(source)
        # bind creation instant
        self.created = time.time()
        # bind message(s)
        self.message = message
        # bind attributes
        for k, v in attrs.items():
            v = copy.deepcopy(v)
            setattr(self, k, v)

    @property
    def name(self: Event) -> str:
        """Return event name.

        Default format is ``EVENT_NAME``, derived from
        the class name in screaming snake case. When
        ``info`` is set, returns ``EVENT_NAME (info)``
        to qualify the event type with subclass-specific
        structured context.

        Returns:
            Event name with optional ``(info)`` qualifier.

        """
        # derive SCREAMING_SNAKE from the class name (acronym-aware split)
        name = self.__class__.__name__
        parts = re.findall(r'[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+', name)
        result = '_'.join(parts).upper()
        if self.info:
            result += f' ({self.info})'
        return result

    @property
    def description(self: Event) -> str:
        """Return event description.

        Composes ``{name} : {source class name}`` plus optional
        message on new line when ``message`` is set -- the class name
        is the friendly label for every emitting source, never a raw
        object repr.

        Returns:
            Full event description for logging.

        """
        result = f'{self.name} : {type(self.source).__name__}'
        if message := self.message:
            if isinstance(message, list):
                if any('\n' in part for part in message):
                    message = '\n'.join(message)
                else:
                    message = ', '.join(message)
            result += f'\n{message}'
        return result


class InitialEvent(Event):
    """Emitted at the start of a paired operation."""


class TerminalEvent(Event):
    """Emitted at the end of a paired operation; computes duration."""

    initial_event: InitialEvent

    def __init__(self: TerminalEvent, source: Any, **kwargs: Any) -> None:
        """Initialize terminal event.

        Pairs with the corresponding ``initial_event`` and
        computes ``duration`` as the elapsed seconds between
        the two events.

        Args:
            source: The object that emitted this event.

        """
        super().__init__(source, **kwargs)
        self.duration = self.created - self.initial_event.created


class FailureEvent(TerminalEvent):
    """Emitted when a paired operation fails."""

    error: BaseException


# ------ helper functions


@functools.cache
def _hints(cls: type) -> tuple[str, ...]:
    """Return a class's annotated payload field names, resolved once.

    ``typing.get_type_hints`` evaluates stringified annotations on every
    call, and events are emitted per stream frame, so the resolution is
    memoized per class.
    """
    return tuple(typing.get_type_hints(cls))
