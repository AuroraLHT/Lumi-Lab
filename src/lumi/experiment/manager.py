"""The PLD experiment drivers: substrate/pixel bookkeeping, chamber reads, and
hardware control.

Ported from ~/Projects/OperationsNotebooks/src/manager.py's BaseExperimentManager /
SingleDepoExperimentManager / PixelExperimentManager, adapted to lumi's current
client API (MiCommandRunner wrapping ChamberMiModeClient, not the retired
lumi.client.pascal.MIModeClient) and to run inside a node process rather than a
notebook kernel:

- Every `ainput`/`strong_ainput` site that only collected a parameter the caller
  already has is gone -- the caller (an ExperimentHandler op) supplies it as a typed
  request field instead.
- Every site that genuinely needs a human to act or read something physical (laser
  power meter, substrate swap, camera gain, a visual pixel check) is split into a
  `begin_*`/`confirm_*`/`resolve_*` pair. The actual PendingConfirmation bookkeeping
  (ids, the update-channel push) lives in ExperimentHandler, which is shared
  machinery across every gated op here -- these methods just do the hardware half and
  report back what happened.
- `perform_experiment`'s canned recipes are NOT ported here at all: they are plain
  client-side Python in `lumi.experiment.recipes`, composed from the primitives
  below, so a notebook user can recombine them freely instead of one fixed sequence.
- GP/BO (gp.py, analyze_rheed_video) is out of scope for this phase; the
  DepositedMaterial/has_material bookkeeping that only existed to feed that closed
  loop is deferred with it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from math import floor

from lumi.contracts.payloads.chamber import SectionQuery
from lumi.contracts.payloads.storage import StorageRequest
from lumi.experiment.db import GrowthDB
from lumi.experiment.mi import MiCommandRunner
from lumi.generated.clients.chamber import ChamberConfigClient, ChamberLogClient
from lumi.generated.clients.rheed import RheedCameraClient
from lumi.generated.clients.storage import StorageStorageClient
from lumi.pascal import command as pcmd

log = logging.getLogger(__name__)


def _projection(base_xs, base_ys, target_ys, source_x=0.0, source_y=0.0):
    tans = (base_xs - source_x) / base_ys
    return tans * target_ys + source_x


def _truthy(value) -> bool:
    """The chamber log's boolean-ish fields (e.g. "Motor free") arrive as whatever
    string PASCAL happened to print, not a real bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("true", "1", "on", "yes")


async def count_down(seconds: float, prefix: str = "", interval: float = 10.0) -> None:
    start = time.time()
    remaining = seconds
    while remaining > 0:
        sleep_time = min(interval, remaining)
        await asyncio.sleep(sleep_time)
        remaining = seconds - (time.time() - start)
    log.info("%s finished", prefix.capitalize() if prefix else "wait")


@dataclass
class PLDChamberConfiguration:
    """Chamber geometry/calibration a growth is planned against. Sourced from
    settings.experiment.pld_config -- previously copy-pasted into each notebook run
    and drifting with every physical realignment."""

    center_mask_pos: float
    center_rheed_pos: float
    plumb_center: float
    target_to_mask_distance: float
    mask_to_sample_distance: float
    rheed_limit: tuple[float, float]
    mask_block_position: float


@dataclass
class ExperimentBounds:
    """Hardware bounds. Sourced from settings.experiment.bounds -- previously bare
    literals inside method bodies (a `< 160` assert, a `[160, 1000]` clamp, a `220`
    PID-engage special case)."""

    mask_travel_max: float
    temperature_min: float
    temperature_max: float
    temperature_pid_engage_threshold: float
    warm_up_step: float
    warm_up_current_ramp_rate: float
    warm_up_wait_interval: float
    warm_up_max_waittime: float


