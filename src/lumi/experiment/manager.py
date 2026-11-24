"""The PLD experiment drivers: substrate/pixel bookkeeping, chamber reads, and
hardware control.

Ported from ~/Projects/OperationsNotebooks/src/manager.py's BaseExperimentManager /
SingleDepoExperimentManager / PixelExperimentManager, adapted to run inside a node
process rather than a notebook kernel:

- The chamber is a backend (`lumi.experiment.chamber`): `PldChamber`, pld-service
  through `chamber.pld` (the default), or `MiChamber`, PASCAL's MI mode and chamber log
  (`--use-legacy-mi-mode`). The manager says what to do -- select a target, ramp to a
  temperature, fire -- and reads the chamber as a decoded log row either way.

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
from datetime import date
from math import floor

from lumi.contracts.payloads.chamber import SectionQuery
from lumi.contracts.payloads.experiment import MaskAlignPass, MaskAlignResult, MaskSweepPoint
from lumi.contracts.payloads.fiducial import MASK_CENTER_ROLE
from lumi.contracts.payloads.storage import RecordingEventIn, StorageRequest
from lumi.experiment.db import GrowthDB, column as _column, to_epoch, to_iso, utc_now
from lumi.experiment.mask_align import locate_slit
from lumi.experiment import sequence as seqlib
from lumi.experiment.chamber import ChamberBackend
from lumi.experiment.rheed_gun import RheedGun, RheedGunSettings, frame_intensity
from lumi.generated.clients.chamber import ChamberConfigClient, ChamberFiducialClient, ChamberLogClient
from lumi.generated.clients.rheed import RheedCameraClient
from lumi.generated.clients.storage import StorageStorageClient
from lumi.pascal import command as pcmd

log = logging.getLogger(__name__)



def _preablation_fields(preablation: tuple[int, float] | None) -> dict:
    """add_experiment's pre-ablation columns. The frequency column is an INTEGER."""
    if preablation is None:
        return {}
    pulses, hz = preablation
    return {"do_preablation": True, "preablation_pulse": int(pulses), "preablation_frequency": round(hz)}

class SequenceLost(RuntimeError):
    """Lumi lost track of a sequence run: pld-service stopped answering or restarted."""

#: The daily laser test: the excimer in HV mode at this HV, fired into the energy meter
#: at Clear. What reaches the meter per mJ the laser reports is what a deposition's
#: excimer energy is worked out from (begin_set_laser_power).
LASER_TEST_KV = 21.0
LASER_TEST_PULSES = 100
LASER_TEST_RATE_HZ = 1.0
#: How often fire_laser_test reads the excimer's energy while the train runs, s.
LASER_TEST_POLL_S = 1.0
#: Fewer readings than this, or readings that never move by more than
#: LASER_TEST_FLAT_MJ, are not taken as the source energy (see `source_from_readings`).
LASER_TEST_MIN_READINGS = 3
LASER_TEST_FLAT_MJ = 0.05
#: A meter reading this far above the source is a mistyped or swapped number: the
#: beam only loses energy between the laser and the meter.
LASER_TEST_MAX_RATIO = 1.1


def source_from_readings(readings: list[tuple[float, float | None]]) -> tuple[float | None, str]:
    """The source energy from the excimer's (energy, energy setpoint) readings during
    the test train, and why, if it is None.

    A real excimer's energy moves from pulse to pulse. Readings that do not move at all
    are not a measurement: a laser (or a twin) answering with its setpoint, or a stale
    value. Then the person reads the laser's display and passes source_mj."""
    if len(readings) < LASER_TEST_MIN_READINGS:
        return None, (f"the excimer reported its energy {len(readings)} time(s) while firing, "
                      f"fewer than {LASER_TEST_MIN_READINGS}")
    energies = [e for e, _ in readings]
    if max(energies) - min(energies) < LASER_TEST_FLAT_MJ:
        setpoints = {s for _, s in readings}
        echo = (len(setpoints) == 1 and None not in setpoints
                and abs(energies[0] - setpoints.pop()) < LASER_TEST_FLAT_MJ)
        return None, (f"the excimer's energy stayed at {energies[0]:.2f} mJ for all {len(readings)} "
                      f"readings{', its energy setpoint' if echo else ''}: that is not a measurement")
    return sum(energies) / len(energies), ""


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


def _log_entry_stamp(entry) -> str:
    """A change-detectable timestamp for a chamber-log row.

    The parsed clock is always in `values` (`process_row` writes `time`,
    `time_stamp` and `Time` there). The top-level `LogEntry.time` / `.time_stamp`
    are also populated now, but a chamber node that predates that fix still sends
    0.0 / "" -- so read `values` first and fall back to the top-level fields,
    rather than depending on both nodes being on the same build.
    """
    values = getattr(entry, "values", None) or {}
    for key in ("time", "time_stamp", "Time"):
        v = values.get(key)
        if v not in (None, "", 0, 0.0):
            return str(v)
    ts = getattr(entry, "time_stamp", "") or ""
    if ts:
        return ts
    t = getattr(entry, "time", 0.0) or 0.0
    return str(t) if t else ""


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
    # The heating current the manual warm-up from cold raises to before PID takes
    # over: enough to clear temperature_pid_engage_threshold, and no more, since the
    # warm-up runs open-loop and anything past the threshold is overshoot.
    warm_up_current: float
    warm_up_step: float
    warm_up_current_ramp_rate: float
    warm_up_wait_interval: float
    warm_up_max_waittime: float
    # How long to wait for the motor holding lock to re-engage before a commanded
    # move. Bounded so a chamber left on manual/unpowered raises instead of hanging.
    motor_ready_timeout: float = 60.0
    # The hottest the substrate may read when the heating laser is turned off. Cutting
    # the laser hot drops the sample from setpoint at once, the rapid cooling a ramp is
    # there to avoid. 160 degC is the pyrometer's floor, so this is "cooled down".
    heating_laser_off_max_temperature: float = 165.0


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
        state: str = "active",
        created_at: str | None = None,
    ) -> None:
        self.state = state
        #: As stored -- CURRENT_TIMESTAMP text. `to_epoch`/`to_iso` render it for the
        #: wire; None means the row predates the column.
        self.created_at = created_at
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
            return [0.0]
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

    def reopen_position(self, index: int) -> None:
        """The inverse of finish_current_position: put a retired position back in play.

        `current_position_id` moves to the reopened index rather than merely
        decrementing, so reopening something other than the most recent finish still
        leaves the substrate pointing at the position that is now next to grow.
        """
        self._accessed_position_ids.discard(index)
        self.current_position_id = min(self.current_position_id, index)

    @property
    def last_finished_position(self) -> int | None:
        return max(self._accessed_position_ids) if self._accessed_position_ids else None

    def reset_progress(self) -> None:
        self._accessed_position_ids = set()
        self.current_position_id = 0

    def restore_progress(self, used_indices: set[int]) -> None:
        """Re-apply growth history to a substrate rebuilt from the database.

        A fresh Substrate starts at position 0 with nothing accessed, which is correct
        for `register_substrate` and wrong for `resume_substrate` -- see
        GrowthDB.get_used_pixel_indices. `current_position_id` lands on the first index
        that has not been grown on, so `get_current_position` and
        `accessible_positions` agree with each other and with the history.
        """
        self._accessed_position_ids = {i for i in used_indices if 0 <= i < len(self.positions)}
        self.current_position_id = next(
            (i for i in range(len(self.positions)) if i not in self._accessed_position_ids),
            len(self.positions),
        )


