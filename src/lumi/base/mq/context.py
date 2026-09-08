"""Who asked for the op currently being handled.

The step journal wants an actor on every row, but a handler method's signature is
`async def <op>(self, req)` -- deliberately, so domain code never sees AMQP. That is
fine for the ops MqServer journals itself, where it still has the headers in hand. It
is not enough for the long-running ones: `to_temperature` returns a TaskAck and opens
its own journal row inside ExperimentHandler._start_task, several frames below the
dispatch that read the header, and a ramp with no actor is exactly the row someone
would later want to attribute.

A context variable rather than a parameter, because the alternative is threading
`actor` through every handler method and every manager call that might start a task,
to be used by one of them. Set by MqServer._handle_request around the handler call, so
it is correct for anything that runs inside it -- including a task the handler spawns,
which inherits the context at creation.
"""

from __future__ import annotations

from contextvars import ContextVar

#: Who is responsible: "hliang16", "mcp:agent", or None when the caller said nothing.
current_actor: ContextVar[str | None] = ContextVar("current_actor", default=None)

#: Which client instance sent it -- always present, since the client sets it itself.
current_source: ContextVar[str | None] = ContextVar("current_source", default=None)
