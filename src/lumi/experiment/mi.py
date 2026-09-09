"""Submit a Pascal command script and wait for it to finish.

Replaces the retired `lumi.client.pascal.MIModeClient.execute_command` that
`manager.py` was originally written against: MI_MODE is PUBSUB (see
`lumi.contracts.chamber.MI_MODE`) -- `register_commands` acknowledges immediately,
and the actual result arrives later on the update channel, correlated by
`commands_uuid`. Every hardware-control call in `lumi.experiment.manager` wants the
simpler "await until it's done" shape, so this turns that submit-then-listen pattern
into one coroutine.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from lumi.contracts.payloads.chamber import MICommands, MIExecution
from lumi.generated.clients.chamber import ChamberMiModeClient
from lumi.pascal.command import PascalCommand, PascalScope

log = logging.getLogger(__name__)


class MIExecutionFailed(Exception):
    """The chamber ran the script and it did not finish cleanly."""

    def __init__(self, execution: MIExecution) -> None:
        super().__init__(
            f"MI execution {execution.commands_uuid} aborted={execution.is_aborted} "
            f"stopped={execution.is_stopped}: {execution.commands!r}"
        )
        self.execution = execution


#: `execute(timeout=...)` not passed at all. Distinct from `None`, which is the
#: caller explicitly asking for an unbounded wait.
_DEFAULT = object()

#: How often to log that an unbounded wait is still waiting. Without this a script
#: that will never complete (see the lost-update modes in docs/TODO.md) is
#: indistinguishable from a healthy three-hour one in the node's log.
_STILL_WAITING_S = 300.0


def brief(text: str, max_length: int = 80) -> str:
    """A script is many lines; a log line is one. (Same idea as mi_mode.brief_commands,
    duplicated rather than imported: that module pulls in watchdog, which is the
    chamber host's dependency, not this node's.)"""
    flat = " ".join(text.split())
    return flat if len(flat) <= max_length else flat[:max_length] + "..."


class MiCommandRunner:
    """Wraps one ChamberMiModeClient with a request/response `execute()`.

    `timeout` is the default deadline for the *completion* wait -- the chamber's
    acknowledgement of the submission is a separate, always-bounded RPC. `None` means
    wait indefinitely, which is what a script whose duration cannot be estimated needs:
    a superlattice loop or a slow mask traverse finishes when it finishes, and there is
    no upper bound to pick that is both safe and generous enough.

    Be aware of what an unbounded wait costs, because nothing else here can rescue it:

    - The completion update is a single lossy message (exclusive, auto-delete, no_ack).
      Lose it -- broker reconnect, a full update queue, a chamber-node restart -- and
      the wait never ends.
    - Nothing can cancel it. `_start_task` keeps no task handle, there is no abort op,
      and the PASCAL firmware has no filesystem cancel, so `$stop` would only lie.
    - Ops that await inline in a request handler hold one of the node's 8 prefetch
      slots for the duration. Eight stuck ops and the node stops taking requests.

    So it is left bounded by default, and the caller opts in. See docs/TODO.md.
    """

    def __init__(
        self,
        client: ChamberMiModeClient,
        *,
        timeout: float | None = 30.0,
        raise_on_abort: bool = True,
    ) -> None:
        self.client = client
        self.timeout = timeout
        #: Whether an execution the chamber reports as `is_aborted` raises
        #: MIExecutionFailed. Default on. The PASCAL firmware writes the
        #: `Aborted_*` assist file for scripts that actually ran to completion
        #: (a known firmware bug), so a caller that verifies the physical result
        #: another way -- the chamber log -- can turn this off, runner-wide here
        #: or per call. `is_stopped` (a deliberate `$stop`) always raises.
        self.raise_on_abort = raise_on_abort
        self._pending: dict[str, asyncio.Future[MIExecution]] = {}
        self._subscribed = False

    async def start(self) -> None:
        if self._subscribed:
            return
        await self.client.subscribe_updates(self._on_update)
        self._subscribed = True

    async def _on_update(self, execution: MIExecution, _payload) -> None:
        fut = self._pending.get(execution.commands_uuid)
        if fut is not None and not fut.done() and execution.is_execution_finished:
            fut.set_result(execution)

    async def _wait(self, fut: asyncio.Future[MIExecution], timeout: float | None, text: str) -> MIExecution:
        """Await a completion, bounded or not. An unbounded wait still says so
        periodically -- silence for an hour should not look like health."""
        if timeout is not None:
            try:
                return await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError:
                raise TimeoutError(
                    f"MI command did not finish within {timeout}s: {text!r}"
                ) from None

        waited = 0.0
        while True:
            try:
                return await asyncio.wait_for(asyncio.shield(fut), _STILL_WAITING_S)
            except asyncio.TimeoutError:
                waited += _STILL_WAITING_S
                log.warning(
                    "still waiting on an unbounded MI execution after %.0f min: %s",
                    waited / 60.0, brief(text),
                )

    async def execute(
        self,
        commands: str | PascalCommand | PascalScope,
        *,
        timeout: float | None | object = _DEFAULT,
        raise_on_abort: bool | object = _DEFAULT,
    ) -> MIExecution:
        """Submit a command script and wait for it to finish.

        `timeout` defaults to this runner's; pass a number to override it for one call,
        or `None` to wait indefinitely (see the class docstring for what that gives up).
        `raise_on_abort` defaults to this runner's; pass `False` to accept an execution
        the chamber reports as aborted (PASCAL raises spurious aborts -- verify the
        physical result against the chamber log when you do this).
        Raises TimeoutError if the chamber does not report completion in time,
        MIExecutionFailed if it reports one that was stopped, or aborted while
        `raise_on_abort` is on.
        """
        deadline = self.timeout if timeout is _DEFAULT else timeout
        assert deadline is None or isinstance(deadline, (int, float))
        check_abort = self.raise_on_abort if raise_on_abort is _DEFAULT else bool(raise_on_abort)
        await self.start()

        text = commands.to_text() if hasattr(commands, "to_text") else str(commands)
        commands_uuid = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[MIExecution] = loop.create_future()
        self._pending[commands_uuid] = fut

        try:
            ack = await self.client.register_commands(
                MICommands(commands=text, commands_uuid=commands_uuid)
            )
            # A `$stop`/`$clean` special command executes immediately and registers
            # no execution -- nothing to wait for.
            if ack.state == "special" or ack.is_execution_finished:
                execution = ack
            else:
                execution = await self._wait(fut, deadline, text)
        finally:
            self._pending.pop(commands_uuid, None)

        if execution.is_stopped:
            raise MIExecutionFailed(execution)
        if execution.is_aborted:
            if check_abort:
                raise MIExecutionFailed(execution)
            log.warning(
                "MI execution %s reported aborted; continuing because raise_on_abort is "
                "off -- PASCAL reports spurious aborts for scripts that ran, so confirm "
                "the physical result against the chamber log: %s",
                execution.commands_uuid, brief(text),
            )
        return execution