class BaseExperimentManager:
    """Shared hardware/DB primitives for driving a PLD growth."""

    def __init__(
        self,
        *,
        chamber: ChamberBackend,
        chamber_log: ChamberLogClient,
        chamber_config: ChamberConfigClient,
        rheed_camera: RheedCameraClient,
        storage: StorageStorageClient,
        growth_db: GrowthDB,
        pld_config: PLDChamberConfiguration,
        bounds: ExperimentBounds,
        target_mapper: dict[str, str],
        chamber_fiducial: ChamberFiducialClient | None = None,
        rheed_gun: RheedGunSettings | None = None,
    ) -> None:
        #: The driver: `PldChamber` (pld-service, the default) or `MiChamber` (legacy MI mode).
        self.chamber = chamber
        #: The RHEED gun at the lab's operating values; pld-service only.
        self.rheed_gun = RheedGun(chamber, rheed_gun or RheedGunSettings(),
                                  intensity=self._rheed_intensity)
        # Only mask-centre auto-alignment reads it; None leaves everything else working.
        self.chamber_fiducial = chamber_fiducial
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
        #: The recording this driver started and has not ended, or None.
        self.recording_name: str | None = None
        # Stashed between begin_set_laser_power() and confirm_laser_power().
        self._pending_laser_power: float | None = None
        #: The excimer energy and meter/source ratio begin_set_laser_power set the
        #: present beam from; None when the beam was not set that way.
        self._laser_source_energy: float | None = None
        self._laser_meter_ratio: float | None = None
        # Between begin_laser_test() and confirm_laser_test(): who fired, and the
        # excimer's energy readings while Lumi fired.
        self._laser_test: dict | None = None
        # Stashed between begin_check_rheed_pixels() and resolve_pixel_check().
        self._pixel_check_index: int = 0
        self._pixel_check_dropped: list[int] = []
        #: Pre-ablations run and not yet written to a growth record, by target material:
        #: (pulses, Hz). The next record of that material says it was pre-ablated.
        self._preablations: dict[str, tuple[int, float]] = {}

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
            created_at=utc_now(),
        )
        self._drop_positions_by_rheed_limit(substrate)

        substrate.db_id = await self.growth_db.add_substrate(
            substrate.materials, substrate.orientation, substrate.width, substrate.height,
            substrate.thickness, substrate.pixel_spacing, json.dumps(substrate.positions),
            substrate_uuid=substrate.uuid, manufacture=manufacturer,
            manufacture_date=manufacture_date, substrate_name=substrate_name,
            created_at=substrate.created_at,
        )
        await self._materialise_samples(substrate)
        self.substrates.append(substrate)
        return substrate

    async def _materialise_samples(self, substrate: Substrate, root_exists: bool = False) -> None:
        """One `sample` row per growable position, created with the substrate.

        A position becomes a specimen the moment the substrate is registered, not when
        it is first grown on -- that is what lets "which positions are spent" be a
        query instead of in-memory state, and what gives a step somewhere to attach
        before any deposition has happened.

        `root_exists` is the re-materialise case (a geometry correction): the root row
        carries the substrate's uuid and outlives the positions hanging off it.
        """
        root = None
        if root_exists:
            existing = await self.growth_db.get_samples_for_substrate(substrate.db_id)
            root = next((r[0] for r in existing if r[4] == "substrate"), None)
        if root is None:
            root = await self.growth_db.add_sample(
                substrate.db_id, kind="substrate", pixel_index=None,
                sample_name=substrate.name, state="active", sample_uuid=substrate.uuid,
            )
        base = substrate.name or substrate.materials
        for index, position in enumerate(substrate.positions):
            await self.growth_db.add_sample(
                substrate.db_id, kind="position", pixel_index=index,
                position_mm=position, parent_sample_id=root,
                sample_name=f"{base}-p{index}",
            )

    async def resume_substrate(self, substrate_id: int) -> Substrate:
        row = await self.growth_db.get_substrate(substrate_id)
        if row is None:
            raise ValueError(f"no substrate with id {substrate_id}")
        substrate = Substrate(
            materials=row[2], orientation=row[3], width=row[5], height=row[4], thickness=row[6],
            pixel_spacing=row[7], positions=json.loads(row[8]) if row[8] else None,
            db_id=row[0], substrate_uuid=row[1], manufacture=row[9], manufacture_date=row[10],
            name=row[11] if len(row) > 11 else None,
            state=_column(row, "state") or "active",
            created_at=_column(row, "substrate_created_at"),
        )
        substrate.restore_progress(await self.growth_db.get_used_pixel_indices(substrate_id))
        # A substrate registered before the sample table existed has no rows; make
        # them now so resuming an old campaign journals against a real sample rather
        # than silently against None.
        if not await self.growth_db.get_samples_for_substrate(substrate_id):
            await self._materialise_samples(substrate)
        await self._mark_grown_samples(substrate)
        self.substrates.append(substrate)
        return substrate

    async def _mark_grown_samples(self, substrate: Substrate) -> None:
        for index in substrate._accessed_position_ids:
            row = await self.growth_db.find_sample(substrate.db_id, index)
            if row is not None:
                await self.growth_db.set_sample_state(row[0], "grown")

    async def finish_substrate(self) -> None:
        await self._finish_position()

    async def finish_current_pixel(self) -> None:
        await self._finish_position()

    async def _finish_position(self) -> None:
        """Retire the current position and mark its sample grown.

        Both ops did the same thing already; the only difference was which contract op
        the caller reached for. Sharing one implementation means the sample state can
        never be updated on one path and not the other.
        """
        substrate = self.current_substrate
        if substrate is None:
            return
        index = substrate.current_position_id
        substrate.finish_current_position()
        if substrate.db_id is None:
            return
        row = await self.growth_db.find_sample(substrate.db_id, index)
        if row is not None:
            await self.growth_db.set_sample_state(row[0], "grown")

    # --- corrections ---------------------------------------------------------
    # Registration used to be write-once: a substrate typed in with the wrong size,
    # or a position finished by accident, had no way back and the only way to keep
    # depositing was to register a second, fictional substrate. These are the undo
    # paths. They are deliberately conservative -- each one refuses rather than
    # rewriting anything a real growth already points at.

    #: `update_substrate` fields that change nothing but what the record *says*.
    _DESCRIPTIVE_FIELDS = ("materials", "orientation", "thickness", "substrate_name",
                           "manufacturer", "manufacture_date")
    #: ...and the ones that re-derive `positions`, and with it every pixel index.
    _GEOMETRY_FIELDS = ("width", "height", "pixel_spacing", "positions")
    #: Request field -> (Substrate attribute, substrate column), for the three names
    #: that differ across the three layers. Everything else is spelled the same.
    _FIELD_ALIASES = {
        "substrate_name": ("name", "substrate_name"),
        "manufacturer": ("manufacture", "manufacture"),
    }

    def _substrate_by_id(self, substrate_id: int) -> Substrate | None:
        """The loaded Substrate for an id, if this manager happens to hold one.

        An edit is a database write first; keeping the in-memory copy in step matters
        only when the substrate being corrected is one that is currently loaded --
        which, for the mistyped-size case, it almost always is.
        """
        return next((s for s in self.substrates if s.db_id == substrate_id), None)

    async def update_substrate(self, substrate_id: int | None = None, **fields) -> Substrate:
        """Correct a registered substrate's record.

        Descriptive fields are always editable. Geometry is only editable while the
        substrate has no `experiment` rows: `width`/`pixel_spacing` re-derive
        `positions`, so applying them to a substrate that has been grown on would
        renumber pixel indices out from under the growths that already reference them.
        Rather than silently keeping stale positions, that case raises and says so.

        `substrate_id=None` means the current substrate, which is the common case --
        you notice the size is wrong on the one you just registered.
        """
        if substrate_id is None:
            current = self.current_substrate
            if current is None or current.db_id is None:
                raise ValueError("no current substrate to update; pass substrate_id")
            substrate_id = current.db_id

        row = await self.growth_db.get_substrate(substrate_id)
        if row is None:
            raise ValueError(f"no substrate with id {substrate_id}")

        fields = {k: v for k, v in fields.items() if v is not None}
        unknown = set(fields) - set(self._DESCRIPTIVE_FIELDS) - set(self._GEOMETRY_FIELDS)
        if unknown:
            raise ValueError(f"unknown substrate field(s): {sorted(unknown)}")

        geometry = {k: v for k, v in fields.items() if k in self._GEOMETRY_FIELDS}
        if geometry:
            grown = await self.growth_db.count_substrate_experiments(substrate_id)
            if grown:
                raise ValueError(
                    f"substrate {substrate_id} has {grown} growth(s) recorded, so "
                    f"{sorted(geometry)} cannot be changed -- the positions would be "
                    "renumbered under experiments that already reference them. "
                    "Register a new substrate for the corrected geometry; the "
                    "descriptive fields "
                    f"({', '.join(self._DESCRIPTIVE_FIELDS)}) are still editable."
                )

        # Rebuild off the stored row so untouched fields keep their values, then let
        # Substrate re-derive positions exactly the way registration would have.
        substrate = self._substrate_by_id(substrate_id) or await self._load_substrate(row)
        old_name, old_base = substrate.name, substrate.name or substrate.materials
        columns = {}
        for name, value in fields.items():
            attribute, column = self._FIELD_ALIASES.get(name, (name, name))
            setattr(substrate, attribute, value)
            columns[column] = value
        if geometry:
            substrate.positions = (
                list(geometry["positions"]) if geometry.get("positions")
                else substrate._compute_positions()
            )
            self._drop_positions_by_rheed_limit(substrate)
            # No experiments exist (checked above), so the per-position sample rows are
            # the placeholders registration created. Replace them to match the new
            # geometry, and put the substrate back at position 0 -- any 'grown' state
            # on them was an accidental finish, which this edit also undoes.
            substrate.reset_progress()
            await self.growth_db.delete_position_samples(substrate_id)
            await self._materialise_samples(substrate, root_exists=True)
            columns["positions"] = json.dumps(substrate.positions)

        await self.growth_db.update_substrate(substrate_id, **columns)
        await self._rename_samples(substrate, old_name, old_base)
        log.info("substrate %s updated: %s", substrate_id, sorted(fields))
        return substrate

    async def _rename_samples(self, substrate: Substrate, old_name: str | None,
                              old_base: str) -> None:
        """Carry a new substrate name (or, unnamed, new materials) onto its samples.

        Registration names the root sample after the substrate and each position
        `<name>-p<index>`, so renaming only the substrate row left samples, layer
        stacks and journal lookups under the old name. Only names still in the form
        registration gave them are rewritten; a sample someone renamed by hand keeps
        its name.
        """
        new_base = substrate.name or substrate.materials
        for r in await self.growth_db.get_samples_for_substrate(substrate.db_id):
            sample_id, kind, index, name = r[0], r[4], r[5], r[7]
            if kind == "substrate":
                if name == old_name and name != substrate.name:
                    await self.growth_db.update_row("sample", sample_id, sample_name=substrate.name)
            elif index is not None and name == f"{old_base}-p{index}" and old_base != new_base:
                await self.growth_db.update_row("sample", sample_id, sample_name=f"{new_base}-p{index}")

    async def _load_substrate(self, row) -> Substrate:
        """A Substrate rebuilt from a database row, without pushing it onto the stack.
        `resume_substrate` is the loading path; this is for editing a substrate that
        is not the one on the chamber."""
        return Substrate(
            materials=row[2], orientation=row[3], width=row[5], height=row[4], thickness=row[6],
            pixel_spacing=row[7], positions=json.loads(row[8]) if row[8] else None,
            db_id=row[0], substrate_uuid=row[1], manufacture=row[9], manufacture_date=row[10],
            name=row[11] if len(row) > 11 else None,
            state=_column(row, "state") or "active",
            created_at=_column(row, "substrate_created_at"),
        )

    async def reopen_position(self, index: int | None = None, force: bool = False) -> int:
        """Undo a `finish_substrate` / `finish_current_pixel` that was not meant.

        `index=None` reopens the most recently finished position. A position with an
        `experiment` row really was deposited on, so reopening it would hand back a
        spent pixel -- that refuses unless `force`, and even forced it does not survive
        a `resume_substrate`, which rebuilds progress from the experiment table.
        """
        substrate = self.current_substrate
        if substrate is None:
            raise ValueError("no current substrate")
        if index is None:
            index = substrate.last_finished_position
            if index is None:
                raise ValueError("no finished position to reopen")
        if not 0 <= index < len(substrate.positions):
            raise ValueError(
                f"position {index} is outside this substrate's 0..{len(substrate.positions) - 1}"
            )

        if substrate.db_id is not None and not force:
            used = await self.growth_db.get_used_pixel_indices(substrate.db_id)
            if index in used:
                raise ValueError(
                    f"position {index} has a recorded growth, so reopening it would "
                    "hand back a spent pixel. Pass force=True if the experiment row "
                    "is itself the mistake."
                )

        substrate.reopen_position(index)
        if substrate.db_id is not None:
            row = await self.growth_db.find_sample(substrate.db_id, index)
            if row is not None:
                await self.growth_db.set_sample_state(row[0], "planned")
        log.info("reopened position %s on substrate %s", index, substrate.db_id)
        return index

    async def list_substrates(self, **query) -> list[dict]:
        """Every registered substrate with enough to pick one out: what it is, how
        many positions it has and how many are spent. `resume_substrate` takes an id
        and nothing else offered a way to learn one.

        `query` is the shared list shape -- filters/since/until/limit/offset/order/
        search/include_retired -- passed straight through to GrowthDB.list_rows.
        """
        summaries = []
        for row in await self.growth_db.list_rows("substrate", **query):
            positions = json.loads(row["positions"]) if row["positions"] else []
            used = await self.growth_db.get_used_pixel_indices(row["substrate_id"])
            created = _column(row, "substrate_created_at")
            summaries.append({
                "substrate_id": row["substrate_id"], "substrate_uuid": row["substrate_uuid"],
                "materials": row["materials"], "orientation": row["orientation"],
                "width": row["width"], "pixel_spacing": row["pixel_spacing"],
                "substrate_name": _column(row, "substrate_name"),
                "num_positions": len(positions),
                "num_used": len({i for i in used if 0 <= i < len(positions)}),
                "state": _column(row, "state") or "active",
                "created_at": to_epoch(created), "created_at_iso": to_iso(created),
            })
        return summaries

    async def retire_substrate(self, substrate_id: int, retire: bool = True) -> Substrate:
        """Hide a substrate from listings without destroying it.

        This is the delete that isn't one. A substrate registered by mistake stops
        cluttering `list_substrates`, but whatever was journalled against it stays
        readable -- deleting the row would orphan steps and samples that point at it,
        and there is no restore path for growth.db. Reversible: `retire=False`.
        """
        row = await self.growth_db.get_substrate(substrate_id)
        if row is None:
            raise ValueError(f"no substrate with id {substrate_id}")
        state = "retired" if retire else "active"
        await self.growth_db.update_substrate(substrate_id, state=state)
        substrate = self._substrate_by_id(substrate_id)
        if substrate is None:
            substrate = await self._load_substrate(row)
        substrate.state = state
        log.info("substrate %s is now %s", substrate_id, state)
        return substrate

    def unload_substrate(self) -> Substrate | None:
        """Take the current substrate off the chamber, leaving the database alone.

        `register_substrate` and `resume_substrate` push onto a stack, so loading the
        wrong one used to be unfixable without restarting the node. Returns whatever
        is current afterwards -- the one loaded before it, or None.
        """
        if self.substrates:
            dropped = self.substrates.pop()
            log.info("unloaded substrate %s", dropped.db_id)
        return self.current_substrate

    async def target_names(self) -> dict[str, str]:
        """The material in each carousel slot (A..F, Clear, Monitor), "" for an empty
        slot. Under the pld driver it is pld-service's own `[target_names]`
        (GetSettings), the live list the PLD GUI edits; only legacy MI mode still reads
        PLD Controller's PLDconfig.ini (TGsettings) through chamber.config."""
        if self.chamber.driver == "pld":
            names = (await self.chamber.client.get_settings()).target_names()
            names.update({"Clear": "Clear", "Monitor": "Monitor"})
            return {slot: names.get(slot, "") for slot in self.target_mapper}
        section = await self.chamber_config.get_configs_by_section(SectionQuery(section="TGsettings"))
        return {slot: section.values.get(field) or "" for slot, field in self.target_mapper.items()}

    async def get_target_name_by_id(self, target_id: str) -> str:
        if target_id not in self.target_mapper:
            raise ValueError(f"target {target_id!r} not in target_mapper: {sorted(self.target_mapper)}")
        name = (await self.target_names()).get(target_id, "")
        if not name:
            raise ValueError(f"slot {target_id} holds no target (pld-service's target_names)")
        return name

    async def get_target_id_by_name(self, target_name: str) -> str:
        names = await self.target_names()
        for slot, name in names.items():
            if name and name.strip().casefold() == target_name.strip().casefold():
                return slot
        loaded = {slot: name for slot, name in names.items() if name and slot not in ("Clear", "Monitor")}
        raise ValueError(f"no target {target_name!r} in the chamber; loaded: {loaded}")

    async def get_current_target(self) -> tuple[int, str | None, str | None]:
        """The carousel position the chamber log reports, as (TG No., slot, name).

        `TG No.` counts from 0 in slot order: 0 is A (TG1name), 6 is Clear (TG7name),
        7 is Monitor (TG8name). The slot is None for a number no `target_mapper`
        entry maps to; the name is the material in that slot (`target_names`)."""
        number = int(float((await self._log_values())["TG No."]))
        slot = next(
            (s for s, field in self.target_mapper.items()
             if str(field).removeprefix("TG").removesuffix("name") == str(number + 1)),
            None,
        )
        name = (await self.target_names()).get(slot) or None if slot is not None else None
        return number, slot, name

    async def show_available_targets(self) -> dict[str, str]:
        return await self.target_names()

    # --- chamber reads ---------------------------------------------------------

    async def _log_values(self) -> dict:
        """The chamber's readings as a decoded log row: pld-service's live state, or the
        newest row of PASCAL's chamber log under legacy MI mode."""
        return await self.chamber.readings()

    async def get_current_log(self):
        batch = await self.chamber_log.log()
        if not batch.entries:
            raise RuntimeError("chamber log has no rows yet")
        return batch.entries[-1]

    async def check_logging_alive(self, timeout_s: float = 5.0, poll: float = 0.25) -> tuple[bool, float, str]:
        """Whether the chamber log's newest row advances within `timeout_s`.

        Read the latest row's timestamp, then re-read until it moves or the timeout
        is up: a timestamp that does not change means the log-reader thread has died
        (a torn CSV row can kill it and freeze every field -- see docs/TODO.md) or
        PASCAL has stopped writing, and every other chamber read is then serving a
        stale row.

        `timeout_s` must exceed the controller's `Log Interval`, which powers up at
        60s -- run `start_mi_logging(interval_s=1)` first, or pass a `timeout_s`
        longer than your interval, or this reports a healthy-but-slow log as dead.
        Returns (alive, waited_s, last_stamp).
        """
        start = time.monotonic()
        before = _log_entry_stamp(await self.get_current_log())
        last = before
        while time.monotonic() - start < timeout_s:
            await asyncio.sleep(poll)
            last = _log_entry_stamp(await self.get_current_log())
            if last != before:
                return True, time.monotonic() - start, last
        return False, time.monotonic() - start, last

    async def is_motor_free(self) -> bool:
        """`Motor Stat` bit 0. "Free" here means the electromagnet holding lock is
        *released* -- the axes are back-driveable by hand and not under servo
        authority (a power cut drops the lock). It is not an "idle" flag: a healthy
        powered chamber reports this clear at rest and while moving alike, which is
        why the recorded idle asset holds `Motor Stat` at 0x0000 throughout. A
        commanded move must only go out while this is False; see `_await_motor_ready`.
        """
        values = await self._log_values()
        return _truthy(values["Motor free"])

    async def get_current_pressure(self, gauge_name: str | None = None) -> tuple[float, str | None]:
        values = await self._log_values()
        if gauge_name is not None:
            if gauge_name not in values:
                raise ValueError(f"gauge {gauge_name!r} not found in the log")
            return float(values[gauge_name]), gauge_name

        # IG1 off (the high regime) reads 0: PASCAL's log prints "0.00E+0", pld-service's
        # state gives 0.0. Either way the capacitance gauge is the reading.
        if float(values["Vac Pres Main"]) == 0.0:
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
            "MV21 (2nd RHEED)", "FV21 (2nd RHEED)", "CV1", "VT1 (L/L N2 vent)",
            "VT2 (L/L air vent)", "CV301",
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
        if mfc_id not in ("1", "2"):
            raise ValueError(f"invalid MFC id: {mfc_id!r}")
        await self.chamber.set_mfc_flow(int(mfc_id), flow)

    async def set_mfc_control(self, enabled: bool) -> None:
        """PASCAL's `MFC Control` -- the master gate every MFC's flow passes through.
        Separate from set_mfc_flow because the command takes no channel argument: it
        is one switch for the whole gas line."""
        await self.chamber.set_mfc_control(enabled)

    async def set_pressure(self, pressure: float) -> None:
        await self.chamber.set_pressure(pressure)

    async def set_pressure_control(self, on: bool) -> None:
        await self.chamber.set_pressure_control(on)

    async def start_mi_logging(self, interval_s: int = 1, file_name: str | None = None) -> tuple[str, int]:
        """Start PASCAL's data logging at a fixed row interval (default every 1s).

        The controller powers up at a 60s `Log Interval`; a growth wants a row a
        second. Issues `Log Interval` then `Data Logging File=<name>`; the log
        reader picks the file up by watching its folder, so the name only has to be
        unique. Returns (file_name, interval_s) actually used.
        """
        interval_s = int(interval_s)
        if interval_s < 1:
            raise ValueError(f"log interval must be a whole number of seconds >= 1, got {interval_s}")
        if not file_name:
            file_name = f"chamber_log_{time.strftime('%Y%m%d_%H%M%S')}.csv"
        await self.chamber.start_logging(file_name, interval_s)
        return file_name, interval_s

    async def stop_mi_logging(self) -> None:
        """`Data Logging OFF`.

        PASCAL will not switch to a new log file while one is already open --
        `start_mi_logging` then acks over MI mode but the file never changes. Call
        this first to close the running logger, then start the one you want.
        """
        await self.chamber.stop_logging()

    async def initiate_heating_laser(self, timeout: float = 30.0, poll: float = 0.5) -> None:
        await self.chamber.heater_on()

        # The MI commands complete when the controller accepts them, but
        # `is_heating_laser_on` reads `Heat Stat` bit 3 from the *chamber log*, which
        # PASCAL rewrites about once a second. Return only once that bit has actually
        # flipped, so a `to_temperature` call right after this one does not read a
        # pre-laser log row and refuse. Bounded like `_await_motor_ready`: a laser that
        # never reports on raises rather than hanging.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await self.is_heating_laser_on():
                return
            await asyncio.sleep(poll)
        raise RuntimeError(f"heating laser did not report on within {timeout:.0f}s of initiate_heating_laser")

    async def turn_off_heating_laser(self, force: bool = False) -> None:
        if not force:
            current = await self.get_current_temperature()
            limit = self.bounds.heating_laser_off_max_temperature
            if current > limit:
                raise RuntimeError(
                    f"the substrate reads {current:.0f}C, above {limit:.0f}C -- turning the "
                    "heating laser off now cools it all at once. cool_down first and wait "
                    "for it to read cold (a to_temperature with wait=false returns before "
                    "the ramp ends); force=true overrides"
                )
        await self.chamber.heater_off()

    async def check_mi_mode(self, timeout: float = 10.0) -> tuple[str, str]:
        """Whether the chamber driver will take commands, as (status, message):
        `on`, `off` or `busy`.

        Under pld-service (the default): whether the service is connected, writable (not
        read-only) and free to be operated (no other client holds control). Under legacy
        MI mode: whether PASCAL runs a no-op script, since nothing in the chamber log
        says whether MI Mode is on (see `MiChamber.check`).
        """
        return await self.chamber.check(timeout)

    async def _await_motor_ready(self, poll: float = 0.5) -> None:
        """Block until the motor holding lock is engaged, so a commanded move has
        somewhere to land.

        `is_motor_free()` is True when the electromagnet lock is *released* -- every
        axis (mask, RHEED-X, target revolution/spin, sample position/rotation) is
        back-driveable by hand and a commanded move would push against nothing (a
        power cut is the usual cause). A move waits for that to clear rather than, as
        this check once did, for it to be set. Every driver primitive that issues an
        axis move calls this first, so the interlock covers raw ops and MCP agents,
        not just the recipe flows.

        Move *sequencing* -- not starting the next axis until the last one arrived --
        is the MI completion wait's job (`nowait=False`), not this gate's. Polling
        rather than a one-shot read only covers the lock taking a log tick or two to
        re-engage after power is restored.

        Re-engaging the lock is a physical MOTOR ENABLE button on the chamber
        controller, so if the axes are found free this logs what the operator has to
        press and then waits, bounded by `bounds.motor_ready_timeout`: a chamber left
        on manual raises rather than hanging, unlike the MI completion wait (see
        docs/TODO.md). An unattended batch can front-run this with the optional
        `recipes.run_ensure_motor_ready`, which asks through an `input_provider`
        instead of only logging.
        """
        timeout = self.bounds.motor_ready_timeout
        deadline = time.monotonic() + timeout
        announced = False
        while time.monotonic() < deadline:
            if not await self.is_motor_free():
                if announced:
                    log.info("motor holding lock re-engaged -- continuing")
                return
            if not announced:
                log.warning(
                    "motor holding lock is released -- press the MOTOR ENABLE button on "
                    "the chamber controller to re-engage it; waiting up to %.0fs", timeout,
                )
                announced = True
            await asyncio.sleep(poll)
        raise RuntimeError(
            f"motor still reports free (holding lock released) after {timeout:.0f}s -- "
            "press the MOTOR ENABLE button on the chamber controller, then retry; "
            "a commanded move cannot drive an unlocked axis"
        )

    async def move_mask_to_position(self, position: float, speed_mm_s: float = 0.0,
                                    accel_mm_s_per_ms: float = 0.0) -> None:
        if not (0 <= position < self.bounds.mask_travel_max):
            raise ValueError(f"mask position must be in [0, {self.bounds.mask_travel_max}), got {position}")
        # The original asserted `not is_motor_free()` here, with the message "Motor is
        # not free". That assertion was right and the later "fix" to require the motor
        # *free* was the bug: "Motor free" is the holding lock released, not an idle
        # flag, so an axis can only be driven while it is NOT free.
        await self._await_motor_ready()
        await self.chamber.move_mask(position, speed_mm_s, accel_mm_s_per_ms)

    async def move_rheed_to_position(self, position: float) -> None:
        lo, hi = min(self.pld_config.rheed_limit), max(self.pld_config.rheed_limit)
        if not (lo <= position <= hi):
            raise ValueError(f"RHEED gun position must be in [{lo}, {hi}], got {position}")
        await self._await_motor_ready()
        await self.chamber.move_rheed_gun(position)

    async def rheed_on(self, judge: str | None = None, **overrides) -> dict:
        """The RHEED gun on at the lab's operating values (`lumi.experiment.rheed_gun`).
        `judge` (None = the setting): "agent" or "person" stop after filament and HV with
        the emission search open, to step (`rheed_step_up`) and end (`rheed_finish`);
        "rule" searches by the emission's digits by itself."""
        if self.chamber.driver != "pld":
            raise RuntimeError("rheed_on needs pld-service: under --use-legacy-mi-mode the gun "
                               "is switched on its own controller, by hand")
        result = await self.rheed_gun.on(judge=judge or self.rheed_gun.settings.filament_judge,
                                         **overrides)
        await self.add_recording_event("rheed_on", result["message"], result)
        return result

    async def _rheed_intensity(self, frames: int) -> dict | None:
        """The RHEED image's pixel statistics over `frames` frames, for the emission search."""
        images, full_scale = [], None
        for i in range(max(1, frames)):
            if i:
                await asyncio.sleep(0.2)
            meta, frame = await self.rheed_camera.image()
            images.append(frame)
            full_scale = getattr(meta, "full_scale", None) or full_scale
        return frame_intensity(images, full_scale)

    async def rheed_step_up(self) -> dict:
        result = await self.rheed_gun.step_up()
        await self.add_recording_event("rheed_emission_step", result["message"], result)
        return result

    async def rheed_finish(self, filament_ma: float | None = None) -> dict:
        result = await self.rheed_gun.finish(filament_ma)
        await self.add_recording_event("rheed_on", result["message"], result)
        return result

    async def rheed_off(self) -> dict:
        """The RHEED gun's filament and HV off, ramped (rheed_all_off_gentle)."""
        if self.chamber.driver != "pld":
            raise RuntimeError("rheed_off needs pld-service: under --use-legacy-mi-mode the gun "
                               "is switched on its own controller, by hand")
        result = await self.rheed_gun.off()
        await self.add_recording_event("rheed_off", result["message"], result)
        return result

    async def set_target(self, target_id: str) -> None:
        # SelectTarget revolves the carousel and the mode commands spin the target;
        # the holding lock frees the target motor too, so gate this like a mask move.
        # Rotation and twist are always AUTO, never OFF: a target that is not
        # rastering gets hit in one spot.
        await self._await_motor_ready()
        await self.chamber.select_target(target_id)
        await self.chamber.target_auto_motion()

    async def rotate_sample_to(self, angle: float) -> None:
        """Absolute sample-stage rotation (`Set Sample Position`), in degrees.

        Gated on the motor holding lock like every other axis move: the lock frees
        the sample-rotation motor too, so a commanded turn while it is released
        drives nothing.
        """
        await self._await_motor_ready()
        await self.chamber.rotate_sample_to(angle)

    async def rotate_sample_by(self, delta_angle: float) -> None:
        """Relative sample-stage rotation (`Rotate Sample`): a signed delta in
        degrees from the current angle. Same holding-lock gate as `rotate_sample_to`.
        """
        await self._await_motor_ready()
        await self.chamber.rotate_sample_by(delta_angle)

    async def perform_preablation(
        self, target_id: str, num_pulse: int = 1000, frequency: float = 10.0,
        is_dryrun: bool = False, move_mask_to_block_position: bool = True,
    ) -> None:
        await self.chamber.sample_shutter(False)

        original_mask_position = None
        if move_mask_to_block_position:
            original_mask_position = await self.get_current_mask_position()
            await self.move_mask_to_position(self.pld_config.mask_block_position)

        await self.set_target(target_id)

        before = await self._log_values()
        if not is_dryrun:
            await self.chamber.fire(num_pulse, frequency)
        after = await self._log_values()

        if before["MissedPuls"] != after["MissedPuls"]:
            raise RuntimeError("laser missed pulses during preablation -- check the laser")

        if original_mask_position is not None:
            await self.move_mask_to_position(original_mask_position)

    def note_preablation(self, material: str | None, num_pulse: int, frequency: float) -> None:
        """A pre-ablation really fired: the next growth record of `material` says so."""
        if material and num_pulse > 0:
            self._preablations[material] = (int(num_pulse), float(frequency))

    def take_preablation(self, material: str | None) -> dict:
        """The do_preablation fields for a record of `material`, once per pre-ablation."""
        found = self._preablations.pop(material, None) if material else None
        return _preablation_fields(found)

    async def _warm_up_to_pid_limit(self) -> None:
        b = self.bounds
        await self.chamber.manual_current()

        values = await self._log_values()
        warm_up_current = b.warm_up_current
        starting_point = float(values["HT set"])

        # Already at the warm-up current (set by hand, or a warm-up cut short): no steps
        # to run, but PID still waits for the threshold below. Returning here engaged it
        # at whatever the substrate read (177 C on the twin, 2026-11-20).
        if starting_point >= warm_up_current:
            log.info("warm-up starting point %.2f A is at or above the warm-up current %.2f A; "
                     "no steps, waiting for the PID-engage threshold", starting_point, warm_up_current)

        try:
            current = starting_point + b.warm_up_step
            while current <= warm_up_current + b.warm_up_step * 0.5:
                await self.chamber.set_heating_current(float(current))
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
        except Exception as exc:
            # The warm-up is open-loop: failing here would leave the heater at whatever
            # current it had reached, with no PID and nobody ramping it. Put it back
            # where it started before reporting.
            try:
                await self.chamber.set_heating_current(starting_point)
                back = f"heating current set back to {starting_point:.2f} A"
            except Exception as again:
                back = f"could NOT set the heating current back to {starting_point:.2f} A ({again}) -- check the heater"
            raise RuntimeError(f"manual warm-up failed: {exc}; {back}") from exc

    async def is_heating_laser_on(self) -> bool:
        """`Heat Stat` bit 3, the power supply's own ON/OFF monitor -- what the heater
        is actually doing, not what it was last told."""
        values = await self._log_values()
        return _truthy(values["ON/OFF monitor in PS"])

    async def to_temperature(self, temperature: float, ramp_rate: float = 20.0, wait: bool = True) -> float:
        """Ramp to `temperature`. With `wait` the call returns once the substrate
        arrives; without it, once the ramp is set going.

        Under legacy MI mode PASCAL runs one command at a time and the arrival wait is
        a command, so `wait` holds every other chamber command until the ramp ends.
        Under pld-service (7148fc7 on) only commands sharing a resource queue behind
        each other, so a waited fire or move does not delay the heater. A failed warm-up
        puts the current back (_warm_up_to_pid_limit)."""
        b = self.bounds
        temperature = max(b.temperature_min, min(b.temperature_max, temperature))

        # A setpoint below the PID-engage threshold has no ramp to run: nothing below
        # it is holdable, the heating diode is deliberately off there. What to do
        # depends on where the chamber is right now, so read it before deciding.
        if temperature < b.temperature_pid_engage_threshold:
            # Already cold -- a room-temperature growth. Don't check the diode and
            # don't touch the temperature controller: issuing TemperatureSet
            # (nowait=False) at a target no current can reach just blocks the MI
            # command until it times out.
            if await self.get_current_temperature() < b.temperature_pid_engage_threshold:
                return temperature
            # Still hot. Asking for a sub-threshold setpoint from here means "come
            # down", so command it the way cool_down does rather than returning a
            # success that leaves the chamber where it was. PID stays out of it --
            # the controller cannot hold this setpoint, the substrate coasts to it.
            await self.chamber.coast_to(temperature, ramp_rate, wait)
            return temperature

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

        # Target first, then the ramp rate, then PID on. The controller then ramps from
        # where the substrate is to where we want it. Engaging PID before the target is
        # set lets it chase whatever setpoint it still holds -- after the manual warm-up
        # that is the cold 160, and the substrate is pulled back down to it.
        await self.chamber.ramp_to(temperature, ramp_rate, wait)
        return temperature

    async def cool_down(self, ramp_rate: float = 20.0) -> None:
        await self.chamber.coast_to(self.bounds.temperature_min, ramp_rate, wait=True)

    async def perform_deposition(self, num_pulse: int, laser_repetition_rate: float, target_id: str, is_dryrun: bool = False) -> None:
        await self.set_target(target_id)
        await self.chamber.sample_shutter(True)
        if not is_dryrun:
            await self.chamber.fire(num_pulse, laser_repetition_rate)

    async def anneal(self, steps: list[tuple[float, float, float]]) -> None:
        for temperature, ramp_rate, wait_time in steps:
            await self.to_temperature(temperature, ramp_rate=ramp_rate)
            if wait_time > 0:
                await count_down(wait_time, prefix="annealing")

    # --- storage / provenance ----------------------------------------------------

    async def _current_sample_name(self) -> str | None:
        """The sample the chamber is loaded on right now, e.g. `STO-a1b2c3-p0`.

        `_materialise_samples` already bakes the position into `sample_name`, so
        pulling it from the DB (rather than re-deriving it here) is what makes the
        recording's name carry the position "if any" without this function having to
        know the substrate's numbering scheme.
        """
        substrate = self.current_substrate
        if substrate is None or substrate.db_id is None:
            return None
        row = await self.growth_db.find_sample(substrate.db_id, substrate.current_position_id)
        return row[7] if row else None

    async def start_storage(
        self,
        project_name: str,
        is_dryrun: bool = False,
        *,
        save_frame: bool = True,
        save_ai: bool = True,
        save_log: bool = True,
        save_integration: bool = True,
        force_rewrite: bool = False,
        rheed_energy_kev: float | None = None,
        note: str | None = None,
        operator: str | None = None,
    ) -> dict:
        record_uuid = str(uuid.uuid4())
        sample_name = await self._current_sample_name()
        # Sample leads: it's the piece of physical film someone is going to go look
        # for on disk, so it belongs first, human-readable, ahead of the uuid. Without
        # a sample the recording is not tied to a specimen (a notebook could call this
        # before registering a substrate); fall back to the project alone rather than
        # raising, since start_storage has no other way to refuse cleanly.
        slug = f"{sample_name}_{project_name}" if sample_name else project_name
        storage_name = f"{slug}_{record_uuid}"
        if is_dryrun:
            # Nothing is written, so there is no recording to name: a growth record
            # given this name would point at a file that does not exist.
            return {"ok": True, "record_uuid": record_uuid, "storage_name": None, "path": None,
                    "message": f"dry run: nothing recorded (it would have been {storage_name!r})"}
        # The recording names its project, so that is the project now: its context
        # and the growth records after it (run_sequence wants one) carry its id.
        if self.project_id is None or self.project_name != project_name:
            await self.register_project(project_name)

        status = await self.storage.start_recording(StorageRequest(
            project_name=storage_name, save_frame=save_frame, save_ai=save_ai, save_log=save_log,
            save_integration=save_integration, force_rewrite=force_rewrite,
            rheed_energy_kev=rheed_energy_kev, note=note, operator=operator,
            context=self.recording_context(sample_name, storage_name, record_uuid,
                                           pyrometer_emissivity=await self.pyrometer_emissivity()),
        ))
        if status.ok:
            self.recording_name = storage_name
        return {
            "ok": status.ok, "record_uuid": record_uuid, "storage_name": storage_name,
            "path": status.path, "message": status.message,
        }

    async def end_storage(self, is_dryrun: bool = False) -> dict:
        if is_dryrun:
            return {"ok": True, "message": "dry run: no recording to end"}
        status = await self.storage.stop_recording()
        if status.ok or "not recording" in (status.message or ""):
            self.recording_name = None
        return {"ok": status.ok, "message": status.message}

    def recording_context(self, sample_name: str | None, storage_name: str, record_uuid: str,
                          pyrometer_emissivity: float | None = None) -> dict:
        """What the recording should know about the experiment it belongs to: kept in
        the file's /meta/experiment, so the file explains itself without growth.db."""
        substrate = self.current_substrate
        position = None
        if substrate is not None and substrate.positions:
            try:
                position = float(substrate.positions[substrate.current_position_id])
            except (IndexError, TypeError, ValueError):
                position = None
        return {
            "project_id": self.project_id, "project_name": self.project_name,
            "substrate_id": substrate.db_id if substrate else None,
            "sample_name": sample_name,
            "pixel_index": substrate.current_position_id if substrate else None,
            "num_pixel_positions": len(substrate.positions) if substrate else None,
            "pixel_position": position,
            "record_name": storage_name, "record_uuid": record_uuid,
            "laser_pulse_energy_set_mj": self._laser_power_set,
            "laser_pulse_energy_measured_mj": self._laser_power_real,
            "laser_source_energy_mj": self._laser_source_energy,
            "laser_meter_ratio": self._laser_meter_ratio,
            "pyrometer_emissivity": pyrometer_emissivity,
        }

    async def pyrometer_emissivity(self) -> float | None:
        """The emissivity the back pyrometer reads at now, which every temperature
        recorded with it depends on (lumi.experiment.pyrometer). None under the legacy
        MI driver, whose log does not carry it."""
        if self.chamber.driver != "pld":
            return None
        try:
            emissivity = (await self.chamber.state()).heater.emissivity
        except Exception as exc:
            log.warning("could not read the pyrometer emissivity: %s", exc)
            return None
        return emissivity if emissivity and emissivity > 0 else None

    async def add_recording_event(self, kind: str, message: str = "", detail: dict | None = None) -> None:
        """Tell the recording in progress what the driver is doing. Best effort: a
        storage node that does not answer must never stop the experiment."""
        if self.recording_name is None:
            return
        try:
            await asyncio.wait_for(self.storage.add_recording_event(
                RecordingEventIn(kind=kind, message=message, detail=detail or {})), 5.0)
        except Exception as exc:
            log.warning("could not add %r to the recording: %s", kind, exc)

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
            pyrometer_emissivity=await self.pyrometer_emissivity(),
            laser_source_energy=self._laser_source_energy, laser_meter_ratio=self._laser_meter_ratio,
            **self.take_preablation(target_material),
        )
        if storage_name is not None:
            await self.growth_db.add_recording(
                experiment_id=experiment_id, record_name=storage_name, record_uuid=record_uuid or "",
            )
        return experiment_id

    # --- gated: laser pulse energy --------------------------------------------------------

    async def begin_set_laser_power(self, laser_power: float, force: bool = False) -> tuple[bool, float | None]:
        """Returns (already_satisfied, cached_measured_power). If already_satisfied,
        no confirmation is needed -- the caller should skip straight to the cached
        value rather than opening a pending gate for nothing.

        Under pld-service no one reads the meter: today's laser test gives what
        reaches the meter per mJ the excimer reports, and the excimer runs in energy
        mode at `laser_power` divided by that. Refused when there was no test today.

        Under the legacy MI driver it is measured at Clear: the meter reads the beam at
        the exit port, which only the empty slot lets through."""
        if self.chamber.driver == "pld":
            return await self._set_laser_power_from_test(laser_power, force)
        if not force and self._laser_power_set == laser_power:
            return True, self._laser_power_real

        await self.chamber.sample_shutter(False)
        await self._await_motor_ready()  # SelectTarget revolves the carousel
        await self.chamber.select_target(pcmd.Targets.Clear)
        self._pending_laser_power = laser_power
        return False, None

    async def confirm_laser_power(self, measured_power: float) -> float:
        self._laser_power_set = self._pending_laser_power
        self._laser_power_real = measured_power
        return measured_power

    async def set_laser_energy(self, mj: float):
        return await self._set_excimer(self.chamber.set_laser_energy(mj))

    async def set_laser_hv(self, kv: float):
        return await self._set_excimer(self.chamber.set_laser_hv(kv))

    async def _set_excimer(self, change):
        try:
            return await change
        finally:
            # Even a refused change may have switched the mode: the confirmed power
            # no longer describes the beam, so the next check measures it again.
            self._laser_power_set = None
            self._laser_power_real = None
            self._laser_source_energy = None
            self._laser_meter_ratio = None

    async def _set_laser_power_from_test(self, laser_power: float, force: bool) -> tuple[bool, None]:
        test = await self.todays_laser_test()
        if test is None:
            raise RuntimeError(
                "no laser test today: run the daily test first (begin_laser_test, the "
                f"excimer at {LASER_TEST_KV:g} kV into the meter), then set the energy")
        ratio = test["ratio"]
        if not force and self._laser_power_set == laser_power and self._laser_meter_ratio == ratio:
            return True, None
        source = laser_power / ratio
        await self.set_laser_energy(source)
        self._laser_power_set = laser_power
        self._laser_source_energy, self._laser_meter_ratio = source, ratio
        message = (f"{laser_power:.1f} mJ at the meter: excimer energy mode at {source:.1f} mJ "
                   f"(ratio {ratio:.3f} from laser test {test['test_id']})")
        log.info("laser energy: %s", message)
        await self.add_recording_event("laser_energy", message, {
            "meter_mj": laser_power, "source_mj": source, "ratio": ratio, "test_id": test["test_id"]})
        return True, None

    # --- the daily laser test ---------------------------------------------------------

    async def record_sequence_layer(self, layer: dict, *, sequence: dict, temperature: float | None,
                                    pressure: float | None, ratio: float | None,
                                    storage_name: str | None, record_uuid: str | None) -> int | None:
        """The growth record of one layer a sequence grew, as finish_experiment_record
        writes one for a deposition. None without a substrate or project."""
        substrate = self.current_substrate
        if substrate is None or substrate.db_id is None or self.project_id is None:
            return None
        meter = layer.get("meter_mj")
        experiment_id = await self.growth_db.add_experiment(
            substrate_id=substrate.db_id, is_pixel=len(substrate.positions) > 1,
            pixel_location=substrate.current_position_id, experiment_uuid=str(uuid.uuid4()),
            project_id=self.project_id, temperature=temperature, pressure=pressure,
            laser_power=meter, laser_pulse_rate=layer["laser_repetition_rate"],
            target_material=layer.get("target_material"), num_pulse=layer["num_pulse"],
            pyrometer_emissivity=await self.pyrometer_emissivity(),
            laser_source_energy=meter / ratio if meter is not None and ratio else None,
            laser_meter_ratio=ratio if meter is not None else None, sequence=sequence,
            **(_preablation_fields(layer["preablation"]) if layer.get("preablation")
               else self.take_preablation(layer.get("target_material"))),
        )
        if storage_name is not None:
            await self.growth_db.add_recording(
                experiment_id=experiment_id, record_name=storage_name, record_uuid=record_uuid or "")
        return experiment_id

    #: How often a running sequence is polled, s.
    SEQUENCE_POLL_S = 0.5
    #: How long pld-service may not answer before a run being followed is given up as unknown.
    SEQUENCE_LOST_S = 15.0
    _SEQUENCE_ENDED = frozenset({"finished", "failed", "cancelled"})

    async def run_sequence(self, seq: dict, on_status, on_event) -> dict:
        """Run one stored, approved version on pld-service and follow it to the end.

        meter_energy steps become laser_energy from today's laser test, with the
        excimer's settings port held in energy mode for the run (it cannot be taken
        while the run holds the service) and given back after. `on_status` gets each
        new SequenceStatus, `on_event` each per-step 'sequence' event. Returns the
        final status; a failed or cancelled run raises. Cancelling this coroutine
        cancels the run on pld-service.
        """
        if self.chamber.driver != "pld":
            raise RuntimeError("sequences need pld-service: not under --use-legacy-mi-mode")
        steps = json.loads(seq["steps_json"])
        sets_energy = any(seqlib.METER_ENERGY in step for step in steps)
        ratio = None
        if sets_energy:
            test = await self.todays_laser_test()
            if test is None:
                raise RuntimeError(
                    "no laser test today: run the daily test (begin_laser_test) before a sequence "
                    "that sets the energy")
            ratio = test["ratio"]
        resolved = seqlib.resolve(steps, ratio)
        name = f"{seq['name']} v{seq['version']}"

        prior = (await self.chamber.state()).sequence
        if prior.state in ("running", "pausing", "paused"):
            raise RuntimeError(f"pld-service is already running {prior.name!r} ({prior.state})")
        seen = max((e.time for e in await self.chamber.recent_events()), default=0.0)
        took = await self.chamber.hold_laser_in_energy_mode() if sets_energy else False
        if sets_energy:
            # The excimer's energy is now the sequence's: a deposition after it sets its own again.
            self._laser_power_set = self._laser_source_energy = self._laser_meter_ratio = None
        status = prior
        try:
            await self.chamber.run_sequence(name, resolved)
            await self.add_recording_event("sequence", f"{name} started", {
                "sequence": seq["name"], "version": seq["version"], "content_hash": seq["content_hash"],
                "ratio": ratio})
            # The run is the status whose `started` differs from the one before it. A poll
            # that fails is retried for SEQUENCE_LOST_S (pld-service keeps running it); a
            # status that no longer is this run (pld-service restarted) means it is lost.
            run_started: float | None = None
            quiet_since = asked = time.monotonic()
            while True:
                await asyncio.sleep(self.SEQUENCE_POLL_S)
                try:
                    status = (await self.chamber.state()).sequence
                    events = await self.chamber.recent_events()
                except Exception as exc:
                    if time.monotonic() - quiet_since > self.SEQUENCE_LOST_S:
                        raise SequenceLost(
                            f"sequence {name}: pld-service has not answered for "
                            f"{self.SEQUENCE_LOST_S:g} s ({exc}); whether it is still running is "
                            "unknown: check the chamber") from exc
                    continue
                quiet_since = time.monotonic()
                if run_started is None and status.started != prior.started:
                    run_started = status.started
                if run_started is None and time.monotonic() - asked > self.SEQUENCE_LOST_S:
                    raise SequenceLost(f"sequence {name}: pld-service never showed it running "
                                       f"(it shows {status.name!r}, {status.state or 'none'})")
                if run_started is not None and status.started != run_started:
                    raise SequenceLost(
                        f"sequence {name}: pld-service no longer shows it (it shows "
                        f"{status.name!r}, {status.state or 'none'}); it was restarted or the run "
                        "was replaced, so how far it got is unknown: check the chamber")
                for event in events:
                    if event.time > seen and event.kind == "sequence":
                        await on_event(event)
                    seen = max(seen, event.time)
                await on_status(status)
                if run_started is not None and status.state in self._SEQUENCE_ENDED:
                    break
        except asyncio.CancelledError:
            try:
                await asyncio.shield(self.chamber.cancel_sequence())
            except Exception as exc:
                log.warning("could not cancel the sequence on pld-service: %s", exc)
            raise
        finally:
            if took:
                try:
                    await self.chamber.release_laser()
                except Exception as exc:
                    log.warning("could not give the excimer's port back after the sequence: %s", exc)
        await self.add_recording_event("sequence", f"{name} {status.state}: {status.message}",
                                       {"state": status.state, "done": status.done, "total": status.total})
        result = {"sequence": seq["name"], "version": seq["version"], "state": status.state,
                  "done": status.done, "total": status.total, "message": status.message}
        if status.state != "finished":
            raise RuntimeError(f"sequence {name} {status.state}: {status.message}")
        return result

    async def todays_laser_test(self) -> dict | None:
        """The latest laser test, if it was done today (this host's calendar day)."""
        tests = await self.growth_db.list_laser_tests(limit=1)
        if tests and date.fromtimestamp(tests[0]["tested_at"]) == date.today():
            return tests[0]
        return None

    async def begin_laser_test(self) -> None:
        """Shutter closed, carousel to Clear (the meter sits at the exit port), excimer
        in HV mode at LASER_TEST_KV. Then the person either says yes and
        fire_laser_test fires, or fires the laser themselves."""
        if self.chamber.driver != "pld":
            raise RuntimeError("the laser test needs pld-service: under --use-legacy-mi-mode "
                               "the excimer is set on its own PC (use begin_set_laser_power)")
        await self.chamber.sample_shutter(False)
        await self._await_motor_ready()  # SelectTarget revolves the carousel
        await self.chamber.select_target(pcmd.Targets.Clear)
        await self.set_laser_hv(LASER_TEST_KV)
        self._laser_test = {"fired_by": "person"}

    async def fire_laser_test(self) -> dict:
        """Fire the test train into the meter, reading the excimer's energy while it
        runs. Returns its average as source_mj, or None with `source_note` saying why
        the readings were not taken (see `source_from_readings`).

        Refused unless the excimer is still at LASER_TEST_KV in HV mode, as
        begin_laser_test left it; fails if the train did not fire all its pulses."""
        if self._laser_test is None:
            raise RuntimeError("no laser test is open: call begin_laser_test first")
        excimer = (await self.chamber.state()).excimer
        if not excimer.mode.upper().startswith("HV") or excimer.hv is None \
                or abs(excimer.hv - LASER_TEST_KV) > 0.05:
            raise RuntimeError(
                f"the excimer is not at {LASER_TEST_KV:g} kV in HV mode (mode {excimer.mode or '?'}, "
                f"HV {excimer.hv if excimer.hv is not None else '?'} kV): call begin_laser_test again")
        readings: list[tuple[float, float | None]] = []  # (energy, energy setpoint)
        fire = asyncio.create_task(self.chamber.fire(LASER_TEST_PULSES, LASER_TEST_RATE_HZ))
        while not fire.done():
            await asyncio.wait({fire}, timeout=LASER_TEST_POLL_S)
            try:
                state = await self.chamber.state()
            except Exception as exc:
                log.warning("laser test: could not read the excimer energy: %s", exc)
                continue
            # Only while this train is firing: before its first pulse the reading is
            # the last train's, or nothing.
            energy = state.excimer.energy
            if state.laser.status == "firing" and state.laser.pulses_fired > 0 and energy and energy > 0:
                readings.append((float(energy), state.excimer.energy_set))
        await fire
        fired = (await self.chamber.state()).laser.pulses_fired
        if fired < LASER_TEST_PULSES:
            self._laser_test = {"fired_by": "lumi", "cut_short": fired}
            raise RuntimeError(f"the test train fired {fired} of {LASER_TEST_PULSES} pulses: the meter's "
                               "average is not over the test; fire_laser_test again")
        source, note = source_from_readings(readings)
        self._laser_test = {"fired_by": "lumi", "source_mj": source, "source_note": note}
        result = {"source_mj": source, "readings": len(readings), "pulses_fired": fired}
        if note:
            result["source_note"] = f"{note}; pass source_mj, read off the laser's display, to confirm_laser_test"
        return result

    async def confirm_laser_test(self, meter_mj: float, source_mj: float | None = None,
                                 actor: str | None = None) -> dict:
        """Record the test: the meter's average from the person, the source energy
        from what the excimer reported while firing (or `source_mj`, read off the
        laser by the person). Refused for a test train cut short, a source energy that
        is not a measurement, or a meter reading above the source."""
        test = self._laser_test
        if test is None:
            raise RuntimeError("no laser test is open: call begin_laser_test first")
        if "cut_short" in test:
            raise RuntimeError(f"the last test train fired only {test['cut_short']} of {LASER_TEST_PULSES} "
                               "pulses: fire_laser_test again (or begin_laser_test again to fire it by hand)")
        note = ""
        if source_mj is None and test["fired_by"] == "lumi":
            source_mj, note = test["source_mj"], test["source_note"]
        elif source_mj is None:
            # The person fired it: one reading, after the train, so only the setpoint
            # echo can be told from a measurement.
            excimer = (await self.chamber.state()).excimer
            energy, setpoint = excimer.energy, excimer.energy_set
            if not energy or energy <= 0:
                note = "the excimer reported no energy"
            elif setpoint is not None and abs(energy - setpoint) < LASER_TEST_FLAT_MJ:
                note = f"the excimer shows {energy:.2f} mJ, its energy setpoint: that is not a measurement"
            else:
                source_mj = float(energy)
        if source_mj is None:
            raise ValueError(f"{note}: pass source_mj, the energy the laser's display showed")
        if meter_mj / source_mj > LASER_TEST_MAX_RATIO:
            raise ValueError(
                f"the meter ({meter_mj:.1f} mJ) reads {meter_mj / source_mj:.2f} times the source "
                f"({source_mj:.1f} mJ): the beam only loses energy on its way to the meter, so one "
                "number is wrong (swapped, or a typo); check both and confirm again")
        lumi_fired = test["fired_by"] == "lumi"
        row = await self.growth_db.add_laser_test(
            tested_at=time.time(), hv_kv=LASER_TEST_KV,
            pulses=LASER_TEST_PULSES if lumi_fired else None,
            rate_hz=LASER_TEST_RATE_HZ if lumi_fired else None,
            fired_by=test["fired_by"], source_mj=source_mj, meter_mj=meter_mj, actor=actor,
        )
        self._laser_test = None
        message = (f"{LASER_TEST_KV:g} kV: {source_mj:.1f} mJ at the source, {meter_mj:.1f} mJ "
                   f"at the meter, ratio {row['ratio']:.3f}")
        log.info("laser test %d: %s", row["test_id"], message)
        await self.add_recording_event("laser_test", message, row)
        return row

    # --- gated: mask-center calibration --------------------------------------------

    async def begin_align_center_mask(self) -> None:
        await self._await_motor_ready()
        await self.chamber.move_rheed_gun(0)
        await self.chamber.move_mask(self.pld_config.center_mask_pos)

    async def confirm_center_mask(self, position: float) -> None:
        await self.set_center_mask_pos(position, source="confirm_center_mask")

    async def set_center_mask_pos(self, position: float, source: str) -> None:
        """The one way `center_mask_pos` changes: in memory, and saved to growth.db
        so the node comes back up with it (see `ExperimentHandler.load_calibration`)."""
        log.info("center_mask_pos %.3f -> %.3f mm (%s)", self.pld_config.center_mask_pos, position, source)
        self.pld_config.center_mask_pos = position
        await self.growth_db.set_calibration("center_mask_pos", position, source=source)

    # --- gated: mask-center check loop ----------------------------------------------

    async def begin_check_mask_center(self) -> None:
        await self._await_motor_ready()
        await self.chamber.select_target(pcmd.Targets.Clear)
        await self.chamber.sample_shutter(True)
        await self.chamber.move_mask(self.pld_config.center_mask_pos)

    async def confirm_mask_center(self, aligned: bool, corrected_position: float | None = None) -> bool:
        """Returns True if still pending (not yet aligned)."""
        if aligned:
            return False

        if corrected_position is not None:
            await self.set_center_mask_pos(corrected_position, source="confirm_mask_center")
        await self._await_motor_ready()
        # Retract-then-reapproach: a move of only a few mm can leave the mask motor
        # stuck, so back off first rather than nudging directly to the new position.
        await self.chamber.move_mask(max(self.pld_config.center_mask_pos - 5, 0))
        await self.chamber.move_mask(self.pld_config.center_mask_pos)
        return True

    # --- automatic: mask-center alignment from a fiducial marker --------------------

    async def auto_align_center_mask(
        self,
        center_mm: float | None = None,
        half_window_mm: float = 4.0,
        step_mm: float = 0.5,
        max_passes: int = 3,
        points_per_pass: int = 15,
        tolerance_mm: float = 0.02,
        frames_per_point: int = 2,
        min_contrast: float = 5.0,
        apply: bool = True,
    ) -> dict:
        """The camera-read version of the begin/confirm_mask_center loop: scan Mask1
        across `center_mm +- half_window_mm` (`center_mm` defaulting to
        `center_mask_pos`), read the marker tagged
        `mask-center` at each point, and find where the slit passes over it from the
        intensity-vs-position curve (see `lumi.experiment.mask_align`). Then re-scan
        just that bump, finer each pass, until the centre settles within
        `tolerance_mm` or `max_passes` runs out. Every pass is one steady scan in
        increasing Mask1, so the mask never wanders back and forth mid-scan.

        Leaves the mask at the found centre, so the result can be checked by eye, and
        sets `center_mask_pos` to it when `apply`. Sets up the view the same way
        `begin_check_mask_center` does -- target carousel on Clear, sample shutter
        open -- since both look at the same thing.
        """
        marker_id, problem = await self.find_mask_center_marker()
        if marker_id is None:
            raise RuntimeError(problem)

        previous = self.pld_config.center_mask_pos
        n = int(round(2 * half_window_mm / step_mm))
        start = (previous if center_mm is None else center_mm) - half_window_mm
        positions = [start + i * step_mm for i in range(n + 1)]
        self._check_mask_travel(positions)

        await self._await_motor_ready()
        await self.chamber.select_target(pcmd.Targets.Clear)
        await self.chamber.sample_shutter(True)

        samples: list[MaskSweepPoint] = []
        passes: list[MaskAlignPass] = []

        async def scan(pass_index: int, points: list[float]) -> list[float]:
            readings = []
            for position in points:
                await self.move_mask_to_position(position)
                readings.append(await self._fresh_marker_reading(marker_id, frames_per_point))
                samples.append(MaskSweepPoint(pass_index=pass_index, position=position, reading=readings[-1]))
                log.info("mask scan %d: %.3f mm -> %.1f", pass_index, position, readings[-1])
            return readings

        # Pass 0: the wide scan. Its median is the plate's reading, which every
        # narrower pass reuses -- those sit mostly on the bump, where a median would
        # be the slit's own level instead.
        fit = locate_slit(positions, await scan(0, positions), min_contrast=min_contrast)
        baseline, step = fit.baseline, step_mm
        passes.append(MaskAlignPass(start=positions[0], stop=positions[-1], step=step, center=fit.center,
                                    width=fit.width, contrast=fit.contrast))
        log.info("mask scan 0: slit at %.3f mm (bump %.2f mm wide)", fit.center, fit.width)

        converged = False
        for pass_index in range(1, max_passes):
            # The bump plus a margin of two of the last pass's steps each side: the
            # bump's true edges lie up to a step beyond its outermost half-height
            # points, and the fit needs plate-level points past both of them.
            half = fit.width / 2 + 2 * step
            step = 2 * half / (points_per_pass - 1)
            points = [fit.center - half + i * step for i in range(points_per_pass)]
            self._check_mask_travel(points)
            refined = locate_slit(points, await scan(pass_index, points),
                                  min_contrast=min_contrast, baseline=baseline)
            passes.append(MaskAlignPass(start=points[0], stop=points[-1], step=step, center=refined.center,
                                        width=refined.width, contrast=refined.contrast))
            shift = abs(refined.center - fit.center)
            log.info("mask scan %d: slit at %.3f mm (moved %.3f mm, step %.3f mm)",
                     pass_index, refined.center, shift, step)
            fit = refined
            if shift < tolerance_mm:
                converged = True
                break

        log.info(
            "mask centre found at %.3f mm (was %.3f; contrast %.1f over baseline %.1f; %s)",
            fit.center, previous, fit.contrast, baseline, "converged" if converged else "max passes reached",
        )
        if apply:
            await self.set_center_mask_pos(fit.center, source="auto_align_center_mask")
        await self.move_mask_to_position(fit.center)
        return MaskAlignResult(
            marker_id=marker_id, previous_center=previous, center=fit.center, applied=apply,
            converged=converged, contrast=fit.contrast, baseline=baseline, polarity=fit.polarity,
            passes=passes, samples=samples,
        ).model_dump()

    async def find_mask_center_marker(self) -> tuple[str | None, str]:
        """The marker tagged `mask-center`, or (None, what is wrong) -- untagged, or
        tagged to a marker that has since been deleted. Asked without raising so the
        handler can hold the alignment open for an operator to fix it."""
        fiducial = self.chamber_fiducial
        if fiducial is None:
            raise RuntimeError("no chamber fiducial client -- auto-alignment needs the chamber camera's markers")
        marker_id = (await fiducial.list_roles()).roles.get(MASK_CENTER_ROLE)
        if marker_id is None:
            return None, (f"no fiducial marker is tagged {MASK_CENTER_ROLE!r} -- tag the marker on "
                          "the sample's centre with that role")
        if marker_id not in {m.marker_id for m in (await fiducial.list_markers()).markers}:
            return None, (f"role {MASK_CENTER_ROLE!r} points at marker {marker_id!r}, which no longer "
                          "exists -- tag the marker on the sample's centre with that role")
        return marker_id, ""

    def _check_mask_travel(self, positions: list[float]) -> None:
        if positions[0] < 0 or positions[-1] >= self.bounds.mask_travel_max:
            raise ValueError(
                f"scan {positions[0]:.2f}..{positions[-1]:.2f} mm leaves the mask's travel "
                f"[0, {self.bounds.mask_travel_max})"
            )

    async def _fresh_marker_reading(
        self, marker_id: str, frames: int, timeout: float = 10.0, poll: float = 0.05,
    ) -> float:
        """Mean of `marker_id`'s reading over the next `frames` camera frames. The
        frame current when this is called may have been exposed mid-move, so it is
        skipped: frames are told apart by their uuid, not by time, which would need
        this host's clock to agree with the chamber node's."""
        assert self.chamber_fiducial is not None
        seen = {(await self.chamber_fiducial.marker_stats()).uuid}
        values: list[float] = []
        deadline = time.monotonic() + timeout
        while len(values) < frames:
            if time.monotonic() > deadline:
                raise RuntimeError(f"no new camera frame within {timeout:.0f}s -- is the chamber camera streaming?")
            sample = await self.chamber_fiducial.marker_stats()
            if sample.uuid in seen:
                await asyncio.sleep(poll)
                continue
            seen.add(sample.uuid)
            stats = sample.stats.get(marker_id)
            if stats is None or stats.mean is None:
                raise RuntimeError(f"marker {marker_id!r} has no reading -- is it on the frame?")
            values.append(stats.mean)
        return sum(values) / len(values)

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
