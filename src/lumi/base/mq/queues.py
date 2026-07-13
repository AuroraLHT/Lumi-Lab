"""Queue lifecycle as an object.

This finishes the decomposition started (and abandoned) in
base/message_queue_refactoring.py: declare / bind / consume / cancel / delete is one
concern, and it belongs in one class rather than being copy-pasted three or four
times into every server and client.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aio_pika.abc import (
    AbstractChannel,
    AbstractExchange,
    AbstractIncomingMessage,
    AbstractQueue,
)

OnMessage = Callable[[AbstractIncomingMessage], Awaitable[None]]

log = logging.getLogger(__name__)


class BaseQueue:
    """A queue and its consumer, with a lifecycle you can actually stop."""

    def __init__(
        self,
        channel: AbstractChannel,
        on_message: OnMessage,
        *,
        name: str = "",
        exchange: AbstractExchange | None = None,
        routing_key: str | None = None,
        durable: bool = False,
        exclusive: bool = True,
        auto_delete: bool = True,
        no_ack: bool = True,
        arguments: dict[str, Any] | None = None,
    ) -> None:
        self.channel = channel
        self.on_message = on_message
        self.name = name
        self.exchange = exchange
        self.routing_key = routing_key
        self.durable = durable
        self.exclusive = exclusive
        self.auto_delete = auto_delete
        self.no_ack = no_ack
        self.arguments = arguments
        self.queue: AbstractQueue | None = None
        self._tag: str | None = None

    @property
    def is_running(self) -> bool:
        return self.queue is not None and self._tag is not None

    async def start(self) -> None:
        if self.is_running:
            return
        self.queue = await self.channel.declare_queue(
            self.name or None,
            durable=self.durable,
            exclusive=self.exclusive,
            auto_delete=self.auto_delete,
            arguments=self.arguments,
        )
        if self.exchange is not None and self.routing_key is not None:
            await self.queue.bind(self.exchange, routing_key=self.routing_key)
        self._tag = await self.queue.consume(self.on_message, no_ack=self.no_ack)
        log.debug("queue %s consuming (key=%s)", self.queue.name, self.routing_key)

    async def stop(self, *, delete: bool = False) -> None:
        """Cancel the consumer. Deleting is opt-in: a durable work queue must
        survive its consumer, or requests in flight are lost on every restart."""
        if self.queue is None:
            return
        if self._tag is not None:
            try:
                await self.queue.cancel(self._tag)
            except Exception:  # broker may already have reaped an exclusive queue
                log.debug("cancel failed for %s", self.queue.name, exc_info=True)
            self._tag = None
        if delete:
            try:
                await self.queue.delete(if_unused=False, if_empty=False)
            except Exception:
                log.debug("delete failed for %s", self.queue.name, exc_info=True)
        self.queue = None


class WorkQueue(BaseQueue):
    """Server-side request intake: named, durable, and *shared*.

    Two things follow from being non-exclusive. Good: two instances of a node
    compete for requests and each request is answered once -- the old code bound an
    exclusive queue per server to a well-known key, so a second instance meant every
    request was answered twice. Bad: the queue outlives its consumers, so requests
    pile up while a node is down and a rebooting node would otherwise be handed a
    twenty-minute-old "start recording". Hence the mandatory TTL.
    """

    def __init__(
        self,
        channel: AbstractChannel,
        on_message: OnMessage,
        *,
        name: str,
        exchange: AbstractExchange,
        routing_key: str,
        ttl_ms: int = 30_000,
        max_length: int = 1_000,
    ) -> None:
        super().__init__(
            channel,
            on_message,
            name=name,
            exchange=exchange,
            routing_key=routing_key,
            durable=True,
            exclusive=False,
            auto_delete=False,
            no_ack=False,  # a request must not be lost if the handler dies mid-flight
            arguments={
                "x-message-ttl": ttl_ms,
                "x-max-length": max_length,
                "x-overflow": "drop-head",
            },
        )


class ControlQueue(BaseQueue):
    """Per-instance control. Exclusive and bound to the shared control key, so a
    control message reaches *every* instance -- 'stop streaming' is addressed to all
    of them, unlike a request, which should be handled by exactly one."""

    def __init__(
        self,
        channel: AbstractChannel,
        on_message: OnMessage,
        *,
        exchange: AbstractExchange,
        routing_key: str,
    ) -> None:
        super().__init__(channel, on_message, exchange=exchange, routing_key=routing_key)


class ReplyQueue(BaseQueue):
    """Client-side RPC replies. Exclusive, unbound: the server replies through the
    default exchange straight to this queue's name."""

    def __init__(self, channel: AbstractChannel, on_message: OnMessage) -> None:
        super().__init__(channel, on_message)


class SubQueue(BaseQueue):
    """Client-side subscription to a stream / update / state key. Each subscriber
    declares its own exclusive queue on the shared key, so all of them get every
    message -- this is how N browsers each see the full camera feed."""

    def __init__(
        self,
        channel: AbstractChannel,
        on_message: OnMessage,
        *,
        exchange: AbstractExchange,
        routing_key: str,
    ) -> None:
        super().__init__(channel, on_message, exchange=exchange, routing_key=routing_key)
