"""The ONE fallback point of the perception package.

Nothing else in `perception/` may catch-and-continue. Anything that cannot be
done is routed through :func:`give_up`, which logs and raises :class:`Fallback`.
Only ``perception.run.run_perception`` catches it, and turns it into an
empty-but-valid ``PerceptionResult``.
"""
from __future__ import annotations

import logging
from typing import NoReturn

LOGGER = logging.getLogger("perception")


class Fallback(Exception):
    """Raised by :func:`give_up`. Caught only in run_perception."""

    def __init__(self, reason: str, request_id: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.request_id = request_id


def give_up(reason: str, request_id: str) -> NoReturn:
    """Log and raise. The only way perception is allowed to stop early."""
    LOGGER.warning("perception fallback [%s]: %s", request_id, reason)
    raise Fallback(reason, request_id)