class Substrate:
    def __init__(
        self,
        materials: str,
        orientation: str,
        width: float,
        height: float | None = None,
        thickness: float | None = None,
        pixel_spacing: float | None = None,
        positions: list[float] | None = None,
        db_id: int | None = None,
        substrate_uuid: str | None = None,
        manufacture: str | None = None,
        manufacture_date: str | None = None,
        name: str | None = None,
    ) -> None:
        self.materials = materials
        self.orientation = orientation
        self.width = width
        self.height = height
        self.thickness = thickness
        self.pixel_spacing = pixel_spacing
        self.current_position_id = 0
        self.uuid = substrate_uuid or str(uuid.uuid4())
        self.db_id = db_id
        self.manufacture = manufacture
        self.manufacture_date = manufacture_date
        self.name = name

        self.positions = positions if positions is not None else self._compute_positions()
        self._accessed_position_ids: set[int] = set()

    @property
    def accessible_positions(self) -> list[float]:
        return [p for i, p in enumerate(self.positions) if i not in self._accessed_position_ids]

    def _compute_positions(self) -> list[float]:
        if self.pixel_spacing is None:
            return [self.width / 2]
        positions = [0.0]
        for i in range(1, floor(self.width / 2 / self.pixel_spacing) + 1):
            positions.append(i * self.pixel_spacing)
            positions.append(-i * self.pixel_spacing)
        return sorted(positions)

    def drop_positions(self, positions_to_disable: list[float]) -> None:
        self.positions = [p for p in self.positions if p not in positions_to_disable]

    def get_current_position(self) -> tuple[bool, float | None]:
        if self.current_position_id < len(self.positions):
            return True, self.positions[self.current_position_id]
        return False, None

    def finish_current_position(self) -> None:
        self._accessed_position_ids.add(self.current_position_id)
        self.current_position_id += 1


