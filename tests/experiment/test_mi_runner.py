"""MiCommandRunner's completion wait, bounded and unbounded.

T0: no broker. The `ChamberMiModeClient` is stood in for by a fake that records the
submission and lets the test decide when -- or whether -- the completion update
arrives, which is the only thing these tests are about.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from lumi.contracts.payloads.chamber import MICommands, MIExecution
from lumi.experiment import mi as mi_module
from lumi.experiment.mi import MiCommandRunner, MIExecutionFailed

pytestmark = pytest.mark.asyncio


class FakeMiClient:
    """Acknowledges a submission the way the chamber node does -- queued, not finished
    -- and hands the test the callback the runner subscribed with."""

    def __init__(self, *, ack_state: str = "LOAD", ack_finished: bool = False) -> None:
        self.ack_state = ack_state
        self.ack_finished = ack_finished
        self.submitted: list[MICommands] = []
        self.on_update = None

    async def subscribe_updates(self, callback) -> None:
        self.on_update = callback

    async def register_commands(self, req: MICommands) -> MIExecution:
        self.submitted.append(req)
        return MIExecution(
            commands=req.commands, commands_uuid=req.commands_uuid,
            state=self.ack_state, is_execution_finished=self.ack_finished,
        )

    async def complete(self, *, aborted: bool = False, stopped: bool = False) -> None:
        """Deliver the completion update for the most recent submission."""
        req = self.submitted[-1]
        await self.on_update(
            MIExecution(
                commands=req.commands, commands_uuid=req.commands_uuid,
                state="ABORTED" if aborted else "COMPLETED",
                is_execution_finished=True, is_aborted=aborted, is_stopped=stopped,
            ),
            None,
        )


async def test_the_default_deadline_is_the_runners_own() -> None:
    runner = MiCommandRunner(FakeMiClient(), timeout=0.05)
    with pytest.raises(TimeoutError, match="within 0.05s"):
        await runner.execute("Trigger Laser N=3000 (0) F=10.0\n")


async def test_a_per_call_timeout_overrides_the_runners() -> None:
    runner = MiCommandRunner(FakeMiClient(), timeout=30.0)
    with pytest.raises(TimeoutError, match="within 0.05s"):
        await runner.execute("Sample Shutter ON\n", timeout=0.05)


async def test_timeout_none_waits_past_the_default_deadline() -> None:
    """The point of the option: a script far longer than any configured deadline still
    completes rather than raising."""
    client = FakeMiClient()
    runner = MiCommandRunner(client, timeout=0.01)

    task = asyncio.create_task(runner.execute("Trigger Laser N=200000 (0) F=10.0\n", timeout=None))
    # Well past the runner's own 0.01s deadline, and still waiting.
    await asyncio.sleep(0.1)
    assert not task.done()

    await client.complete()
    execution = await asyncio.wait_for(task, 1.0)
    assert execution.is_execution_finished


async def test_an_unbounded_runner_needs_no_per_call_argument() -> None:
    """`--mi-timeout 0` configures the node once; manager.py's call sites are untouched."""
    client = FakeMiClient()
    runner = MiCommandRunner(client, timeout=None)

    task = asyncio.create_task(runner.execute("Set Mask1 D=160.0\n"))
    await asyncio.sleep(0.05)
    assert not task.done()

    await client.complete()
    assert (await asyncio.wait_for(task, 1.0)).is_execution_finished


async def test_an_unbounded_wait_says_so_periodically(monkeypatch, caplog) -> None:
    """Silence for an hour must not look like health -- there is no other signal that
    an unbounded wait is still outstanding."""
    monkeypatch.setattr(mi_module, "_STILL_WAITING_S", 0.02)
    client = FakeMiClient()
    runner = MiCommandRunner(client, timeout=None)

    with caplog.at_level(logging.WARNING, logger="lumi.experiment.mi"):
        task = asyncio.create_task(runner.execute("Trigger Laser N=200000 (0) F=10.0\n"))
        await asyncio.sleep(0.1)
        await client.complete()
        await asyncio.wait_for(task, 1.0)

    waiting = [r for r in caplog.records if "still waiting" in r.message]
    assert waiting, "an unbounded wait should report itself"
    # The script is summarised, not dumped whole, and the elapsed time is reported.
    assert "Trigger Laser" in waiting[0].getMessage()
    assert "min" in waiting[0].getMessage()


