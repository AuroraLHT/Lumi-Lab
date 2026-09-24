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
    AutoAlignMaskCenter,
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
    CheckLogging,
    LoggingAlive,
    LoggingStatus,
    ExperimentId,
    ExperimentInfo,
    ExperimentList,
    ListExperiments,
    ListQuery,
    ListRecords,
    PageInfo,
    ProjectId,
    ProjectList,
    RecordId,
    RecordInfo,
    RecordList,
    ReopenPosition,
    ReopenResult,
    ResolvePixelCheck,
    ResumeSubstrate,
    RetireExperiment,
    RetireMeasurement,
    RetireProject,
    RetireRecord,
    RetireSubstrate,
    SubstrateId,
    UpdateExperiment,
    UpdateMeasurement,
    UpdateProject,
    UpdateRecord,
    UpdateSample,
    SampleAngle,
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
    StartMiLogging,
    StartStorage,
    StorageResult,
    SubstrateInfo,
    SubstrateList,
    SubstrateSummary,
    ListSubstrates,
    UpdateSubstrate,
    TargetId,
    TargetMap,
    TargetName,
    TaskAck,
    TaskEvent,
    TemperatureReading,
    ToTemperature,
    ValveStatus,
)
from lumi.experiment.db import GrowthDB, column as _column, to_epoch, to_iso
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
        "auto_align_center_mask",
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
            chamber_fiducial=self.sources.get("chamber_fiducial"),
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

    async def _require_heating_laser(self, target_temperature: float | None = None) -> None:
        """The same refusal `manager.to_temperature` makes, hoisted ahead of
        `_start_task`.

        A long-running op reports failure on the update channel, not to its caller --
        `_start_task` returns a TaskAck the moment it spawns the runner. That is fine
        for a client subscribed to `driver.update`, but the MCP server exposes ops and
        nothing else: an agent has no way to read `current_task`, so a refusal raised
        inside the task is invisible to it and the ramp looks like it started. Checked
        here, it comes back as an error on the tool call itself.

        `target_temperature` mirrors `manager.to_temperature`'s sub-threshold branch:
        a setpoint below the PID-engage threshold is either an RT growth (diode
        deliberately off) or a cooldown, and neither needs the diode lit.
        """
        if (
            target_temperature is not None
            and target_temperature < self.manager.bounds.temperature_pid_engage_threshold
        ):
            return
        if not await self.manager.is_heating_laser_on():
            raise RuntimeError(
                "heating laser is off -- call initiate_heating_laser first; ramping "
                "with it off sets a setpoint no current can reach"
            )

    async def to_temperature(self, req: ToTemperature) -> TaskAck:
        await self._require_heating_laser(req.temperature)
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
            substrate_name=substrate.name, manufacturer=substrate.manufacture,
            manufacture_date=substrate.manufacture_date, state=substrate.state,
            created_at=to_epoch(substrate.created_at),
            created_at_iso=to_iso(substrate.created_at),
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

    # --- bookkeeping corrections -----------------------------------------------

    async def list_substrates(self, req: ListSubstrates) -> SubstrateList:
        filters = {"materials": req.materials}
        rows = await self.manager.list_substrates(**self._query(req, **filters))
        return SubstrateList(
            substrates=[SubstrateSummary(**row) for row in rows],
            page=await self._page("substrate", req, **filters),
        )

    async def get_substrate(self, req: SubstrateId) -> SubstrateInfo:
        row = await self._fetch("substrate", req.substrate_id)
        return self._substrate_info(await self.manager._load_substrate(row))

    async def update_substrate(self, req: UpdateSubstrate) -> SubstrateInfo:
        substrate = await self.manager.update_substrate(
            req.substrate_id,
            materials=req.materials, orientation=req.orientation, thickness=req.thickness,
            substrate_name=req.substrate_name, manufacturer=req.manufacturer,
            manufacture_date=req.manufacture_date, width=req.width, height=req.height,
            pixel_spacing=req.pixel_spacing, positions=req.positions or None,
        )
        return self._substrate_info(substrate)

    async def reopen_position(self, req: ReopenPosition) -> ReopenResult:
        index = await self.manager.reopen_position(req.index, force=req.force)
        substrate = self.manager.current_substrate
        return ReopenResult(
            index=index,
            substrate=self._substrate_info(substrate) if substrate else None,
        )

    async def retire_substrate(self, req: RetireSubstrate) -> SubstrateInfo:
        substrate = await self.manager.retire_substrate(req.substrate_id, retire=req.retire)
        return self._substrate_info(substrate)

    async def unload_substrate(self, req: Empty) -> CurrentSubstrateResponse:
        substrate = self.manager.unload_substrate()
        return CurrentSubstrateResponse(
            substrate=self._substrate_info(substrate) if substrate else None
        )

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

    async def check_logging_alive(self, req: CheckLogging) -> LoggingAlive:
        alive, waited, last_stamp = await self.manager.check_logging_alive(req.timeout_s)
        return LoggingAlive(alive=alive, waited_s=waited, last_stamp=last_stamp)

    # --- fast hardware control ---------------------------------------------------

    async def set_target(self, req: SetTarget) -> Ack:
        await self.manager.set_target(req.target_id, req.rotation_mode, req.twist_mode)
        return Ack()

    async def start_mi_logging(self, req: StartMiLogging) -> LoggingStatus:
        file_name, interval_s = await self.manager.start_mi_logging(req.interval_s, req.file_name or None)
        return LoggingStatus(file_name=file_name, interval_s=interval_s)

    async def stop_mi_logging(self, req: Empty) -> Ack:
        await self.manager.stop_mi_logging()
        return Ack()

    async def move_mask_to_position(self, req: MoveTo) -> Ack:
        await self.manager.move_mask_to_position(req.position)
        return Ack()

    async def move_rheed_to_position(self, req: MoveTo) -> Ack:
        await self.manager.move_rheed_to_position(req.position)
        return Ack()

    async def rotate_sample_to(self, req: SampleAngle) -> Ack:
        await self.manager.rotate_sample_to(req.angle)
        return Ack()

    async def rotate_sample_by(self, req: SampleAngle) -> Ack:
        await self.manager.rotate_sample_by(req.angle)
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
        result = await self.manager.start_storage(
            req.project_name, req.is_dryrun,
            save_frame=req.save_frame, save_ai=req.save_ai, save_log=req.save_log,
            save_integration=req.save_integration, force_rewrite=req.force_rewrite,
        )
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

    # --- growth.db as data: list / get / update / retire ---------------------------
    # Everything below reads and writes the database directly rather than going
    # through the manager: none of it touches the chamber, and routing record-keeping
    # through the object that owns the hardware is what made `substrates` a stack you
    # could not correct. The manager still owns the substrate ops, because those move
    # what is loaded on the chamber.

    def _query(self, req: ListQuery, **filters) -> dict:
        """A ListQuery plus this op's own filters, as kwargs for GrowthDB.list_rows."""
        return dict(
            filters=filters, since=req.since, until=req.until, limit=req.limit,
            offset=req.offset, order=req.order, search=req.search,
            include_retired=req.include_retired,
        )

    async def _page(self, table: str, req: ListQuery, **filters) -> PageInfo:
        total = await self.growth_db.count_rows(
            table, filters=filters, since=req.since, until=req.until,
            search=req.search, include_retired=req.include_retired,
        )
        return PageInfo(
            total=total, limit=req.limit, offset=req.offset,
            has_more=req.offset + req.limit < total,
        )

    async def _fetch(self, table: str, row_id: int):
        row = await self.growth_db.get_row(table, row_id)
        if row is None:
            raise ValueError(f"no {table} with id {row_id}")
        return row

    @staticmethod
    def _set_fields(req, *names) -> dict:
        """The fields a caller actually set. Every update_* payload is all-optional so
        that a request carrying one field writes one column; None means 'leave it', not
        'set it to null'."""
        return {n: getattr(req, n) for n in names if getattr(req, n) is not None}

    async def _retire(self, table: str, row_id: int, retire: bool):
        await self.growth_db.set_row_state(table, row_id, "retired" if retire else "active")
        return await self._fetch(table, row_id)

    # --- project -------------------------------------------------------------------

    @staticmethod
    def _project_info(row) -> ProjectInfo:
        at = _column(row, "project_created_at")
        return ProjectInfo(
            project_id=row["project_id"], project_name=row["project_name"],
            description=_column(row, "description"),
            created_at=to_epoch(at), created_at_iso=to_iso(at),
            state=_column(row, "state") or "active",
        )

    async def list_projects(self, req: ListQuery) -> ProjectList:
        rows = await self.growth_db.list_rows("project", **self._query(req))
        return ProjectList(
            projects=[self._project_info(r) for r in rows],
            page=await self._page("project", req),
        )

    async def get_project(self, req: ProjectId) -> ProjectInfo:
        return self._project_info(await self._fetch("project", req.project_id))

    async def update_project(self, req: UpdateProject) -> ProjectInfo:
        await self._fetch("project", req.project_id)
        fields = self._set_fields(req, "project_name", "description")
        await self.growth_db.update_row("project", req.project_id, **fields)
        return self._project_info(await self._fetch("project", req.project_id))

    async def retire_project(self, req: RetireProject) -> ProjectInfo:
        return self._project_info(await self._retire("project", req.project_id, req.retire))

    # --- experiment ----------------------------------------------------------------

    @staticmethod
    def _experiment_info(row) -> ExperimentInfo:
        at = _column(row, "experiment_created_at")
        return ExperimentInfo(
            experiment_id=row["experiment_id"], experiment_uuid=_column(row, "experiment_uuid"),
            substrate_id=_column(row, "substrate_id"), project_id=_column(row, "project_id"),
            is_pixel=bool(_column(row, "is_pixel")),
            pixel_location=_column(row, "pixel_location"),
            temperature=_column(row, "temperature"), pressure=_column(row, "pressure"),
            laser_power=_column(row, "laser_power"),
            laser_pulse_rate=_column(row, "laser_pulse_rate"),
            target_material=_column(row, "target_material"),
            num_pulse=_column(row, "num_pulse"),
            do_preablation=bool(_column(row, "do_preablation")),
            preablation_pulse=_column(row, "preablation_pulse"),
            preablation_frequency=_column(row, "preablation_frequency"),
            before_experiment_waittime=_column(row, "before_experiment_waittime"),
            after_experiment_waittime=_column(row, "after_experiment_waittime"),
            ramp_rate=_column(row, "ramp_rate"),
            created_at=to_epoch(at), created_at_iso=to_iso(at),
            state=_column(row, "state") or "active",
        )

    async def list_experiments(self, req: ListExperiments) -> ExperimentList:
        filters = {"substrate_id": req.substrate_id, "project_id": req.project_id}
        rows = await self.growth_db.list_rows("experiment", **self._query(req, **filters))
        return ExperimentList(
            experiments=[self._experiment_info(r) for r in rows],
            page=await self._page("experiment", req, **filters),
        )

    async def get_experiment(self, req: ExperimentId) -> ExperimentInfo:
        return self._experiment_info(await self._fetch("experiment", req.experiment_id))

    async def update_experiment(self, req: UpdateExperiment) -> ExperimentInfo:
        await self._fetch("experiment", req.experiment_id)
        fields = self._set_fields(
            req, "project_id", "temperature", "pressure", "laser_power",
            "laser_pulse_rate", "target_material", "num_pulse", "ramp_rate",
        )
        await self.growth_db.update_row("experiment", req.experiment_id, **fields)
        return self._experiment_info(await self._fetch("experiment", req.experiment_id))

    async def retire_experiment(self, req: RetireExperiment) -> ExperimentInfo:
        row = await self._retire("experiment", req.experiment_id, req.retire)
        # Retiring a growth hands its pixel back, but only the database knows that so
        # far -- a substrate already loaded is holding the old progress in memory.
        substrate = self.manager.current_substrate if self.manager else None
        if substrate is not None and substrate.db_id == _column(row, "substrate_id"):
            substrate.restore_progress(
                await self.growth_db.get_used_pixel_indices(substrate.db_id)
            )
        return self._experiment_info(row)

    # --- record --------------------------------------------------------------------

    @staticmethod
    def _record_info(row) -> RecordInfo:
        at = _column(row, "record_created_at")
        return RecordInfo(
            record_id=row["record_id"], record_uuid=_column(row, "record_uuid"),
            experiment_id=_column(row, "experiment_id"),
            record_name=_column(row, "record_name"),
            created_at=to_epoch(at), created_at_iso=to_iso(at),
            state=_column(row, "state") or "active",
        )

    async def list_records(self, req: ListRecords) -> RecordList:
        filters = {"experiment_id": req.experiment_id}
        rows = await self.growth_db.list_rows("record", **self._query(req, **filters))
        return RecordList(
            records=[self._record_info(r) for r in rows],
            page=await self._page("record", req, **filters),
        )

    async def get_record(self, req: RecordId) -> RecordInfo:
        return self._record_info(await self._fetch("record", req.record_id))

    async def update_record(self, req: UpdateRecord) -> RecordInfo:
        await self._fetch("record", req.record_id)
        fields = self._set_fields(req, "record_name", "experiment_id")
        await self.growth_db.update_row("record", req.record_id, **fields)
        return self._record_info(await self._fetch("record", req.record_id))

    async def retire_record(self, req: RetireRecord) -> RecordInfo:
        return self._record_info(await self._retire("record", req.record_id, req.retire))

    # --- sample tracking -----------------------------------------------------------

    @staticmethod
    def _sample_info(row) -> SampleInfo:
        at = _column(row, "created_at")
        return SampleInfo(
            sample_id=row["sample_id"], sample_uuid=_column(row, "sample_uuid") or "",
            parent_sample_id=_column(row, "parent_sample_id"),
            substrate_id=row["substrate_id"], kind=_column(row, "kind") or "position",
            pixel_index=_column(row, "pixel_index"), position_mm=_column(row, "position_mm"),
            sample_name=_column(row, "sample_name"), state=_column(row, "state") or "planned",
            notes=_column(row, "notes"),
            created_at=to_epoch(at), created_at_iso=to_iso(at),
        )

    async def list_samples(self, req: ListSamples) -> SampleList:
        substrate_id = req.substrate_id
        if substrate_id is None:
            substrate = self.manager.current_substrate if self.manager else None
            substrate_id = substrate.db_id if substrate else None
        filters = {"substrate_id": substrate_id, "kind": req.kind, "state": req.state}
        rows = await self.growth_db.list_rows("sample", **self._query(req, **filters))
        return SampleList(
            samples=[self._sample_info(r) for r in rows],
            page=await self._page("sample", req, **filters),
        )

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
                    step_id=l["step_id"], started_at=l["started_at"],
                    started_at_iso=to_iso(l["started_at"]), is_dryrun=l["is_dryrun"],
                )
                for l in layers
            ],
        )

    async def update_sample(self, req: UpdateSample) -> SampleInfo:
        await self._fetch("sample", req.sample_id)
        fields = self._set_fields(req, "sample_name", "notes", "state", "position_mm")
        await self.growth_db.update_row("sample", req.sample_id, **fields)
        return self._sample_info(await self._fetch("sample", req.sample_id))

    async def sample_history(self, req: ListSteps) -> StepList:
        filters = {"sample_id": req.sample_id, "session_id": req.session_id,
                   "kind": req.kind}
        # The journal reads oldest-first unless asked otherwise: a step chain is a
        # narrative, and `order` is there for a UI that wants the tail.
        rows = await self.growth_db.list_rows(
            "step", **{**self._query(req, **filters), "order": req.order or "asc"}
        )
        return StepList(
            steps=[
                StepInfo(
                    step_id=r["step_id"], parent_step_id=_column(r, "parent_step_id"),
                    sample_id=_column(r, "sample_id"), kind=r["kind"],
                    params=_loads(_column(r, "params")), result=_loads(_column(r, "result")),
                    ok=None if _column(r, "ok") is None else bool(r["ok"]),
                    error=_column(r, "error"), actor=_column(r, "actor"),
                    source=_column(r, "source"),
                    started_at=_column(r, "started_at") or 0.0,
                    started_at_iso=to_iso(_column(r, "started_at")),
                    ended_at=_column(r, "ended_at"),
                    ended_at_iso=to_iso(_column(r, "ended_at")),
                )
                for r in rows
            ],
            page=await self._page("step", req, **filters),
        )

    async def add_measurement(self, req: AddMeasurement) -> MeasurementId:
        measurement_id = await self.growth_db.add_measurement(
            req.sample_id, req.kind, value=req.value,
            detail=json.dumps(req.detail) if req.detail else None,
            source=req.source, step_id=req.step_id, record_id=req.record_id,
        )
        return MeasurementId(measurement_id=measurement_id)

    async def _measurement_info(self, row) -> MeasurementInfo:
        at = _column(row, "created_at")
        return MeasurementInfo(
            measurement_id=row["measurement_id"], sample_id=row["sample_id"],
            kind=row["kind"], value=_column(row, "value"),
            detail=_loads(_column(row, "detail")), source=_column(row, "source"),
            created_at=to_epoch(at), created_at_iso=to_iso(at),
            state=_column(row, "state") or "active",
            conditions=await self.growth_db.growth_conditions(row["sample_id"]),
        )

    async def list_measurements(self, req: ListMeasurements) -> MeasurementList:
        # substrate_id is not a column on `measurement` -- it resolves through the
        # sample tree, so that filter stays in query_measurements and the shared
        # list/page path handles the rest.
        if req.substrate_id is not None:
            rows = await self.growth_db.query_measurements(
                sample_id=req.sample_id, kind=req.kind, substrate_id=req.substrate_id
            )
            if not req.include_retired:
                rows = [r for r in rows if (_column(r, "state") or "active") != "retired"]
            total = len(rows)  # the whole match, before this page is cut out of it
            page = rows[req.offset:req.offset + req.limit]
            return MeasurementList(
                measurements=[await self._measurement_info(r) for r in page],
                page=PageInfo(total=total, limit=req.limit, offset=req.offset,
                              has_more=req.offset + req.limit < total),
            )
        filters = {"sample_id": req.sample_id, "kind": req.kind}
        rows = await self.growth_db.list_rows("measurement", **self._query(req, **filters))
        return MeasurementList(
            measurements=[await self._measurement_info(r) for r in rows],
            page=await self._page("measurement", req, **filters),
        )

    async def get_measurement(self, req: MeasurementId) -> MeasurementInfo:
        return await self._measurement_info(
            await self._fetch("measurement", req.measurement_id)
        )

    async def update_measurement(self, req: UpdateMeasurement) -> MeasurementInfo:
        await self._fetch("measurement", req.measurement_id)
        fields = self._set_fields(req, "kind", "value", "source")
        if req.detail is not None:
            fields["detail"] = json.dumps(req.detail)
        await self.growth_db.update_row("measurement", req.measurement_id, **fields)
        return await self._measurement_info(
            await self._fetch("measurement", req.measurement_id)
        )

    async def retire_measurement(self, req: RetireMeasurement) -> MeasurementInfo:
        return await self._measurement_info(
            await self._retire("measurement", req.measurement_id, req.retire)
        )

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

    async def auto_align_center_mask(self, req: AutoAlignMaskCenter) -> TaskAck:
        params = req.model_dump(exclude={"role_wait_timeout_s"})

        async def run() -> dict:
            await self._await_mask_center_role(req.role_wait_timeout_s)
            return await self.manager.auto_align_center_mask(**params)

        return await self._start_task("auto_align_center_mask", run(), req.model_dump())

    async def _await_mask_center_role(self, timeout_s: float, poll: float = 1.0) -> None:
        """Hold the alignment until a marker is tagged `mask-center`, asking for it
        through a `fiducial_role` pending confirmation rather than failing outright --
        the operator tags the marker (chamber.fiducial.set_role, from any client) and
        the task carries on by itself. Resolving the confirmation with `confirm`
        instead cancels the alignment; so does `timeout_s` running out. A timeout of 0
        fails straight away, for an unattended caller with nobody to ask."""
        marker_id, problem = await self.manager.find_mask_center_marker()
        if marker_id is not None:
            return
        if timeout_s <= 0:
            raise RuntimeError(problem)

        await self._set_pending("fiducial_role", f"{problem}; mask auto-alignment continues once it is set")
        pending_id = self._pending.id
        deadline = time.monotonic() + timeout_s
        try:
            while True:
                await asyncio.sleep(poll)
                if self._pending is None or self._pending.id != pending_id:
                    raise RuntimeError("mask auto-alignment cancelled while waiting for the mask-center marker")
                marker_id, problem = await self.manager.find_mask_center_marker()
                if marker_id is not None:
                    log.info("mask-center role set to %r -- continuing auto-alignment", marker_id)
                    return
                if time.monotonic() > deadline:
                    raise RuntimeError(f"gave up after {timeout_s:.0f}s waiting: {problem}")
        finally:
            if self._pending is not None and self._pending.id == pending_id:
                await self._clear_pending()

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
