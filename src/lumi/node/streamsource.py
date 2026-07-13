"""Bridging a worker thread's queue.Queue to a capability's stream.

Every streaming server in the old code wrote out the same eight lines -- pull from a
stdlib queue if it is non-empty, encode, wrap in a stream message, else return None:

    async def on_streaming(self):
        if not self.camera_queue.empty():
            item, headers = self.camera_queue.get()
            body, headers = encode_img(item, headers)
            return self.create_stream_message(body, headers, stream_type="live_camera")
        return None

Four near-identical copies (LiveCamera, LiveChamberLog, LiveIntegrator, LiveSTFT),
differing only in the queue attribute, the encoder, and a string. One copy now.
"""

from __future__ import annotations

import queue
from typing import Callable, Generic, TypeVar

from pydantic import BaseModel

from lumi.base.mq.codec import Payload

T = TypeVar("T", bound=BaseModel)

# item as the worker thread produced it -> what the contract says goes on the wire
Adapter = Callable[[object], "tuple[BaseModel, Payload] | BaseModel | None"]


class QueueStreamSource(Generic[T]):
    """Wraps a thread-fed queue.Queue as an `async def next()`.

    Non-blocking by design: `next()` returns None when the queue is empty and the
    server's stream loop idles briefly, rather than blocking the event loop on a
    thread-safe queue (which would stall every other capability on the node).
    """

    def __init__(self, q: "queue.Queue", adapt: Adapter) -> None:
        self.queue = q
        self.adapt = adapt

    async def next(self):
        try:
            item = self.queue.get_nowait()
        except queue.Empty:
            return None
        return self.adapt(item)

    def qsize(self) -> int:
        return self.queue.qsize()
