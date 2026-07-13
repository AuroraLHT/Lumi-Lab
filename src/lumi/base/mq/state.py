"""State: broadcast on change, and pollable on demand.

Ported from the old ServerState/StateCallbackMixin, which was the best idea in the
previous message_queue.py -- mutate the state and subscribers are told, with no
explicit publish call at any site.

Two changes. State is now a typed pydantic model per capability rather than a
free-form dict, so consumers stop reading it by string (`camera_state["frame_dims"]`,
a KeyError waiting to happen mid-experiment). And the change notification now goes
through `call_soon_threadsafe`: the old code called `asyncio.create_task` directly,
which works only on the event loop thread, and the new handlers sit much closer to
the camera and compressor threads than the old servers did.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from pydantic import BaseModel

log = logging.getLogger(__name__)

Publish = Callable[[BaseModel], Awaitable[None]]


class StatePublisher:
    """Holds a capability's state and publishes it whenever it actually changes."""

    def __init__(self, initial: BaseModel, publish: Publish) -> None:
        self._state = initial
        self._publish = publish
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    @property
    def state(self) -> BaseModel:
        return self._state

    def set(self, new: BaseModel) -> None:
        """Replace the state. Publishes only on a real change -- a camera thread
        writing an unchanged state 30 times a second must not become 30 messages."""
        if new == self._state:
            return
        self._state = new
        self._schedule()

    def update(self, **fields: object) -> None:
        self.set(self._state.model_copy(update=fields))

    def _schedule(self) -> None:
        if self._loop is None:
            log.debug("state changed before the loop was bound; not publishing")
            return
        snapshot = self._state
        try:
            # Safe from any thread. The old code called create_task() directly,
            # which raises "no running event loop" off the loop thread.
            self._loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(self._publish_safely(snapshot))
            )
        except RuntimeError:
            log.debug("event loop is gone; dropping state publish")

    async def _publish_safely(self, snapshot: BaseModel) -> None:
        try:
            await self._publish(snapshot)
        except Exception:
            log.exception("failed to publish state")