class BaseExperimentManager:
    """Shared hardware/DB primitives for driving a PLD growth."""

    def __init__(
        self,
        *,
        chamber_mi: MiCommandRunner,
        chamber_log: ChamberLogClient,
        chamber_config: ChamberConfigClient,
        rheed_camera: RheedCameraClient,
        storage: StorageStorageClient,
        growth_db: GrowthDB,
        pld_config: PLDChamberConfiguration,
        bounds: ExperimentBounds,
        target_mapper: dict[str, str],
    ) -> None:
        self.chamber_mi = chamber_mi
        self.chamber_log = chamber_log
        self.chamber_config = chamber_config
        self.rheed_camera = rheed_camera
        self.storage = storage
        self.growth_db = growth_db
        self.pld_config = pld_config
        self.bounds = bounds
        self.target_mapper = target_mapper

        self.substrates: list[Substrate] = []
        self.project_id: int | None = None
        self.project_name: str | None = None
        self._laser_power_set: float | None = None
        self._laser_power_real: float | None = None
        # Stashed between begin_set_laser_power() and confirm_laser_power().
        self._pending_laser_target_id: str | None = None
        self._pending_laser_power: float | None = None
        # Stashed between begin_check_rheed_pixels() and resolve_pixel_check().
        self._pixel_check_index: int = 0
        self._pixel_check_dropped: list[int] = []

    @property
    def current_substrate(self) -> Substrate | None:
        return self.substrates[-1] if self.substrates else None

    # --- bookkeeping ---------------------------------------------------------

    async def register_project(self, project_name: str, description: str = "") -> int:
        self.project_id = await self.growth_db.add_project(project_name, description)
        self.project_name = project_name
        return self.project_id

    def _pixel_to_mask_position(self, pixel_position: float) -> float:
        target_to_sample_distance = self.pld_config.target_to_mask_distance + self.pld_config.mask_to_sample_distance
        return self.pld_config.center_mask_pos + _projection(
            pixel_position, target_to_sample_distance, self.pld_config.target_to_mask_distance, self.pld_config.plumb_center,
        )

    def _pixel_to_rheed_position(self, pixel_position: float) -> float:
        return -pixel_position + self.pld_config.center_rheed_pos

    def _drop_positions_by_rheed_limit(self, substrate: Substrate) -> None:
        lo, hi = min(self.pld_config.rheed_limit), max(self.pld_config.rheed_limit)
        to_drop = [
            p for p in substrate.positions
            if not (lo <= self._pixel_to_rheed_position(p) <= hi)
        ]
        if to_drop:
            log.info("dropping positions outside the RHEED limit: %s", to_drop)
            substrate.drop_positions(to_drop)

    async def register_substrate(
        self,
        *,
        materials: str,
        orientation: str,
        width: float,
        height: float = 0.0,
        thickness: float = 0.0,
        pixel_spacing: float | None = None,
        positions: list[float] | None = None,
        manufacturer: str | None = None,
        manufacture_date: str | None = None,
        substrate_name: str | None = None,
    ) -> Substrate:
        substrate = Substrate(
            materials, orientation, width, height, thickness, pixel_spacing, positions or None,
            manufacture=manufacturer, manufacture_date=manufacture_date, name=substrate_name,
        )
        self._drop_positions_by_rheed_limit(substrate)

        substrate.db_id = await self.growth_db.add_substrate(
            substrate.materials, substrate.orientation, substrate.width, substrate.height,
            substrate.thickness, substrate.pixel_spacing, json.dumps(substrate.positions),
            substrate_uuid=substrate.uuid, manufacture=manufacturer,
            manufacture_date=manufacture_date, substrate_name=substrate_name,
        )
        self.substrates.append(substrate)
        return substrate

    async def resume_substrate(self, substrate_id: int) -> Substrate:
        row = await self.growth_db.get_substrate(substrate_id)
        if row is None:
            raise ValueError(f"no substrate with id {substrate_id}")
        substrate = Substrate(
            materials=row[2], orientation=row[3], width=row[5], height=row[4], thickness=row[6],
            pixel_spacing=row[7], positions=json.loads(row[8]) if row[8] else None,
            db_id=row[0], substrate_uuid=row[1], manufacture=row[9], manufacture_date=row[10],
            name=row[11] if len(row) > 11 else None,
        )
        self.substrates.append(substrate)
        return substrate

    async def finish_substrate(self) -> None:
        if self.current_substrate is not None:
            self.current_substrate.finish_current_position()

    async def finish_current_pixel(self) -> None:
        if self.current_substrate is not None:
            self.current_substrate.finish_current_position()

    async def get_target_name_by_id(self, target_id: str) -> str:
        field = self.target_mapper.get(target_id)
        if field is None:
            raise ValueError(f"target {target_id!r} not in target_mapper: {sorted(self.target_mapper)}")
        section = await self.chamber_config.get_configs_by_section(SectionQuery(section="TGsettings"))
        return section.values[field]

    async def get_target_id_by_name(self, target_name: str) -> str:
        section = await self.chamber_config.get_configs_by_section(SectionQuery(section="TGsettings"))
        rev = {v: k for k, v in self.target_mapper.items()}
        for field in self.target_mapper.values():
            if section.values.get(field) == target_name:
                return rev[field]
        valid = [section.values.get(f) for f in self.target_mapper.values()]
        raise ValueError(f"target {target_name!r} not found; valid targets: {valid}")

    async def show_available_targets(self) -> dict[str, str]:
        section = await self.chamber_config.get_configs_by_section(SectionQuery(section="TGsettings"))
        rev = {v: k for k, v in self.target_mapper.items()}
        return {rev[f]: section.values.get(f) for f in self.target_mapper.values()}

    # --- chamber reads ---------------------------------------------------------

    async def _log_values(self) -> dict:
        batch = await self.chamber_log.log()
        if not batch.entries:
            raise RuntimeError("chamber log has no rows yet")
        return batch.entries[-1].values

    async def get_current_log(self):
        batch = await self.chamber_log.log()
        if not batch.entries:
            raise RuntimeError("chamber log has no rows yet")
        return batch.entries[-1]

    async def is_motor_free(self) -> bool:
        values = await self._log_values()
        return _truthy(values["Motor free"])

    async def get_current_pressure(self, gauge_name: str | None = None) -> tuple[float, str | None]:
        values = await self._log_values()
        if gauge_name is not None:
            if gauge_name not in values:
                raise ValueError(f"gauge {gauge_name!r} not found in the log")
            return float(values[gauge_name]), gauge_name

        if str(values["Vac Pres Main"]) == "0.00E+0":
            if float(values["Prc Pres Main"]) == 1:
                return float(values["Prc Pres Main2"]), "Prc Pres Main2"
            return float(values["Prc Pres Main"]), "Prc Pres Main"
        return float(values["Vac Pres Main"]), "Vac Pres Main"

    async def get_current_temperature(self) -> float:
        values = await self._log_values()
        return float(values["HT Temp moni"])

    async def get_current_mask_position(self) -> float:
        values = await self._log_values()
        return float(values["Mask1"])

    async def get_valve_status(self) -> dict[str, bool]:
        values = await self._log_values()
        fields = [
            "MV10 (Main)", "MV11 (Main bypass)", "FV1 (Main)", "MV2 (RHEED)",
            "FV2 (RHEED)", "MV3 (L/L)", "FV3 (L/L)", "RV3 (L/L)",
        ]
        return {f: _truthy(values[f]) for f in fields}

    async def get_pump_status(self) -> dict[str, bool]:
        values = await self._log_values()
        fields = [
            "DP1 (Main)", "DP2 (2nd RHEED)", "DP3 (L/L)",
            "TMP1 (Main)", "TMP2 (RHEED)", "TMP3 (L/L)", "TMP4 (2nd RHEED)",
        ]
        return {f: _truthy(values[f]) for f in fields}

    async def get_mfc_status(self, mfc_id: str) -> tuple[float, float]:
        values = await self._log_values()
        return float(values[f"MFC{mfc_id} set"]), float(values[f"MFC{mfc_id} moni"])

    # --- hardware control --------------------------------------------------------

    async def set_mfc_flow(self, mfc_id: str, flow: float) -> None:
        if mfc_id == "1":
            await self.chamber_mi.execute(pcmd.SetMFC1Flow(flow))
        elif mfc_id == "2":
            await self.chamber_mi.execute(pcmd.SetMFC2Flow(flow))
        else:
            raise ValueError(f"invalid MFC id: {mfc_id!r}")

    async def set_mfc_control(self, enabled: bool) -> None:
        """PASCAL's `MFC Control` -- the master gate every MFC's flow passes through.
        Separate from set_mfc_flow because the command takes no channel argument: it
        is one switch for the whole gas line."""
        await self.chamber_mi.execute(pcmd.SetMFCControl(enable=enabled))

    async def set_pressure(self, pressure: float) -> None:
        await self.chamber_mi.execute(pcmd.SetPressure(pressure))

    async def set_pressure_control(self, on: bool) -> None:
        await self.chamber_mi.execute(pcmd.PressureControl(state=pcmd.PascalState(on)))

    async def initiate_heating_laser(self) -> None:
        await self.chamber_mi.execute(pcmd.HeatingLaserLock(locked=False, nowait=False))
        await self.chamber_mi.execute(pcmd.HeatingLaser(state=pcmd.PascalState("ON"), nowait=False))
        await self.chamber_mi.execute(pcmd.HeatingLaserThreshold(state=pcmd.PascalState("ON")))
        await self.chamber_mi.execute(pcmd.TemperatureControl(mode=pcmd.TemperatureControlModeType.PID))

    async def turn_off_heating_laser(self) -> None:
        await self.chamber_mi.execute(pcmd.TemperatureControl(mode=pcmd.TemperatureControlModeType.MANUAL))
        await self.chamber_mi.execute(pcmd.HeatingLaserThreshold(state=pcmd.PascalState("OFF")))
        await self.chamber_mi.execute(pcmd.HeatingLaser(state=pcmd.PascalState("OFF"), nowait=False))
        await self.chamber_mi.execute(pcmd.HeatingLaserLock(locked=True, nowait=False))

    async def move_mask_to_position(self, position: float) -> None:
        if not (0 <= position < self.bounds.mask_travel_max):
            raise ValueError(f"mask position must be in [0, {self.bounds.mask_travel_max}), got {position}")
        # The original asserted `not is_motor_free()` here -- backwards from what the
        # assertion message ("Motor is not free") says, and from what a pre-move
        # safety check should require. Fixed to require the motor free before moving.
        if not await self.is_motor_free():
            raise RuntimeError("motor is not free")
        await self.chamber_mi.execute(pcmd.SetMaskPosition(mask_id=pcmd.MaskID.M1, distance=position, sync=False, nowait=False))

    async def move_rheed_to_position(self, position: float) -> None:
        lo, hi = min(self.pld_config.rheed_limit), max(self.pld_config.rheed_limit)
        if not (lo <= position <= hi):
            raise ValueError(f"RHEED gun position must be in [{lo}, {hi}], got {position}")
        if not await self.is_motor_free():
            raise RuntimeError("motor is not free")
        await self.chamber_mi.execute(pcmd.SetRHEEDGunX(position))

    async def set_target(self, target_id: str, rotation_mode: str = "AUTO", twist_mode: str = "AUTO") -> None:
        await self.chamber_mi.execute(pcmd.SelectTarget(target_id, nowait=False))
        await self.chamber_mi.execute(pcmd.TargetRotationMode(mode=rotation_mode))
        await self.chamber_mi.execute(pcmd.TargetTwistMode(mode=twist_mode))

    async def perform_preablation(
        self, target_id: str, num_pulse: int = 1000, frequency: float = 10.0,
        is_dryrun: bool = False, move_mask_to_block_position: bool = True,
    ) -> None:
        await self.chamber_mi.execute(pcmd.SampleShutter(pcmd.PascalState("OFF")))

        original_mask_position = None
        if move_mask_to_block_position:
            original_mask_position = await self.get_current_mask_position()
            await self.move_mask_to_position(self.pld_config.mask_block_position)

        await self.set_target(target_id, "AUTO", "AUTO")

        before = await self._log_values()
        if not is_dryrun:
            await self.chamber_mi.execute(pcmd.TriggerLaser(num_pulse=num_pulse, frequency=frequency, sync=False, nowait=False))
        after = await self._log_values()

        if before["MissedPuls"] != after["MissedPuls"]:
            raise RuntimeError("laser missed pulses during preablation -- check the laser")

        if original_mask_position is not None:
            await self.move_mask_to_position(original_mask_position)

    async def _warm_up_to_pid_limit(self) -> bool:
        b = self.bounds
        await self.chamber_mi.execute(pcmd.TemperatureControl(mode=pcmd.TemperatureControlModeType.MANUAL))

        values = await self._log_values()
        all_configs = await self.chamber_config.get_all_config()
        ld_min = float(all_configs.configs["PIDsettings"]["LDmin"])
        starting_point = float(values["HT set"])

        if starting_point > ld_min:
            log.warning("warm-up starting point %.2f is above the PID limit %.2f; skipping", starting_point, ld_min)
            return False

        current = starting_point + b.warm_up_step
        while current <= ld_min + b.warm_up_step * 0.5:
            await self.chamber_mi.execute(pcmd.SetHeatingCurrent(current=float(current)))
            await asyncio.sleep(b.warm_up_step / b.warm_up_current_ramp_rate)
            current += b.warm_up_step

        start_time = time.time()
        current_temperature = await self.get_current_temperature()
        while time.time() - start_time < b.warm_up_max_waittime:
            current_temperature = await self.get_current_temperature()
            if current_temperature > b.temperature_pid_engage_threshold:
                break
            await asyncio.sleep(b.warm_up_wait_interval)

        if current_temperature < b.temperature_pid_engage_threshold:
            raise RuntimeError(
                f"temperature stalled at {current_temperature}C, below the "
                f"{b.temperature_pid_engage_threshold}C PID-engage threshold -- check the heater"
            )
        return True

    async def is_heating_laser_on(self) -> bool:
        """`Heat Stat` bit 3, the power supply's own ON/OFF monitor -- what the heater
        is actually doing, not what it was last told."""
        values = await self._log_values()
        return _truthy(values["ON/OFF monitor in PS"])

    async def to_temperature(self, temperature: float, ramp_rate: float = 20.0) -> float:
        b = self.bounds
        temperature = max(b.temperature_min, min(b.temperature_max, temperature))

        # With the laser off, PID has nothing to drive: the setpoint climbs, the diode
        # current stays at zero and the pyrometer sits at Pyro_min. The failure is
        # silent on the log and, because TemperatureSet is issued nowait=False, shows
        # up only as an MI command that blocks until it times out. Refuse up front.
        if not await self.is_heating_laser_on():
            raise RuntimeError(
                "heating laser is off -- call initiate_heating_laser first; ramping "
                "with it off sets a setpoint no current can reach"
            )

        current_temperature = await self.get_current_temperature()
        if current_temperature < b.temperature_pid_engage_threshold <= temperature:
            await asyncio.sleep(2)  # let the log catch up before warming
            await self._warm_up_to_pid_limit()

        await self.chamber_mi.execute(pcmd.TemperatureControl(mode=pcmd.TemperatureControlModeType.PID))
        await self.chamber_mi.execute(pcmd.TemperatureRamp(ramp_rate, state=pcmd.PascalState("ON")))
        await self.chamber_mi.execute(pcmd.TemperatureSet(temperature, nowait=False))
        return temperature

    async def cool_down(self, ramp_rate: float = 20.0) -> None:
        await self.chamber_mi.execute(pcmd.TemperatureRamp(ramp_rate, state=pcmd.PascalState("ON")))
        await self.chamber_mi.execute(pcmd.TemperatureSet(self.bounds.temperature_min, nowait=False))

    async def perform_deposition(self, num_pulse: int, laser_repetition_rate: float, target_id: str, is_dryrun: bool = False) -> None:
        await self.set_target(target_id, "AUTO", "AUTO")
        await self.chamber_mi.execute(pcmd.SampleShutter(pcmd.PascalState("ON")))
        if not is_dryrun:
            await self.chamber_mi.execute(pcmd.TriggerLaser(num_pulse=num_pulse, frequency=laser_repetition_rate, sync=False, nowait=False))

    async def anneal(self, steps: list[tuple[float, float, float]]) -> None:
        for temperature, ramp_rate, wait_time in steps:
            await self.to_temperature(temperature, ramp_rate=ramp_rate)
            if wait_time > 0:
                await count_down(wait_time, prefix="annealing")

    # --- storage / provenance ----------------------------------------------------

    async def start_storage(self, project_name: str, is_dryrun: bool = False) -> dict:
        record_uuid = str(uuid.uuid4())
        storage_name = f"{project_name}_{record_uuid}"
        if is_dryrun:
            return {"ok": True, "record_uuid": record_uuid, "storage_name": storage_name, "path": None, "message": ""}

        status = await self.storage.start_recording(StorageRequest(
            project_name=storage_name, save_frame=True, save_ai=True, save_log=True, force_rewrite=False,
        ))
        return {
            "ok": status.ok, "record_uuid": record_uuid, "storage_name": storage_name,
            "path": status.path, "message": status.message,
        }

    async def end_storage(self, is_dryrun: bool = False) -> dict:
        if is_dryrun:
            return {"ok": True, "message": ""}
        status = await self.storage.stop_recording()
        return {"ok": status.ok, "message": status.message}

    async def finish_experiment_record(
        self, *, substrate_id: int, project_id: int, experiment_uuid: str, temperature: float,
        pressure: float, laser_power: float, laser_repetition_rate: float, target_material: str,
        num_pulse: int, is_pixel: bool = False, pixel_index: int | None = None,
        storage_name: str | None = None, record_uuid: str | None = None,
    ) -> int:
        experiment_id = await self.growth_db.add_experiment(
            substrate_id=substrate_id, is_pixel=is_pixel, pixel_location=pixel_index,
            experiment_uuid=experiment_uuid, project_id=project_id, temperature=temperature,
            pressure=pressure, laser_power=laser_power, laser_pulse_rate=laser_repetition_rate,
            target_material=target_material, num_pulse=num_pulse,
        )
        if storage_name is not None:
            await self.growth_db.add_recording(
                experiment_id=experiment_id, record_name=storage_name, record_uuid=record_uuid or "",
            )
        return experiment_id

    # --- gated: laser power --------------------------------------------------------

    async def begin_set_laser_power(self, laser_power: float, target_id: str | None = None, force: bool = False) -> tuple[bool, float | None]:
        """Returns (already_satisfied, cached_measured_power). If already_satisfied,
        no confirmation is needed -- the caller should skip straight to the cached
        value rather than opening a pending gate for nothing."""
        if not force and self._laser_power_set == laser_power:
            return True, self._laser_power_real

        await self.chamber_mi.execute(pcmd.SampleShutter(pcmd.PascalState("OFF")))
        if target_id is not None:
            await self.set_target(target_id, "ON", "ON")
        else:
            await self.chamber_mi.execute(pcmd.SelectTarget(pcmd.Targets("Clear"), nowait=False))
        # Stashed for confirm_laser_power, which the caller invokes with only the
        # measured value -- it has no other way to know what this begin() targeted.
        self._pending_laser_target_id = target_id
        self._pending_laser_power = laser_power
        return False, None

    async def confirm_laser_power(self, measured_power: float) -> float:
        if self._pending_laser_target_id is not None:
            await self.set_target(self._pending_laser_target_id, "AUTO", "AUTO")
        self._laser_power_set = self._pending_laser_power
        self._laser_power_real = measured_power
        return measured_power

    # --- gated: mask-center calibration --------------------------------------------

    async def begin_align_center_mask(self) -> None:
        await self.chamber_mi.execute(pcmd.SetRHEEDGunX(0))
        await self.chamber_mi.execute(pcmd.SetMaskPosition(mask_id=pcmd.MaskID.M1, distance=self.pld_config.center_mask_pos, sync=False, nowait=False))

    async def confirm_center_mask(self, position: float) -> None:
        self.pld_config.center_mask_pos = position

    # --- gated: mask-center check loop ----------------------------------------------

    async def begin_check_mask_center(self) -> None:
        await self.chamber_mi.execute(pcmd.SelectTarget(pcmd.Targets.Clear, nowait=False))
        await self.chamber_mi.execute(pcmd.SampleShutter(pcmd.PascalState("ON")))
        await self.chamber_mi.execute(pcmd.SetMaskPosition(mask_id=pcmd.MaskID.M1, distance=self.pld_config.center_mask_pos, sync=False, nowait=False))

    async def confirm_mask_center(self, aligned: bool, corrected_position: float | None = None) -> bool:
        """Returns True if still pending (not yet aligned)."""
        if aligned:
            return False

        if corrected_position is not None:
            self.pld_config.center_mask_pos = corrected_position
        # Retract-then-reapproach: a move of only a few mm can leave the mask motor
        # stuck, so back off first rather than nudging directly to the new position.
        await self.chamber_mi.execute(pcmd.SetMaskPosition(
            mask_id=pcmd.MaskID.M1, distance=max(self.pld_config.center_mask_pos - 5, 0), sync=False, nowait=False,
        ))
        await self.chamber_mi.execute(pcmd.SetMaskPosition(
            mask_id=pcmd.MaskID.M1, distance=self.pld_config.center_mask_pos, sync=False, nowait=False,
        ))
        return True

    # --- gated: RHEED gain tuning ------------------------------------------------

    async def begin_adjust_rheed_gain(self):
        return await self.rheed_camera.get_camera_config()

    async def set_rheed_gain(self, gain: float) -> None:
        from lumi.contracts.payloads.camera import CameraConfig
        await self.rheed_camera.update_camera_config(CameraConfig(gain=gain))

    async def confirm_rheed_gain(self) -> None:
        return None

    # --- gated: per-pixel keep/drop ---------------------------------------------

    async def _pixel_check_status(self, substrate: Substrate, index: int) -> dict:
        position = substrate.positions[index]
        mask_position = self._pixel_to_mask_position(position)
        rheed_position = self._pixel_to_rheed_position(position)
        await self.move_mask_to_position(mask_position)
        await self.move_rheed_to_position(rheed_position)
        return {
            "done": False, "index": index, "position": position,
            "mask_position": mask_position, "rheed_position": rheed_position, "dropped": [],
        }

    async def begin_check_rheed_pixels(self) -> dict:
        substrate = self.current_substrate
        if substrate is None or not substrate.positions:
            return {"done": True, "index": None, "position": None, "mask_position": None, "rheed_position": None, "dropped": []}
        self._pixel_check_index = 0
        self._pixel_check_dropped: list[int] = []
        return await self._pixel_check_status(substrate, 0)

    async def resolve_pixel_check(self, keep: bool) -> dict:
        substrate = self.current_substrate
        index = self._pixel_check_index
        dropped = self._pixel_check_dropped
        if not keep:
            dropped.append(index)

        next_index = index + 1
        if substrate is None or next_index >= len(substrate.positions):
            if dropped:
                to_drop = [substrate.positions[i] for i in dropped]
                substrate.drop_positions(to_drop)
            return {"done": True, "index": None, "position": None, "mask_position": None, "rheed_position": None, "dropped": dropped}

        self._pixel_check_index = next_index
        self._pixel_check_dropped = dropped
        return await self._pixel_check_status(substrate, next_index)


