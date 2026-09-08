"""Growth provenance: substrate, project, experiment and recording history.

Ported near-verbatim from ~/Projects/OperationsNotebooks/src/db.py -- the schema and
queries are unchanged (an existing growth.db from the old notebook driver should open
and read fine against this), and it was already properly async (aiosqlite) unlike a
lot of the driver code around it. The only real change: the path is no longer a bare
"growth.db" default the caller has to know to override -- it comes from
settings.experiment.growth_db_path, and connect() makes sure the parent directory
exists rather than failing on a path like "cfg/growth.db" in a fresh checkout.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from uuid import uuid4

import aiosqlite


class GrowthDB:
    def __init__(self, db_path: str = "growth.db", logger: logging.Logger | None = None) -> None:
        self.db_path = db_path
        self.conn: aiosqlite.Connection | None = None
        self.logger = logger or logging.getLogger(__name__)

    async def connect(self) -> None:
        parent = Path(self.db_path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.db_path)

    async def create_database(self) -> None:
        """Create the schema if it does not already exist."""
        await self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS substrate (
                substrate_id INTEGER PRIMARY KEY,
                substrate_uuid VARCHAR(100) NOT NULL,
                materials VARCHAR(100) NOT NULL,
                orientation VARCHAR(50),
                height FLOAT,
                width FLOAT,
                thickness FLOAT,
                pixel_spacing FLOAT,
                positions TEXT,
                manufacture VARCHAR(50),
                manufacture_date VARCHAR(50),
                substrate_name TEXT
            );

            CREATE TABLE IF NOT EXISTS project (
                project_id INTEGER PRIMARY KEY,
                project_name VARCHAR(100) NOT NULL,
                description TEXT,
                project_created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS experiment (
                experiment_id INTEGER PRIMARY KEY,
                experiment_uuid VARCHAR(100),
                substrate_id INTEGER,
                project_id INTEGER,
                is_pixel BOOLEAN DEFAULT FALSE,
                pixel_location INTEGER,
                temperature FLOAT,
                pressure FLOAT,
                laser_power FLOAT,
                laser_pulse_rate FLOAT,
                target_material VARCHAR(100),
                num_pulse INTEGER,
                do_preablation BOOLEAN DEFAULT FALSE,
                preablation_pulse INTEGER,
                preablation_frequency INTEGER,
                before_experiment_waittime FLOAT,
                after_experiment_waittime FLOAT,
                ramp_rate FLOAT,
                experiment_created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (substrate_id) REFERENCES substrate(substrate_id),
                FOREIGN KEY (project_id) REFERENCES project(project_id)
            );

            CREATE TABLE IF NOT EXISTS record (
                record_id INTEGER PRIMARY KEY,
                record_uuid VARCHAR(100),
                experiment_id INTEGER,
                record_name VARCHAR(100),
                FOREIGN KEY (experiment_id) REFERENCES experiment(experiment_id)
            );

            -- What exists: one row per physical specimen, as a tree.
            -- A bare substrate is the root (parent NULL, kind 'substrate'); each
            -- growable position is a child; a piece cleaved off a grown position is a
            -- child of that. `pixel_index` is NULL for a single-deposition sample that
            -- covers the whole substrate, which is what lets the pixel and non-pixel
            -- paths stop being two different code paths.
            CREATE TABLE IF NOT EXISTS sample (
                sample_id INTEGER PRIMARY KEY,
                sample_uuid VARCHAR(100) NOT NULL,
                parent_sample_id INTEGER,
                substrate_id INTEGER NOT NULL,
                kind VARCHAR(20) NOT NULL DEFAULT 'position',
                pixel_index INTEGER,
                position_mm FLOAT,
                sample_name TEXT,
                state VARCHAR(20) NOT NULL DEFAULT 'planned',
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (parent_sample_id) REFERENCES sample(sample_id),
                FOREIGN KEY (substrate_id) REFERENCES substrate(substrate_id)
            );

            -- A run of the chamber, from node start to node stop. Steps chain within
            -- one of these rather than per sample, because plenty of what happens --
            -- alignment, a gas change, warming up with nothing loaded -- belongs to
            -- the chamber and not to any specimen.
            CREATE TABLE IF NOT EXISTS chamber_session (
                session_id INTEGER PRIMARY KEY,
                session_uuid VARCHAR(100) NOT NULL,
                node_instance VARCHAR(100),
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ended_at TIMESTAMP
            );

            -- What happened. Append-only, written by the journal hooks, never edited
            -- by hand. `parent_step_id` is the previous step of the same session, so a
            -- session reads back as a chain.
            CREATE TABLE IF NOT EXISTS step (
                step_id INTEGER PRIMARY KEY,
                step_uuid VARCHAR(100) NOT NULL,
                session_id INTEGER,
                parent_step_id INTEGER,
                sample_id INTEGER,
                kind VARCHAR(100) NOT NULL,
                params TEXT,
                result TEXT,
                ok BOOLEAN,
                error TEXT,
                actor VARCHAR(100),
                source VARCHAR(100),
                started_at TIMESTAMP NOT NULL,
                ended_at TIMESTAMP,
                FOREIGN KEY (session_id) REFERENCES chamber_session(session_id),
                FOREIGN KEY (parent_step_id) REFERENCES step(step_id),
                FOREIGN KEY (sample_id) REFERENCES sample(sample_id)
            );

            -- What it measured. The BO metric is one kind among many; ex-situ results
            -- (XRD, AFM, PFM, transport) get a row here too.
            CREATE TABLE IF NOT EXISTS measurement (
                measurement_id INTEGER PRIMARY KEY,
                sample_id INTEGER NOT NULL,
                step_id INTEGER,
                experiment_id INTEGER,
                kind VARCHAR(50) NOT NULL,
                value FLOAT,
                detail TEXT,
                source VARCHAR(100),
                record_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (sample_id) REFERENCES sample(sample_id),
                FOREIGN KEY (step_id) REFERENCES step(step_id),
                FOREIGN KEY (experiment_id) REFERENCES experiment(experiment_id),
                FOREIGN KEY (record_id) REFERENCES record(record_id)
            );

            CREATE INDEX IF NOT EXISTS idx_substrate_uuid ON substrate(substrate_uuid);
            CREATE INDEX IF NOT EXISTS idx_experiment_uuid ON experiment(experiment_uuid);
            CREATE INDEX IF NOT EXISTS idx_record_uuid ON record(record_uuid);
            CREATE INDEX IF NOT EXISTS idx_step_session ON step(session_id, started_at);
            CREATE INDEX IF NOT EXISTS idx_step_kind ON step(kind, started_at);
            CREATE INDEX IF NOT EXISTS idx_step_sample ON step(sample_id, started_at);
            CREATE INDEX IF NOT EXISTS idx_sample_substrate ON sample(substrate_id);
            CREATE INDEX IF NOT EXISTS idx_sample_parent ON sample(parent_sample_id);
            CREATE INDEX IF NOT EXISTS idx_meas_sample ON measurement(sample_id);
            """
        )
        await self.conn.commit()
        self.logger.info("growth database ready at %s", self.db_path)

    # --- project -------------------------------------------------------------

    async def add_project(self, project_name: str, description: str | None = None) -> int:
        """Add a new project and return its ID. Idempotent on project_name."""
        async with self.conn.execute(
            "SELECT project_id FROM project WHERE project_name = ?", (project_name,)
        ) as cursor:
            existing = await cursor.fetchone()
        if existing:
            return existing[0]

        async with self.conn.execute(
            "INSERT INTO project (project_name, description) VALUES (?, ?)",
            (project_name, description),
        ) as cursor:
            await self.conn.commit()
            return cursor.lastrowid

    async def modify_project(self, project_id: int, project_name: str, description: str) -> None:
        await self.conn.execute(
            "UPDATE project SET project_name = ?, description = ? WHERE project_id = ?",
            (project_name, description, project_id),
        )
        await self.conn.commit()

    async def remove_project(self, project_id: int) -> None:
        await self.conn.execute("DELETE FROM project WHERE project_id = ?", (project_id,))
        await self.conn.commit()

    async def get_project(self, project_id: int):
        async with self.conn.execute(
            "SELECT * FROM project WHERE project_id = ?", (project_id,)
        ) as cursor:
            project_info = await cursor.fetchone()

        async with self.conn.execute(
            """SELECT experiment.*, substrate.materials, substrate.orientation
            FROM experiment
            JOIN substrate ON experiment.substrate_id = substrate.substrate_id
            WHERE experiment.project_id = ?""",
            (project_id,),
        ) as cursor:
            experiments = await cursor.fetchall()

        return project_info, experiments

    async def get_projects(self):
        async with self.conn.execute("SELECT * FROM project") as cursor:
            return await cursor.fetchall()

    # --- substrate -------------------------------------------------------------

    async def add_substrate(
        self,
        materials: str,
        orientation: str,
        width: float,
        height: float,
        thickness: float,
        pixel_spacing: float,
        positions: str | None = None,
        substrate_uuid: str | None = None,
        manufacture: str | None = None,
        manufacture_date: str | None = None,
        substrate_name: str | None = None,
    ) -> int:
        substrate_uuid = substrate_uuid or str(uuid4())
        async with self.conn.execute(
            """INSERT INTO substrate
            (materials, orientation, width, height, thickness, pixel_spacing,
             positions, substrate_uuid, manufacture, manufacture_date, substrate_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (materials, orientation, width, height, thickness, pixel_spacing,
             positions, substrate_uuid, manufacture, manufacture_date, substrate_name),
        ) as cursor:
            await self.conn.commit()
            return cursor.lastrowid

    async def get_substrates(self):
        async with self.conn.execute("SELECT * FROM substrate") as cursor:
            return await cursor.fetchall()

    async def get_substrate(self, substrate_id: int):
        async with self.conn.execute(
            "SELECT * FROM substrate WHERE substrate_id = ?", (substrate_id,)
        ) as cursor:
            return await cursor.fetchone()

    async def get_substrate_by_name(self, name: str):
        async with self.conn.execute(
            "SELECT * FROM substrate WHERE substrate_name = ?", (name,)
        ) as cursor:
            return await cursor.fetchone()

    # --- experiment / recording --------------------------------------------------

    async def add_experiment(
        self,
        substrate_id: int,
        is_pixel: bool = False,
        pixel_location: int | None = None,
        experiment_uuid: str | None = None,
        project_id: int | None = None,
        temperature: float | None = None,
        pressure: float | None = None,
        laser_power: float | None = None,
        laser_pulse_rate: float | None = None,
        target_material: str | None = None,
        num_pulse: int | None = None,
    ) -> int:
        async with self.conn.execute(
            """INSERT INTO experiment
            (substrate_id, is_pixel, pixel_location, experiment_uuid, project_id,
             temperature, pressure, laser_power, laser_pulse_rate, target_material, num_pulse)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (substrate_id, is_pixel, pixel_location, experiment_uuid, project_id,
             temperature, pressure, laser_power, laser_pulse_rate, target_material, num_pulse),
        ) as cursor:
            await self.conn.commit()
            return cursor.lastrowid

    async def get_used_pixel_indices(self, substrate_id: int) -> set[int]:
        """Which of a substrate's positions have already been grown on.

        `resume_substrate` used to rebuild a Substrate with `current_position_id = 0`
        and an empty accessed set, so resuming a campaign after a node restart handed
        back a spent pixel and deposited on top of it. The history was always here --
        one `experiment` row per growth -- it was just never read back.

        A single-deposition growth (`is_pixel = 0`) writes a NULL `pixel_location` but
        still consumes position 0, which is what `finish_substrate` advances past; it
        counts as used.
        """
        async with self.conn.execute(
            "SELECT is_pixel, pixel_location FROM experiment WHERE substrate_id = ?",
            (substrate_id,),
        ) as cursor:
            rows = await cursor.fetchall()

        used: set[int] = set()
        for is_pixel, pixel_location in rows:
            if is_pixel and pixel_location is not None:
                used.add(int(pixel_location))
            elif not is_pixel:
                used.add(0)
        return used

    async def add_recording(self, experiment_id: int | None, record_name: str, record_uuid: str) -> int:
        async with self.conn.execute(
            "INSERT INTO record (experiment_id, record_name, record_uuid) VALUES (?, ?, ?)",
            (experiment_id, record_name, record_uuid),
        ) as cursor:
            await self.conn.commit()
            return cursor.lastrowid

    async def get_experiment(self, experiment_id: int):
        async with self.conn.execute(
            """SELECT experiment.*, substrate.materials, substrate.orientation,
                   record.record_id, record.record_name
            FROM experiment
            JOIN substrate ON experiment.substrate_id = substrate.substrate_id
            LEFT JOIN record ON experiment.experiment_id = record.experiment_id
            WHERE experiment.experiment_id = ?""",
            (experiment_id,),
        ) as cursor:
            return await cursor.fetchone()

    async def get_experiment_by_uuid(self, experiment_uuid: str):
        async with self.conn.execute(
            """SELECT experiment.*, substrate.materials, substrate.orientation,
                   record.record_id, record.record_name
            FROM experiment
            JOIN substrate ON experiment.substrate_id = substrate.substrate_id
            LEFT JOIN record ON experiment.experiment_id = record.experiment_id
            WHERE experiment.experiment_uuid = ?""",
            (experiment_uuid,),
        ) as cursor:
            return await cursor.fetchone()

    async def get_experiments(self):
        async with self.conn.execute("SELECT * FROM experiment") as cursor:
            return await cursor.fetchall()

    async def get_substrate_experiments(self, substrate_id: int):
        async with self.conn.execute(
            """SELECT experiment.*, substrate.materials, substrate.orientation,
                   record.record_id, record.record_name
            FROM experiment
            JOIN substrate ON experiment.substrate_id = substrate.substrate_id
            LEFT JOIN record ON experiment.experiment_id = record.experiment_id
            WHERE substrate.substrate_id = ?""",
            (substrate_id,),
        ) as cursor:
            return await cursor.fetchall()

    async def get_pixel_experiments(self):
        async with self.conn.execute(
            """SELECT experiment.*, substrate.materials, substrate.orientation,
                   record.record_id, record.record_name
            FROM experiment
            JOIN substrate ON experiment.substrate_id = substrate.substrate_id
            LEFT JOIN record ON experiment.experiment_id = record.experiment_id
            WHERE experiment.is_pixel = TRUE"""
        ) as cursor:
            return await cursor.fetchall()

    async def get_records(self):
        async with self.conn.execute("SELECT * FROM record") as cursor:
            return await cursor.fetchall()

    # --- schema introspection / migration helpers --------------------------------

    # --- sample: what exists ---------------------------------------------------

    async def add_sample(
        self,
        substrate_id: int,
        *,
        kind: str = "position",
        pixel_index: int | None = None,
        position_mm: float | None = None,
        sample_name: str | None = None,
        parent_sample_id: int | None = None,
        state: str = "planned",
        sample_uuid: str | None = None,
        notes: str | None = None,
    ) -> int:
        async with self.conn.execute(
            """INSERT INTO sample
            (sample_uuid, parent_sample_id, substrate_id, kind, pixel_index,
             position_mm, sample_name, state, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (sample_uuid or str(uuid4()), parent_sample_id, substrate_id, kind,
             pixel_index, position_mm, sample_name, state, notes),
        ) as cursor:
            await self.conn.commit()
            return cursor.lastrowid

    async def get_sample(self, sample_id: int):
        async with self.conn.execute(
            "SELECT * FROM sample WHERE sample_id = ?", (sample_id,)
        ) as cursor:
            return await cursor.fetchone()

    async def get_samples_for_substrate(self, substrate_id: int):
        async with self.conn.execute(
            "SELECT * FROM sample WHERE substrate_id = ? ORDER BY pixel_index IS NULL, pixel_index",
            (substrate_id,),
        ) as cursor:
            return await cursor.fetchall()

    async def find_sample(self, substrate_id: int, pixel_index: int | None):
        """The sample row for one growth position. `pixel_index=None` is the
        whole-substrate (single-deposition) sample, so `IS NULL` rather than `= ?`."""
        if pixel_index is None:
            sql = "SELECT * FROM sample WHERE substrate_id = ? AND pixel_index IS NULL"
            args: tuple = (substrate_id,)
        else:
            sql = "SELECT * FROM sample WHERE substrate_id = ? AND pixel_index = ?"
            args = (substrate_id, pixel_index)
        async with self.conn.execute(sql, args) as cursor:
            return await cursor.fetchone()

    async def set_sample_state(self, sample_id: int, state: str) -> None:
        await self.conn.execute(
            "UPDATE sample SET state = ? WHERE sample_id = ?", (state, sample_id)
        )
        await self.conn.commit()

    async def get_layer_stack(self, sample_id: int):
        """The stack on a sample, bottom-up: (seq, material, num_pulse, step_id, at,
        is_dryrun).

        Deliberately a query and not a `layer` table. A stored stack is a second copy
        of something the journal already knows, and the two drift the first time a
        growth fails halfway -- here a layer exists exactly when a deposition step
        succeeded on this sample.

        Dryrun layers are included, not dropped: a dryrun deposits nothing onto the
        physical sample, but it is still a step someone ran and needs to see -- a
        rehearsed recipe silently vanishing from the stack looked like the run itself
        had gone missing. Each layer carries `is_dryrun` so a caller can grey those out
        instead of pretending they never happened.
        """
        async with self.conn.execute(
            """SELECT json_extract(params, '$.target_material'),
                      json_extract(params, '$.num_pulse'),
                      step_id, started_at,
                      COALESCE(json_extract(params, '$.is_dryrun'), 0)
               FROM step
               WHERE sample_id = ? AND kind = 'perform_deposition' AND ok = 1
               ORDER BY started_at""",
            (sample_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            {"seq": i, "material": m, "num_pulse": n, "step_id": sid, "started_at": at,
             "is_dryrun": bool(dry)}
            for i, (m, n, sid, at, dry) in enumerate(rows)
        ]

    # --- chamber session + step journal: what happened -------------------------

    async def start_chamber_session(self, node_instance: str | None = None) -> int:
        async with self.conn.execute(
            "INSERT INTO chamber_session (session_uuid, node_instance) VALUES (?, ?)",
            (str(uuid4()), node_instance),
        ) as cursor:
            await self.conn.commit()
            return cursor.lastrowid

    async def end_chamber_session(self, session_id: int) -> None:
        await self.conn.execute(
            "UPDATE chamber_session SET ended_at = CURRENT_TIMESTAMP WHERE session_id = ?",
            (session_id,),
        )
        await self.conn.commit()

    async def add_step(
        self,
        kind: str,
        *,
        step_uuid: str,
        started_at: float,
        session_id: int | None = None,
        parent_step_id: int | None = None,
        sample_id: int | None = None,
        params: str | None = None,
        actor: str | None = None,
        source: str | None = None,
    ) -> int:
        async with self.conn.execute(
            """INSERT INTO step
            (step_uuid, session_id, parent_step_id, sample_id, kind, params,
             actor, source, started_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (step_uuid, session_id, parent_step_id, sample_id, kind, params,
             actor, source, started_at),
        ) as cursor:
            await self.conn.commit()
            return cursor.lastrowid

    async def finish_step(
        self,
        step_id: int,
        *,
        ok: bool,
        ended_at: float,
        result: str | None = None,
        error: str | None = None,
    ) -> None:
        await self.conn.execute(
            "UPDATE step SET ok = ?, ended_at = ?, result = ?, error = ? WHERE step_id = ?",
            (ok, ended_at, result, error, step_id),
        )
        await self.conn.commit()

    async def get_steps(self, session_id: int | None = None, sample_id: int | None = None):
        where, args = [], []
        if session_id is not None:
            where.append("session_id = ?")
            args.append(session_id)
        if sample_id is not None:
            where.append("sample_id = ?")
            args.append(sample_id)
        sql = "SELECT * FROM step"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY started_at, step_id"
        async with self.conn.execute(sql, tuple(args)) as cursor:
            return await cursor.fetchall()

    # --- measurement: what it measured -----------------------------------------

    async def add_measurement(
        self,
        sample_id: int,
        kind: str,
        *,
        value: float | None = None,
        detail: str | None = None,
        source: str | None = None,
        step_id: int | None = None,
        experiment_id: int | None = None,
        record_id: int | None = None,
    ) -> int:
        async with self.conn.execute(
            """INSERT INTO measurement
            (sample_id, step_id, experiment_id, kind, value, detail, source, record_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (sample_id, step_id, experiment_id, kind, value, detail, source, record_id),
        ) as cursor:
            await self.conn.commit()
            return cursor.lastrowid

    async def get_measurements(self, sample_id: int, kind: str | None = None):
        if kind is None:
            sql, args = "SELECT * FROM measurement WHERE sample_id = ?", (sample_id,)
        else:
            sql = "SELECT * FROM measurement WHERE sample_id = ? AND kind = ?"
            args = (sample_id, kind)
        async with self.conn.execute(sql + " ORDER BY created_at", args) as cursor:
            return await cursor.fetchall()

    async def query_measurements(
        self,
        *,
        sample_id: int | None = None,
        kind: str | None = None,
        substrate_id: int | None = None,
    ):
        where, args = [], []
        if sample_id is not None:
            where.append("m.sample_id = ?")
            args.append(sample_id)
        if kind is not None:
            where.append("m.kind = ?")
            args.append(kind)
        if substrate_id is not None:
            where.append("s.substrate_id = ?")
            args.append(substrate_id)
        sql = "SELECT m.* FROM measurement m JOIN sample s ON s.sample_id = m.sample_id"
        if where:
            sql += " WHERE " + " AND ".join(where)
        async with self.conn.execute(sql + " ORDER BY m.created_at", tuple(args)) as cursor:
            return await cursor.fetchall()

    async def growth_conditions(self, sample_id: int) -> dict:
        """The conditions that produced a sample, for a GP training set.

        Both sources are needed and neither is sufficient. The `experiment` row holds
        the *measured* values a GP is actually regressing on -- the pressure read off
        the gauge, the laser power read off the meter -- which the deposition request
        never carried. The journal step holds what was asked of the hardware, plus the
        material resolved at the time. So the row is the base and the step overlays it;
        a sample grown before the journal existed still gets the row alone.
        """
        conditions: dict = {}

        async with self.conn.execute(
            """SELECT e.temperature, e.pressure, e.laser_power, e.laser_pulse_rate,
                      e.target_material, e.num_pulse
               FROM experiment e
               JOIN sample s ON s.substrate_id = e.substrate_id
               WHERE s.sample_id = ?
                 AND (e.pixel_location IS s.pixel_index OR e.is_pixel = 0)
               ORDER BY e.experiment_created_at DESC LIMIT 1""",
            (sample_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is not None:
            keys = ("temperature", "pressure", "laser_power", "laser_repetition_rate",
                    "target_material", "num_pulse")
            conditions.update({k: v for k, v in zip(keys, row) if v is not None})

        async with self.conn.execute(
            """SELECT params FROM step
               WHERE sample_id = ? AND kind = 'perform_deposition' AND ok = 1
               ORDER BY started_at DESC LIMIT 1""",
            (sample_id,),
        ) as cursor:
            step_row = await cursor.fetchone()
        if step_row and step_row[0]:
            try:
                value = json.loads(step_row[0])
            except (TypeError, ValueError):
                value = None
            if isinstance(value, dict):
                conditions.update({k: v for k, v in value.items() if v is not None})

        return conditions

    async def list_tables(self):
        async with self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ) as cursor:
            return await cursor.fetchall()

    async def get_table_columns(self, table_name: str):
        async with self.conn.execute(f"PRAGMA table_info({table_name})") as cursor:
            return await cursor.fetchall()

    async def add_table_column(self, table_name: str, column_name: str, column_type: str = "TEXT") -> None:
        await self.conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")
        await self.conn.commit()

    async def rename_table_column(self, table_name: str, old_column_name: str, new_column_name: str) -> None:
        await self.conn.execute(
            f"ALTER TABLE {table_name} RENAME COLUMN {old_column_name} TO {new_column_name}"
        )
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None