async def test_an_unbounded_wait_is_still_cancellable() -> None:
    """Nothing in the node cancels these today (docs/TODO.md), but the wait must not
    swallow a cancellation when something eventually does -- and it must not leak the
    pending future either."""
    client = FakeMiClient()
    runner = MiCommandRunner(client, timeout=None)

    task = asyncio.create_task(runner.execute("Trigger Laser N=200000 (0) F=10.0\n"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runner._pending == {}


async def test_an_unbounded_wait_still_fails_on_an_aborted_execution() -> None:
    """No timeout is not no error: an abort reported by the chamber must still raise."""
    client = FakeMiClient()
    runner = MiCommandRunner(client, timeout=None)

    task = asyncio.create_task(runner.execute("Select Target TG=3\n"))
    await asyncio.sleep(0.01)
    await client.complete(aborted=True)

    with pytest.raises(MIExecutionFailed):
        await asyncio.wait_for(task, 1.0)


async def test_raise_on_abort_false_returns_the_aborted_execution(caplog) -> None:
    """PASCAL reports spurious aborts; a caller that verifies the result another way can
    opt out per call and get the execution back instead of an exception."""
    client = FakeMiClient()
    runner = MiCommandRunner(client, timeout=None)

    task = asyncio.create_task(runner.execute("Temperature Ramp 20.0\n", raise_on_abort=False))
    await asyncio.sleep(0.01)
    with caplog.at_level(logging.WARNING, logger="lumi.experiment.mi"):
        await client.complete(aborted=True)
        execution = await asyncio.wait_for(task, 1.0)

    assert execution.is_aborted
    assert any("reported aborted" in r.getMessage() for r in caplog.records)


async def test_raise_on_abort_can_be_off_runner_wide() -> None:
    client = FakeMiClient()
    runner = MiCommandRunner(client, timeout=None, raise_on_abort=False)

    task = asyncio.create_task(runner.execute("Temperature Ramp 20.0\n"))
    await asyncio.sleep(0.01)
    await client.complete(aborted=True)
    assert (await asyncio.wait_for(task, 1.0)).is_aborted
    assert runner._pending == {}


async def test_a_stopped_execution_still_raises_with_raise_on_abort_off() -> None:
    """`raise_on_abort=False` covers spurious aborts only -- a deliberate `$stop` is a
    real cancellation and must still fail the op."""
    client = FakeMiClient()
    runner = MiCommandRunner(client, timeout=None, raise_on_abort=False)

    task = asyncio.create_task(runner.execute("Trigger Laser N=3000 (0) F=10.0\n"))
    await asyncio.sleep(0.01)
    await client.complete(stopped=True)
    with pytest.raises(MIExecutionFailed):
        await asyncio.wait_for(task, 1.0)


async def test_a_special_command_never_waits() -> None:
    """`$stop`/`$clean` register no execution, so there is nothing to wait for -- with
    or without a deadline."""
    client = FakeMiClient(ack_state="special")
    runner = MiCommandRunner(client, timeout=None)
    execution = await asyncio.wait_for(runner.execute("$stop"), 1.0)
    assert execution.state == "special"


async def test_the_pending_future_is_dropped_after_every_outcome() -> None:
    client = FakeMiClient()
    runner = MiCommandRunner(client, timeout=0.02)

    with pytest.raises(TimeoutError):
        await runner.execute("Sample Shutter ON\n")
    assert runner._pending == {}

    task = asyncio.create_task(runner.execute("Sample Shutter OFF\n", timeout=None))
    await asyncio.sleep(0.01)
    await client.complete()
    await asyncio.wait_for(task, 1.0)
    assert runner._pending == {}
