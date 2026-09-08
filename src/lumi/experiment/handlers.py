"""ExperimentHandler: the wire adapter between the `experiment` contract's ops and
`lumi.experiment.manager`'s ported driving logic.

Follows storage/handlers.py's pattern: constructed with empty `sources`, wired up by
nodes/experiment.py once the node is connected (the handler needs clients, and the
clients need the node's channel). `sources` holds the generated clients this node
depends on (chamber's mi_mode/log/config, rheed's camera, storage) -- experiment is,
like storage, a *consumer* with no equipment of its own, so `deps_available` in its
state says which of those are actually up rather than failing opaquely mid-growth.

Every op method here is thin: unpack the request, call the matching manager.py
method, translate the result. The two mechanisms manager.py can't provide by itself
live here instead, because they are shared machinery across every op that needs
them, not particular to any one of them:

- Long-running ops (to_temperature, cool_down, perform_preablation,
  perform_deposition, anneal) are fired as a background task and tracked via
  `state.current_task` -- a handler method is one inline `await` on the server side
  (see CapabilityServer._handle_request), so nothing that can take minutes may sit
  inside an op and still expect the caller's RPC timeout to hold.
- Gated ops track `state.pending_confirmation`, with the `id` existing so a stale
  confirm can't resolve a gate that has since moved on.

Both are pushed on the `driver` capability's update channel via `next_update()`, so a
connected client does not have to poll state in a loop to notice a transition.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

from lumi.base.mq.context import current_actor, current_source
from lumi.contracts.payloads.camera import CameraConfig
from lumi.contracts.payloads.chamber import LogEntry
from lumi.contracts.payloads.common import Ack, Empty
from lumi.contracts.payloads.experiment import (
    Anneal,
    BeginSetLaserPower,
    ConfirmCenterMask,
    ConfirmLaserPower,
    ConfirmMaskCenter,
    ConfirmProceed,
    CoolDown,
    CurrentSubstrateResponse,
    CurrentTask,
    EndStorage,
    ExperimentReadout,
    ExperimentRecordId,
    FinishCurrentPixel,
    FinishExperimentRecord,
    LaserPowerResult,
    MaskPosition,
    MfcQuery,
    MfcStatus,
    MotorFree,
    MoveTo,
    PendingConfirmation,
    PendingStatus,
    PerformDeposition,
    PerformPreablation,
    PixelCheckStatus,
    PixelIndex,
    PixelMoveResult,
    PixelPosition,
    PressureReading,
    ProjectInfo,
    PumpStatus,
    RegisterProject,
    RegisterSubstrate,
    AddMeasurement,
    LayerInfo,
    ListMeasurements,
    ListSamples,
    ListSteps,
    MeasurementId,
    MeasurementInfo,
    MeasurementList,
    ResolvePixelCheck,
    ResumeSubstrate,
    SampleDetail,
    SampleId,
    SampleInfo,
    SampleList,
    StepInfo,
    StepList,
    SetMfcControl,
    SetMfcFlow,
    SetPressure,
    SetPressureControl,
    SetRheedGain,
    SetTarget,
    StartStorage,
    StorageResult,
    SubstrateInfo,
    TargetId,
    TargetMap,
    TargetName,
    TaskAck,
    TaskEvent,
    TemperatureReading,
    ToTemperature,
    ValveStatus,
)
from lumi.experiment.db import GrowthDB
from lumi.experiment.journal import StepJournal
from lumi.experiment.manager import ExperimentBounds, PLDChamberConfiguration, PixelExperimentManager, Substrate

log = logging.getLogger(__name__)

#: What this node needs on the bus before it can drive anything. Same idiom as
#: storage's DEPENDENCIES -- "the monitor did not answer" is deliberately not the
#: same as "the source is down" (see refresh_deps()).
DEPENDENCY_EQUIPMENT = ("chamber", "rheed", "storage")


def _loads(raw) -> dict:
    """A step's params/result and a measurement's detail are stored as JSON text.
    A row written before a column existed, or by an older version, reads back as
    NULL or as something that is not an object -- neither is worth failing a
    listing over."""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {"value": value}


def _jsonable(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, (tuple, list)):
        return {"value": list(value)}
    return {"value": value}


class ExperimentHandler:
    def __init__(
        self,
        sources: dict,
        growth_db: GrowthDB,
        pld_config: PLDChamberConfiguration,
        bounds: ExperimentBounds,
        target_mapper: dict[str, str],
        registry_client=None,
    ) -> None:
        self.sources = sources
        self.growth_db = growth_db
        self.pld_config = pld_config
        self.bounds = bounds
        self.target_mapper = target_mapper
        self.registry = registry_client

        # Built once sources are filled in by nodes/experiment.py (mirrors
        # StorageHandler.sources being set post-connect). PixelExperimentManager's
        # primitives are a superset of SingleDepoExperimentManager's -- a
        # single-position substrate (pixel_spacing=None) drives identically through
        # to_pixel(0)/to_current_pixel, so one manager instance covers both; the
        # "single vs pixel" distinction that mattered was perform_experiment's
        # bookkeeping, which is now client-side (lumi.experiment.recipes), not here.
        self.manager: PixelExperimentManager | None = None

        self._deps: dict[str, bool] = {name: False for name in DEPENDENCY_EQUIPMENT}
        self._pending: PendingConfirmation | None = None
        self._current_task: CurrentTask | None = None
        self._updates: asyncio.Queue[TaskEvent] = asyncio.Queue()

        # Set by nodes/experiment.py once the growth database is open. None in tests
        # and anywhere the journal is not wanted; every call site tolerates that.
        self.journal: StepJournal | None = None

    #: Ops this handler journals itself, so MqServer's dispatch leaves them alone.
    #: These return a TaskAck immediately and finish minutes or hours later --
    #: `_start_task`'s runner is the only place that knows when they really ended.
    journals_own = frozenset({
        "to_temperature", "cool_down", "perform_preablation", "perform_deposition", "anneal",
    })

    async def current_sample_id(self) -> int | None:
        """The sample the chamber is working on, for StepJournal.sample_resolver.

        Queried rather than cached: register/resume/finish_pixel all move the current
        position, and a cache would be one more thing to keep in step with the
        substrate. None whenever nothing is loaded, which is a normal state --
        alignment, a gas change and a warm-up are real steps that belong to the
        session and to no specimen.
        """
        m = self.manager
        substrate = m.current_substrate if m else None
        if substrate is None or substrate.db_id is None:
            return None
        row = await self.growth_db.find_sample(substrate.db_id, substrate.current_position_id)
        return row[0] if row else None

    def build_manager(self) -> None:
        """Called once `sources` is filled in."""
        self.manager = PixelExperimentManager(
            chamber_mi=self.sources["chamber_mi"],
            chamber_log=self.sources["chamber_log"],
            chamber_config=self.sources["chamber_config"],
            rheed_camera=self.sources["rheed_camera"],
            storage=self.sources["storage"],
            growth_db=self.growth_db,
            pld_config=self.pld_config,
            bounds=self.bounds,
            target_mapper=self.target_mapper,
        )

    # --- state / updates -----------------------------------------------------

    def readout(self) -> ExperimentReadout:
        m = self.manager
        substrate = m.current_substrate if m else None
        mode = "idle"
        if substrate is not None:
            mode = "pixel" if len(substrate.positions) > 1 else "single"
        return ExperimentReadout(
            mode=mode,
            project_id=m.project_id if m else None,
            project_name=m.project_name if m else None,
            substrate_id=substrate.db_id if substrate else None,
            current_pixel_index=substrate.current_position_id if substrate else None,
            num_pixel_positions=len(substrate.positions) if substrate else None,
            is_recording=False,
            pending_confirmation=self._pending,
            current_task=self._current_task,
            deps_available=dict(self._deps),
            laser_power_set=m._laser_power_set if m else None,
            laser_power_real=m._laser_power_real if m else None,
        )

    async def next_update(self) -> TaskEvent | None:
        try:
            return self._updates.get_nowait()
        except asyncio.QueueEmpty:
            return None

    async def _push(self, **fields) -> None:
        await self._updates.put(TaskEvent(**fields))

    # --- dependency liveness ---------------------------------------------------

    async def refresh_deps(self) -> None:
        """Driven by a loop in nodes/experiment.py, same idiom as storage's."""
        up = await self._equipment_up()
        if up is None:
            return  # monitor unreachable; keep the last known picture rather than lying
        self._deps = {name: name in up for name in self._deps}

    async def _equipment_up(self) -> set[str] | None:
        if self.registry is None:
            return None
        try:
            listing = await self.registry.list_nodes()
        except Exception as exc:
            log.warning("registry unreachable: %s", exc, exc_info=True)
            return None
        return {n.equipment for n in listing.nodes if n.status == "up"}

    # --- long-running ops: fire-and-return, tracked via current_task -------------

    async def _start_task(self, kind: str, coro, detail: dict | None = None) -> TaskAck:
        task_id = uuid.uuid4().hex
        started_at = time.time()
        self._current_task = CurrentTask(id=task_id, kind=kind, started_at=started_at, detail=detail or {})
        await self._push(current_task=self._current_task)

        # The journal row is opened here and closed by the runner below, so its
        # duration is the ramp's, not the ack's. `detail` is already the typed
        # request's fields and `task_result` is already the outcome -- the journal
        # stores what this method was computing and discarding anyway.
        step_id = None
        if self.journal is not None:
            step_id = await self.journal.begin(
                kind, params=detail or {}, step_uuid=task_id, started_at=started_at,
                actor=current_actor.get(),
                source=current_source.get() or "experiment.driver",
            )

        async def runner() -> None:
            try:
                result = await coro
                task_result = {"ok": True, **_jsonable(result)}
            except Exception as exc:
                log.exception("experiment task %s (%s) failed", kind, task_id)
                task_result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            self._current_task = None
            if self.journal is not None:
                await self.journal.end(
                    step_id, ok=bool(task_result.get("ok")),
                    result=task_result, error=task_result.get("error"),
                )
            await self._push(current_task=None, task_result=task_result)

        asyncio.create_task(runner(), name=f"experiment-task-{kind}")
        return TaskAck(task_id=task_id)

    async def _require_heating_laser(self) -> None:
        """The same refusal `manager.to_temperature` makes, hoisted ahead of
        `_start_task`.

        A long-running op reports failure on the update channel, not to its caller --
        `_start_task` returns a TaskAck the moment it spawns the runner. That is fine
        for a client subscribed to `driver.update`, but the MCP server exposes ops and
        nothing else: an agent has no way to read `current_task`, so a refusal raised
        inside the task is invisible to it and the ramp looks like it started. Checked
        here, it comes back as an error on the tool call itself.
        """
        if not await self.manager.is_heating_laser_on():
            raise RuntimeError(
                "heating laser is off -- call initiate_heating_laser first; ramping "
                "with it off sets a setpoint no current can reach"
            )

    async def to_temperature(self, req: ToTemperature) -> TaskAck:
        await self._require_heating_laser()
        return await self._start_task(
            "to_temperature", self.manager.to_temperature(req.temperature, req.ramp_rate),
            {"temperature": req.temperature, "ramp_rate": req.ramp_rate},
        )

    async def cool_down(self, req: CoolDown) -> TaskAck:
        return await self._start_task("cool_down", self.manager.cool_down(req.ramp_rate), {"ramp_rate": req.ramp_rate})

    async def _chamber_conditions(self) -> dict:
        """The chamber state a deposition is actually running at.

        Temperature and pressure are not fields of PerformDeposition -- they are set
        by earlier steps and by hand at the gauge -- so a deposition step that records
        only its request does not say what conditions the film was grown at. That is
        precisely what a GP regresses on, and relying on `finish_experiment_record` to
        supply it later means a growth that was never finalised (a dryrun, an aborted
        run) contributes nothing. Read here so the step stands on its own.
        """
        conditions: dict = {}
        try:
            conditions["temperature"] = (await self.manager.get_current_temperature())
            conditions["pressure"] = (await self.manager.get_current_pressure())[0]
        except Exception:
            log.warning("could not read chamber conditions for the journal", exc_info=True)
        return conditions

    async def _target_material(self, target_id: str) -> str | None:
        """The material in a carousel slot, resolved now rather than at read time.

        The slot-to-material map is chamber config, and it changes whenever targets
        are swapped. A step that recorded only `target_id: "C"` is therefore not a
        durable record of what was deposited -- read back after the next target
        change it names a different material. Resolving here freezes the answer into
        the journal row.
        """
        try:
            return await self.manager.get_target_name_by_id(target_id)
        except Exception:
            log.warning("could not resolve target %r to a material for the journal", target_id)
            return None

    async def perform_preablation(self, req: PerformPreablation) -> TaskAck:
        return await self._start_task(
            "perform_preablation",
            self.manager.perform_preablation(
                req.target_id, req.num_pulse, req.frequency, req.is_dryrun, req.move_mask_to_block_position,
            ),
            {
                "target_id": req.target_id, "num_pulse": req.num_pulse,
                "target_material": await self._target_material(req.target_id),
                "frequency": req.frequency, "is_dryrun": req.is_dryrun,
            },
        )

    async def perform_deposition(self, req: PerformDeposition) -> TaskAck:
        return await self._start_task(
            "perform_deposition",
            self.manager.perform_deposition(req.num_pulse, req.laser_repetition_rate, req.target_id, req.is_dryrun),
            {
                "target_id": req.target_id, "num_pulse": req.num_pulse,
                "target_material": await self._target_material(req.target_id),
                "laser_repetition_rate": req.laser_repetition_rate,
                # A dryrun fires no laser, so the step happened but no film was
                # deposited. get_layer_stack still surfaces it (a rehearsal should not
                # vanish from the record) but tags it with this, so a caller can tell
                # a real layer from a rehearsed one.
                "is_dryrun": req.is_dryrun,
                **await self._chamber_conditions(),
            },
        )

    async def anneal(self, req: Anneal) -> TaskAck:
        await self._require_heating_laser()  # every step is a to_temperature
        steps = [(s.temperature, s.ramp_rate, s.wait_time) for s in req.steps]
        return await self._start_task("anneal", self.manager.anneal(steps), {"n_steps": len(steps)})

    # --- pending-confirmation gates -----------------------------------------------

    async def _set_pending(self, kind: str, message: str) -> None:
        self._pending = PendingConfirmation(id=uuid.uuid4().hex, kind=kind, message=message, requested_at=time.time())
        await self._push(pending_confirmation=self._pending)

    async def _clear_pending(self) -> None:
        self._pending = None
        await self._push(pending_confirmation=None)

    def _require_pending(self, kind: str) -> None:
        if self._pending is None or self._pending.kind != kind:
            raise ValueError(f"no pending {kind!r} confirmation to resolve")

    # --- bookkeeping -----------------------------------------------------------

    async def register_project(self, req: RegisterProject) -> ProjectInfo:
        project_id = await self.manager.register_project(req.project_name, req.description)
        return ProjectInfo(project_id=project_id, project_name=req.project_name)

    def _substrate_info(self, substrate: Substrate) -> SubstrateInfo:
        positions = []
        for i, p in enumerate(substrate.positions):
            positions.append(PixelPosition(
                index=i, position=p,
                mask_position=self.manager._pixel_to_mask_position(p),
                rheed_position=self.manager._pixel_to_rheed_position(p),
                accessible=p in substrate.accessible_positions,
            ))
        return SubstrateInfo(
            substrate_id=substrate.db_id or 0, substrate_uuid=substrate.uuid,
            materials=substrate.materials, orientation=substrate.orientation,
            width=substrate.width, height=substrate.height or 0.0, thickness=substrate.thickness or 0.0,
            pixel_spacing=substrate.pixel_spacing, positions=positions,
            current_pixel_index=substrate.current_position_id,
        )

    async def register_substrate(self, req: RegisterSubstrate) -> SubstrateInfo:
        substrate = await self.manager.register_substrate(
            materials=req.materials, orientation=req.orientation, width=req.width,
            height=req.height, thickness=req.thickness, pixel_spacing=req.pixel_spacing,
            positions=req.positions or None, manufacturer=req.manufacturer,
            manufacture_date=req.manufacture_date, substrate_name=req.substrate_name,
        )
        return self._substrate_info(substrate)

    async def resume_substrate(self, req: ResumeSubstrate) -> SubstrateInfo:
        substrate = await self.manager.resume_substrate(req.substrate_id)
        return self._substrate_info(substrate)

    async def show_available_targets(self, req: Empty) -> TargetMap:
        return TargetMap(targets=await self.manager.show_available_targets())

    async def get_target_name_by_id(self, req: TargetId) -> TargetName:
        return TargetName(target_name=await self.manager.get_target_name_by_id(req.target_id))

    async def get_target_id_by_name(self, req: TargetName) -> TargetId:
        return TargetId(target_id=await self.manager.get_target_id_by_name(req.target_name))

    async def current_substrate(self, req: Empty) -> CurrentSubstrateResponse:
        substrate = self.manager.current_substrate
        return CurrentSubstrateResponse(substrate=self._substrate_info(substrate) if substrate else None)

    async def finish_substrate(self, req: Empty) -> Ack:
        await self.manager.finish_substrate()
        return Ack()

    async def finish_current_pixel(self, req: FinishCurrentPixel) -> Ack:
        await self.manager.finish_current_pixel()
        return Ack()

    # --- chamber reads ---------------------------------------------------------

    async def get_current_log(self, req: Empty) -> LogEntry:
        return await self.manager.get_current_log()

    async def get_current_pressure(self, req: Empty) -> PressureReading:
        pressure, gauge = await self.manager.get_current_pressure()
        return PressureReading(pressure=pressure, gauge=gauge)

    async def get_current_temperature(self, req: Empty) -> TemperatureReading:
        return TemperatureReading(temperature=await self.manager.get_current_temperature())

    async def get_valve_status(self, req: Empty) -> ValveStatus:
        return ValveStatus(valves=await self.manager.get_valve_status())

    async def get_mfc_status(self, req: MfcQuery) -> MfcStatus:
        set_flow, monitor = await self.manager.get_mfc_status(req.mfc_id)
        return MfcStatus(mfc_id=req.mfc_id, set=set_flow, monitor=monitor)

    async def get_pump_status(self, req: Empty) -> PumpStatus:
        return PumpStatus(pumps=await self.manager.get_pump_status())

    async def is_motor_free(self, req: Empty) -> MotorFree:
        return MotorFree(free=await self.manager.is_motor_free())

    async def get_current_mask_position(self, req: Empty) -> MaskPosition:
        return MaskPosition(position=await self.manager.get_current_mask_position())

    # --- fast hardware control ---------------------------------------------------

    async def set_target(self, req: SetTarget) -> Ack:
        await self.manager.set_target(req.target_id, req.rotation_mode, req.twist_mode)
        return Ack()

    async def move_mask_to_position(self, req: MoveTo) -> Ack:
        await self.manager.move_mask_to_position(req.position)
        return Ack()

    async def move_rheed_to_position(self, req: MoveTo) -> Ack:
        await self.manager.move_rheed_to_position(req.position)
        return Ack()

    async def to_pixel(self, req: PixelIndex) -> PixelMoveResult:
        result = await self.manager.to_pixel(req.index)
        if result is None:
            raise ValueError(f"pixel index {req.index} is out of range")
        mask_position, rheed_position = result
        return PixelMoveResult(index=req.index, mask_position=mask_position, rheed_position=rheed_position)

    async def to_current_pixel(self, req: Empty) -> PixelMoveResult:
        result = await self.manager.to_current_pixel()
        if result is None:
            raise ValueError("no current pixel to move to (no substrate registered, or positions exhausted)")
        mask_position, rheed_position = result
        return PixelMoveResult(
            index=self.manager.current_substrate.current_position_id,
            mask_position=mask_position, rheed_position=rheed_position,
        )

    async def set_mfc_flow(self, req: SetMfcFlow) -> Ack:
        await self.manager.set_mfc_flow(req.mfc_id, req.flow)
        return Ack()

    async def set_mfc_control(self, req: SetMfcControl) -> Ack:
        await self.manager.set_mfc_control(req.enabled)
        return Ack()

    async def set_pressure(self, req: SetPressure) -> Ack:
        await self.manager.set_pressure(req.pressure)
        return Ack()

    async def set_pressure_control(self, req: SetPressureControl) -> Ack:
        await self.manager.set_pressure_control(req.on)
        return Ack()

    async def initiate_heating_laser(self, req: Empty) -> Ack:
        await self.manager.initiate_heating_laser()
        return Ack()

    async def turn_off_heating_laser(self, req: Empty) -> Ack:
        await self.manager.turn_off_heating_laser()
        return Ack()

    async def start_storage(self, req: StartStorage) -> StorageResult:
        result = await self.manager.start_storage(req.project_name, req.is_dryrun)
        return StorageResult(**result)

    async def end_storage(self, req: EndStorage) -> StorageResult:
        result = await self.manager.end_storage(req.is_dryrun)
        return StorageResult(ok=result["ok"], message=result["message"])

    async def finish_experiment_record(self, req: FinishExperimentRecord) -> ExperimentRecordId:
        experiment_id = await self.manager.finish_experiment_record(
            substrate_id=req.substrate_id, project_id=req.project_id, experiment_uuid=req.experiment_uuid,
            temperature=req.temperature, pressure=req.pressure, laser_power=req.laser_power,
            laser_repetition_rate=req.laser_repetition_rate, target_material=req.target_material,
            num_pulse=req.num_pulse, is_pixel=req.is_pixel, pixel_index=req.pixel_index,
            storage_name=req.storage_name, record_uuid=req.record_uuid,
        )
        return ExperimentRecordId(experiment_id=experiment_id)

    # --- sample tracking -----------------------------------------------------------

    @staticmethod
    def _sample_info(row) -> SampleInfo:
        # sample_id, sample_uuid, parent_sample_id, substrate_id, kind, pixel_index,
        # position_mm, sample_name, state, notes, created_at
        return SampleInfo(
            sample_id=row[0], sample_uuid=row[1] or "", parent_sample_id=row[2],
            substrate_id=row[3], kind=row[4], pixel_index=row[5], position_mm=row[6],
            sample_name=row[7], state=row[8],
        )

    async def list_samples(self, req: ListSamples) -> SampleList:
        substrate_id = req.substrate_id
        if substrate_id is None:
            substrate = self.manager.current_substrate if self.manager else None
            substrate_id = substrate.db_id if substrate else None
        if substrate_id is None:
            return SampleList(samples=[])
        rows = await self.growth_db.get_samples_for_substrate(substrate_id)
        return SampleList(samples=[self._sample_info(r) for r in rows])

    async def get_sample(self, req: SampleId) -> SampleDetail:
        row = await self.growth_db.get_sample(req.sample_id)
        if row is None:
            return SampleDetail(sample=None, layers=[])
        layers = await self.growth_db.get_layer_stack(req.sample_id)
        return SampleDetail(
            sample=self._sample_info(row),
            layers=[
                LayerInfo(
                    seq=l["seq"], material=l["material"], num_pulse=l["num_pulse"],
                    step_id=l["step_id"], started_at=l["started_at"], is_dryrun=l["is_dryrun"],
                )
                for l in layers
            ],
        )

    async def sample_history(self, req: ListSteps) -> StepList:
        rows = await self.growth_db.get_steps(
            session_id=req.session_id, sample_id=req.sample_id
        )
        steps = [
            StepInfo(
                step_id=r[0], parent_step_id=r[3], sample_id=r[4], kind=r[5],
                params=_loads(r[6]), result=_loads(r[7]),
                ok=None if r[8] is None else bool(r[8]), error=r[9],
                actor=r[10], source=r[11], started_at=r[12] or 0.0, ended_at=r[13],
            )
            for r in rows
        ]
        # Newest last, but bounded -- a long campaign's journal is not something to
        # push through one RPC by accident.
        return StepList(steps=steps[-req.limit:] if req.limit else steps)

    async def add_measurement(self, req: AddMeasurement) -> MeasurementId:
        measurement_id = await self.growth_db.add_measurement(
            req.sample_id, req.kind, value=req.value,
            detail=json.dumps(req.detail) if req.detail else None,
            source=req.source, step_id=req.step_id, record_id=req.record_id,
        )
        return MeasurementId(measurement_id=measurement_id)

    async def list_measurements(self, req: ListMeasurements) -> MeasurementList:
        rows = await self.growth_db.query_measurements(
            sample_id=req.sample_id, kind=req.kind, substrate_id=req.substrate_id
        )
        out = []
        for r in rows:
            # measurement_id, sample_id, step_id, experiment_id, kind, value, detail,
            # source, record_id, created_at
            out.append(MeasurementInfo(
                measurement_id=r[0], sample_id=r[1], kind=r[4], value=r[5],
                detail=_loads(r[6]), source=r[7], created_at=r[9],
                conditions=await self.growth_db.growth_conditions(r[1]),
            ))
        return MeasurementList(measurements=out)

    # --- gated: laser power --------------------------------------------------------

    async def begin_set_laser_power(self, req: BeginSetLaserPower) -> Ack:
        already_satisfied, _ = await self.manager.begin_set_laser_power(req.laser_power, req.target_id, req.force)
        if not already_satisfied:
            await self._set_pending("laser_power", f"Set laser power to {req.laser_power:.2f} W and read the meter")
        return Ack()

    async def confirm_laser_power(self, req: ConfirmLaserPower) -> LaserPowerResult:
        self._require_pending("laser_power")
        measured = await self.manager.confirm_laser_power(req.measured_power)
        await self._clear_pending()
        return LaserPowerResult(measured_power=measured)

    # --- gated: mask-center calibration --------------------------------------------

    async def begin_align_center_mask(self, req: Empty) -> Ack:
        await self.manager.begin_align_center_mask()
        await self._set_pending(
            "mask_center_alignment",
            "Align the mask center to the RHEED cathode luminescence center, then confirm the position",
        )
        return Ack()

    async def confirm_center_mask(self, req: ConfirmCenterMask) -> Ack:
        self._require_pending("mask_center_alignment")
        await self.manager.confirm_center_mask(req.position)
        await self._clear_pending()
        return Ack()

    # --- gated: mask-center check loop ----------------------------------------------

    async def begin_check_mask_center(self, req: Empty) -> Ack:
        await self.manager.begin_check_mask_center()
        await self._set_pending("mask_center_check", "Is the mask centered on camera?")
        return Ack()

    async def confirm_mask_center(self, req: ConfirmMaskCenter) -> PendingStatus:
        self._require_pending("mask_center_check")
        still_pending = await self.manager.confirm_mask_center(req.aligned, req.corrected_position)
        if still_pending:
            await self._push(pending_confirmation=self._pending)
            return PendingStatus(pending=self._pending)
        await self._clear_pending()
        return PendingStatus(pending=None)

    # --- gated: RHEED gain tuning ------------------------------------------------

    async def begin_adjust_rheed_gain(self, req: Empty) -> Ack:
        config = await self.manager.begin_adjust_rheed_gain()
        await self._set_pending(
            "rheed_gain",
            f"Tune gain 0-240 (current: {config.gain}); view via rheed.camera, "
            "set_rheed_gain to try a value, confirm_rheed_gain when good",
        )
        return Ack()

    async def set_rheed_gain(self, req: SetRheedGain) -> Ack:
        self._require_pending("rheed_gain")
        await self.manager.set_rheed_gain(req.gain)
        return Ack()

    async def confirm_rheed_gain(self, req: Empty) -> Ack:
        self._require_pending("rheed_gain")
        await self.manager.confirm_rheed_gain()
        await self._clear_pending()
        return Ack()

    # --- gated: per-pixel keep/drop ---------------------------------------------

    async def begin_check_rheed_pixels(self, req: Empty) -> PixelCheckStatus:
        status = await self.manager.begin_check_rheed_pixels()
        if status["done"]:
            return PixelCheckStatus(**status)
        await self._set_pending("pixel_check", "Keep this pixel position? (visual RHEED check)")
        return PixelCheckStatus(**status)

    async def resolve_pixel_check(self, req: ResolvePixelCheck) -> PixelCheckStatus:
        self._require_pending("pixel_check")
        status = await self.manager.resolve_pixel_check(req.keep)
        if status["done"]:
            await self._clear_pending()
        else:
            await self._push(pending_confirmation=self._pending)
        return PixelCheckStatus(**status)

    # --- generic confirm -----------------------------------------------------------

    async def confirm(self, req: ConfirmProceed) -> Ack:
        if self._pending is None or self._pending.id != req.confirmation_id:
            raise ValueError("no matching pending confirmation")
        await self._clear_pending()
        return Ack()