class SingleDepoExperimentManager(BaseExperimentManager):
    """One substrate, one active position, fixed recipe per call."""

    def is_substrate_available(self) -> bool:
        substrate = self.current_substrate
        return substrate is not None and len(substrate.accessible_positions) > 0

    def get_current_position(self) -> tuple[bool, float | None]:
        if self.current_substrate is None:
            return False, None
        return self.current_substrate.get_current_position()


class PixelExperimentManager(BaseExperimentManager):
    """Multi-pixel substrate: growths at distinct positions on the same substrate."""

    def get_current_position(self) -> tuple[bool, float | None]:
        if self.current_substrate is None:
            return False, None
        return self.current_substrate.get_current_position()

    async def to_pixel(self, index: int) -> tuple[float, float] | None:
        substrate = self.current_substrate
        if substrate is None or index >= len(substrate.positions):
            return None
        position = substrate.positions[index]
        mask_position = self._pixel_to_mask_position(position)
        rheed_position = self._pixel_to_rheed_position(position)
        await self.move_mask_to_position(mask_position)
        await self.move_rheed_to_position(rheed_position)
        return mask_position, rheed_position

    async def to_current_pixel(self) -> tuple[float, float] | None:
        substrate = self.current_substrate
        if substrate is None:
            return None
        available, position = substrate.get_current_position()
        if not available:
            return None
        mask_position = self._pixel_to_mask_position(position)
        rheed_position = self._pixel_to_rheed_position(position)
        await self.move_mask_to_position(mask_position)
        await self.move_rheed_to_position(rheed_position)
        return mask_position, rheed_position
