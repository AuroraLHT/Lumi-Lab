"""ExperimentSession: the notebook-facing "direct class" that drives an experiment.

    async with ExperimentSession.connect(host="localhost") as exp:
        await exp.driver.register_project(RegisterProject(project_name="demo"))
        await exp.driver.to_temperature(ToTemperature(temperature=650))
        img, meta = await exp.rheed.image()

`.driver` is the full generated `ExperimentDriverClient` -- every op on the
`experiment` contract, typed. `.rheed`/`.chamber_log` are held directly (there is no
passthrough op on the experiment contract for either; that would just duplicate the
RHEED/chamber contracts for no domain logic gained). This class exists at all
because it spans three exchanges (EXPERIMENT + RHEED + CHAMBER) and owns the AMQP
connection lifecycle -- no generated `<Node>Client` does either of those, the same
reason the old MQCommunication was hand-written, just built on the current generated
clients instead of the retired lumi.client.* hierarchy.
"""

from __future__ import annotations

from aio_pika import ExchangeType, connect_robust
from aio_pika.abc import AbstractChannel, AbstractConnection

from lumi.config import settings
from lumi.contracts.chamber import CHAMBER
from lumi.contracts.experiment import EXPERIMENT
from lumi.contracts.payloads.common import Ack
from lumi.contracts.payloads.experiment import (
    ConfirmCenterMask,
    ConfirmLaserPower,
    ConfirmMaskCenter,
    ConfirmProceed,
    CurrentTask,
    PendingConfirmation,
    ResolvePixelCheck,
    TaskEvent,
)
from lumi.contracts.rheed import RHEED
from lumi.generated.clients.chamber import ChamberLogClient
from lumi.generated.clients.experiment import ExperimentDriverClient
from lumi.generated.clients.rheed import RheedCameraClient


class ExperimentSession:
    def __init__(
        self,
        connection: AbstractConnection,
        channel: AbstractChannel,
        driver: ExperimentDriverClient,
        rheed: RheedCameraClient,
        chamber_log: ChamberLogClient,
    ) -> None:
        self._connection = connection
        self._channel = channel
        self.driver = driver
        self.rheed = rheed
        self.chamber_log = chamber_log
        self._pending: PendingConfirmation | None = None
        self._current_task: CurrentTask | None = None
        self._last_task_result: dict | None = None

    @classmethod
    async def open(
        cls, *, host: str | None = None, user: str = "guest", password: str = "guest", timeout: float = 120.0,
    ) -> "ExperimentSession":
        """Connect and return a ready session. Prefer `.connect()` as an `async
        with` block so the connection is always closed; use this directly only when
        you need the session to outlive one cell."""
        host = host or settings.rabbitmq.host
        connection = await connect_robust(f"amqp://{user}:{password}@{host}/")
        channel = await connection.channel()

        experiment_x = await channel.declare_exchange(
            EXPERIMENT.exchange, ExchangeType(EXPERIMENT.exchange_type), durable=True
        )
        rheed_x = await channel.declare_exchange(
            RHEED.exchange, ExchangeType(RHEED.exchange_type), durable=True
        )
        chamber_x = await channel.declare_exchange(
            CHAMBER.exchange, ExchangeType(CHAMBER.exchange_type), durable=True
        )

        driver = ExperimentDriverClient(channel, experiment_x, timeout=timeout)
        rheed = RheedCameraClient(channel, rheed_x, timeout=timeout)
        chamber_log = ChamberLogClient(channel, chamber_x, timeout=timeout)
        for client in (driver, rheed, chamber_log):
            await client.start()

        session = cls(connection, channel, driver, rheed, chamber_log)
        await driver.subscribe_updates(session._on_update)
        # Prime pending/current_task from whatever the node is already doing --
        # subscribing only catches *future* transitions, and a notebook that
        # reconnects mid-gate should see it immediately rather than after the next one.
        state = await driver.get_state()
        session._pending = state.pending_confirmation
        session._current_task = state.current_task
        return session

    @classmethod
    def connect(cls, **kwargs) -> "_SessionConnectCtx":
        return _SessionConnectCtx(kwargs)

    async def _on_update(self, event: TaskEvent, _payload) -> None:
        self._pending = event.pending_confirmation
        self._current_task = event.current_task
        # A long-running op reports its outcome exactly once, here. Dropping it left a
        # failed ramp indistinguishable from a successful one -- `current_task` clears
        # either way -- so a caller waiting on the task would carry on as if the
        # substrate had reached temperature.
        if event.task_result is not None:
            self._last_task_result = event.task_result

    @property
    def pending(self) -> PendingConfirmation | None:
        return self._pending

    @property
    def current_task(self) -> CurrentTask | None:
        return self._current_task

    @property
    def last_task_result(self) -> dict | None:
        """The most recent long-running op's outcome: `{"ok": True, ...}` or
        `{"ok": False, "error": "..."}`. None until one has finished."""
        return self._last_task_result

    async def confirm(self, **fields) -> Ack:
        """Resolve whatever is currently pending. Convenience only -- for a typed
        call, use exp.driver.confirm_laser_power(...) etc. directly."""
        if self._pending is None:
            raise RuntimeError("nothing is pending")
        kind = self._pending.kind
        if kind == "laser_power":
            result = await self.driver.confirm_laser_power(ConfirmLaserPower(measured_power=fields["measured_power"]))
            return Ack(ok=True) if result is not None else Ack(ok=False)
        if kind == "mask_center_alignment":
            return await self.driver.confirm_center_mask(ConfirmCenterMask(position=fields["position"]))
        if kind == "mask_center_check":
            status = await self.driver.confirm_mask_center(ConfirmMaskCenter(
                aligned=fields.get("aligned", False), corrected_position=fields.get("corrected_position"),
            ))
            return Ack(ok=status.pending is None)
        if kind == "rheed_gain":
            return await self.driver.confirm_rheed_gain()
        if kind == "pixel_check":
            status = await self.driver.resolve_pixel_check(ResolvePixelCheck(keep=fields.get("keep", True)))
            return Ack(ok=True)
        return await self.driver.confirm(ConfirmProceed(confirmation_id=self._pending.id))

    async def close(self) -> None:
        for client in (self.driver, self.rheed, self.chamber_log):
            await client.stop()
        if not self._channel.is_closed:
            await self._channel.close()
        if not self._connection.is_closed:
            await self._connection.close()

    async def __aenter__(self) -> "ExperimentSession":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


class _SessionConnectCtx:
    def __init__(self, kwargs: dict) -> None:
        self._kwargs = kwargs
        self._session: ExperimentSession | None = None

    async def __aenter__(self) -> ExperimentSession:
        self._session = await ExperimentSession.open(**self._kwargs)
        return self._session

    async def __aexit__(self, *exc: object) -> None:
        if self._session is not None:
            await self._session.close()
