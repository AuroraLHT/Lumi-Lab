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


class MiCommandRunner:
    """Wraps one ChamberMiModeClient with a request/response `execute()`."""

    def __init__(self, client: ChamberMiModeClient) -> None:
        self.client = client
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

    async def execute(self, commands: str | PascalCommand | PascalScope, *, timeout: float = 30.0) -> MIExecution:
        """Submit a command script and wait for it to finish. Raises TimeoutError if
        the chamber does not report completion in time, MIExecutionFailed if it
        reports one that was aborted or stopped."""
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
                try:
                    execution = await asyncio.wait_for(fut, timeout)
                except asyncio.TimeoutError:
                    raise TimeoutError(
                        f"MI command did not finish within {timeout}s: {text!r}"
                    ) from None
        finally:
            self._pending.pop(commands_uuid, None)

        if execution.is_aborted or execution.is_stopped:
            raise MIExecutionFailed(execution)
        return execution
