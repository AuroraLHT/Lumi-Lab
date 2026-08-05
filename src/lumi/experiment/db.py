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

            CREATE INDEX IF NOT EXISTS idx_substrate_uuid ON substrate(substrate_uuid);
            CREATE INDEX IF NOT EXISTS idx_experiment_uuid ON experiment(experiment_uuid);
            CREATE INDEX IF NOT EXISTS idx_record_uuid ON record(record_uuid);
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
