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
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import aiosqlite

#: How SQLite's CURRENT_TIMESTAMP writes a time: UTC, second resolution, and -- the
#: property the time-window filters lean on -- lexically sortable, so a string
#: comparison in SQL is a chronological one.
_SQLITE_UTC = "%Y-%m-%d %H:%M:%S"


def utc_now() -> str:
    """A CURRENT_TIMESTAMP-shaped string, for the columns a migration added.

    `ALTER TABLE ... ADD COLUMN` will not accept CURRENT_TIMESTAMP as a default (SQLite
    requires a constant there), so a column added to an existing growth.db has no
    default at all. Writing the value explicitly is what keeps a migrated database and
    a freshly created one storing the same thing.
    """
    return datetime.now(timezone.utc).strftime(_SQLITE_UTC)


def to_epoch(value) -> float | None:
    """Any timestamp this schema stores -> epoch seconds, the one form on the wire.

    The schema grew two conventions: `step` holds epoch floats (the journal passes
    `time.time()` straight through), everything else holds CURRENT_TIMESTAMP text. A
    client should not have to know which table a field came from to sort it, so every
    payload carries epoch and this is the single place the two meet.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("T", " ")
    # Trailing 'Z', an offset, or fractional seconds all show up in rows written by
    # hand or by an older tool; parse what we can and give up quietly rather than
    # failing a listing over one malformed cell.
    for fmt in (_SQLITE_UTC, "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text.rstrip("Z"), fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def to_iso(value) -> str | None:
    """The same instant as `to_epoch`, spelled for a human.

    Every timestamped payload carries both. They are two renderings of one stored
    column rather than two stored columns, so there is nothing for them to disagree
    about, and a UI gets a sortable number and a printable string without parsing.
    """
    epoch = to_epoch(value)
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def from_epoch(value: float | None) -> str | None:
    """Epoch seconds -> the stored text form, so a wire-side `since` can be compared
    against a CURRENT_TIMESTAMP column without converting every row."""
    if value is None:
        return None
    return datetime.fromtimestamp(float(value), tz=timezone.utc).strftime(_SQLITE_UTC)


def column(row, name: str, default=None):
    """One column of an aiosqlite.Row by name, tolerating its absence.

    growth.db is migrated in place and a node can be pointed at a database an older
    build wrote, so a column the current payload wants may genuinely not be there. A
    listing that renders the row without it beats one that raises.
    """
    try:
        value = row[name]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


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
        # Rows stay index-addressable (every reader here and in handlers.py predates
        # this and reads positionally) but gain name access, which is what keeps the
        # CRUD readers below from being a wall of magic numbers.
        self.conn.row_factory = aiosqlite.Row

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
                substrate_name TEXT,
                -- 'active' or 'retired'. A substrate record is never deleted: a
                -- mistaken registration is retired, which hides it from listings
                -- while leaving whatever was journalled against it intact.
                state VARCHAR(20) NOT NULL DEFAULT 'active',
                substrate_created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS project (
                project_id INTEGER PRIMARY KEY,
                project_name VARCHAR(100) NOT NULL,
                description TEXT,
                project_created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                state VARCHAR(20) NOT NULL DEFAULT 'active'
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
                state VARCHAR(20) NOT NULL DEFAULT 'active',
                FOREIGN KEY (substrate_id) REFERENCES substrate(substrate_id),
                FOREIGN KEY (project_id) REFERENCES project(project_id)
            );

            CREATE TABLE IF NOT EXISTS record (
                record_id INTEGER PRIMARY KEY,
                record_uuid VARCHAR(100),
                experiment_id INTEGER,
                record_name VARCHAR(100),
                record_created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                state VARCHAR(20) NOT NULL DEFAULT 'active',
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

            -- Calibrations the driver has learned, by name (e.g. center_mask_pos),
            -- so a node restart keeps them rather than falling back to settings.
            -- One row per name, the latest; how it got there is in the step journal.
            CREATE TABLE IF NOT EXISTS calibration (
                name VARCHAR(100) PRIMARY KEY,
                value REAL NOT NULL,
                source VARCHAR(100),
                set_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
                state VARCHAR(20) NOT NULL DEFAULT 'active',
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
        await self._migrate()
        self.logger.info("growth database ready at %s", self.db_path)

    #: Columns added after the schema above first shipped, as
    #: {table: {column: declaration}}. `CREATE TABLE IF NOT EXISTS` is a no-op on a
    #: database that already has the table, so a growth.db opened from an earlier
    #: version would otherwise be missing them and fail on the first read.
    #: `state` carries a constant default, which ALTER TABLE accepts. The `*_created_at`
    #: columns cannot (CURRENT_TIMESTAMP is not constant), so they arrive bare and
    #: `add_substrate`/`add_recording` write the value explicitly -- see utc_now().
    #: Rows that predate the column keep NULL, which reads back as "registered before
    #: this was recorded" rather than as a fabricated date.
    _ADDED_COLUMNS: dict[str, dict[str, str]] = {
        "substrate": {"state": "VARCHAR(20) NOT NULL DEFAULT 'active'",
                      "substrate_created_at": "TIMESTAMP"},
        "project": {"state": "VARCHAR(20) NOT NULL DEFAULT 'active'"},
        "experiment": {"state": "VARCHAR(20) NOT NULL DEFAULT 'active'"},
        "record": {"state": "VARCHAR(20) NOT NULL DEFAULT 'active'",
                   "record_created_at": "TIMESTAMP"},
        "measurement": {"state": "VARCHAR(20) NOT NULL DEFAULT 'active'"},
    }

    async def _migrate(self) -> None:
        """Bring an existing database up to the current schema, idempotently.

        Deliberately additive only: new columns with a default, never a drop or a
        rename. The lab's growth.db is the record of every growth ever run on this
        chamber and there is no restore path, so a migration that can only append is
        worth more than one that can tidy.
        """
        for table, columns in self._ADDED_COLUMNS.items():
            async with self.conn.execute(f"PRAGMA table_info({table})") as cursor:
                existing = {row[1] for row in await cursor.fetchall()}
            if not existing:  # table itself is absent -- create_database just made it
                continue
            for name, declaration in columns.items():
                if name in existing:
                    continue
                self.logger.info("migrating %s: adding column %s", table, name)
                await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
        await self.conn.commit()

    # --- generic CRUD ----------------------------------------------------------
    # One filtered/paged reader, one updater and one soft-remover, shared by every
    # entity a data-management client browses. The alternative -- a hand-written
    # query per table -- is how `ListSteps` ended up with a limit while ListSamples
    # and ListMeasurements had none and nothing had a time window.

    #: table -> (primary key, creation-time column, columns a text `search` scans).
    _TABLES: dict[str, tuple[str, str, tuple[str, ...]]] = {
        "substrate": ("substrate_id", "substrate_created_at",
                      ("substrate_name", "materials", "orientation", "substrate_uuid")),
        "project": ("project_id", "project_created_at", ("project_name", "description")),
        "experiment": ("experiment_id", "experiment_created_at",
                       ("target_material", "experiment_uuid")),
        "record": ("record_id", "record_created_at", ("record_name", "record_uuid")),
        "sample": ("sample_id", "created_at", ("sample_name", "notes", "sample_uuid")),
        "measurement": ("measurement_id", "created_at", ("kind", "source")),
        "step": ("step_id", "started_at", ("kind", "actor", "source")),
    }

    #: Whose creation-time column holds epoch floats rather than CURRENT_TIMESTAMP
    #: text. `step` alone: the journal passes time.time() straight through.
    _EPOCH_TIME_TABLES = frozenset({"step"})

    #: Whose rows carry the 'active'/'retired' soft-delete column. `sample.state` is
    #: deliberately not one of these -- it means planned/active/grown, a growth state,
    #: and overloading it with 'retired' would make "which positions are left" lie.
    _RETIRABLE = frozenset({"substrate", "project", "experiment", "record", "measurement"})

    #: What `update_row` will write, per table. Identity columns (the primary key, the
    #: uuid) are absent on purpose: a correction fixes what a record says, it does not
    #: turn the record into a different one.
    _EDITABLE: dict[str, frozenset[str]] = {
        "substrate": frozenset({
            "materials", "orientation", "height", "width", "thickness", "pixel_spacing",
            "positions", "manufacture", "manufacture_date", "substrate_name", "state",
        }),
        "project": frozenset({"project_name", "description", "state"}),
        "experiment": frozenset({
            "project_id", "temperature", "pressure", "laser_power", "laser_pulse_rate",
            "target_material", "num_pulse", "do_preablation", "preablation_pulse",
            "preablation_frequency", "before_experiment_waittime",
            "after_experiment_waittime", "ramp_rate", "state",
        }),
        "record": frozenset({"record_name", "experiment_id", "state"}),
        "sample": frozenset({"sample_name", "notes", "state", "position_mm"}),
        "measurement": frozenset({"kind", "value", "detail", "source", "state"}),
    }

    def _check_table(self, table: str, *, editable: bool = False) -> tuple[str, str, tuple[str, ...]]:
        if table not in self._TABLES:
            raise ValueError(f"not a listable table: {table!r} (have {sorted(self._TABLES)})")
        if editable and table not in self._EDITABLE:
            # `step` is the journal: append-only by design, so that what happened
            # cannot be quietly rewritten after the fact.
            raise ValueError(f"{table} rows are not editable")
        return self._TABLES[table]

    def _filter_sql(
        self, table: str, *, filters: dict | None, since: float | None, until: float | None,
        search: str | None, include_retired: bool,
    ) -> tuple[str, list]:
        _, time_column, search_columns = self._TABLES[table]
        where: list[str] = []
        args: list = []

        for column, value in (filters or {}).items():
            if value is None:
                continue
            where.append(f"{column} = ?")
            args.append(value)

        if not include_retired and table in self._RETIRABLE:
            # COALESCE, not `state = 'active'`: a row written before the column existed
            # reads back NULL and is still a perfectly good record.
            where.append("COALESCE(state, 'active') != 'retired'")

        # A wire-side window is always epoch; convert it once to whatever this table
        # stores rather than converting every row on the way out.
        as_stored = (lambda v: float(v)) if table in self._EPOCH_TIME_TABLES else from_epoch
        if since is not None:
            where.append(f"{time_column} >= ?")
            args.append(as_stored(since))
        if until is not None:
            where.append(f"{time_column} <= ?")
            args.append(as_stored(until))

        if search and search_columns:
            where.append("(" + " OR ".join(f"{c} LIKE ?" for c in search_columns) + ")")
            args.extend([f"%{search}%"] * len(search_columns))

        return (" WHERE " + " AND ".join(where) if where else ""), args

    async def list_rows(
        self,
        table: str,
        *,
        filters: dict | None = None,
        since: float | None = None,
        until: float | None = None,
        limit: int = 100,
        offset: int = 0,
        order: str = "desc",
        search: str | None = None,
        include_retired: bool = False,
    ):
        """A filtered, paged read of one table. `since`/`until` are epoch seconds."""
        id_column, time_column, _ = self._check_table(table)
        clause, args = self._filter_sql(
            table, filters=filters, since=since, until=until, search=search,
            include_retired=include_retired,
        )
        direction = "DESC" if str(order).lower() != "asc" else "ASC"
        # Tie-break on the primary key so a page boundary is stable: CURRENT_TIMESTAMP
        # is second-resolution and two substrates registered in the same second would
        # otherwise be free to swap places between one page and the next. Rows
        # predating the timestamp column sort NULL, which SQLite puts last under DESC
        # -- the far end of a newest-first listing, which is where they belong.
        sql = (f"SELECT * FROM {table}{clause} "
               f"ORDER BY {time_column} {direction}, {id_column} {direction} LIMIT ? OFFSET ?")
        args.extend([max(1, min(int(limit), 1000)), max(0, int(offset))])
        async with self.conn.execute(sql, tuple(args)) as cursor:
            return await cursor.fetchall()

    async def count_rows(
        self,
        table: str,
        *,
        filters: dict | None = None,
        since: float | None = None,
        until: float | None = None,
        search: str | None = None,
        include_retired: bool = False,
    ) -> int:
        """How many rows the same filters match, ignoring limit/offset -- a pager
        needs the total, and asking for it by fetching everything defeats the point."""
        self._check_table(table)
        clause, args = self._filter_sql(
            table, filters=filters, since=since, until=until, search=search,
            include_retired=include_retired,
        )
        async with self.conn.execute(f"SELECT COUNT(*) FROM {table}{clause}", tuple(args)) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else 0

    async def get_row(self, table: str, row_id: int):
        id_column, _, _ = self._check_table(table)
        async with self.conn.execute(
            f"SELECT * FROM {table} WHERE {id_column} = ?", (row_id,)
        ) as cursor:
            return await cursor.fetchone()

    async def update_row(self, table: str, row_id: int, **fields) -> None:
        """Write the given columns of one row. An unknown column raises rather than
        being dropped: a typo'd field name that silently no-ops looks exactly like a
        correction that was applied."""
        id_column, _, _ = self._check_table(table, editable=True)
        unknown = set(fields) - self._EDITABLE[table]
        if unknown:
            raise ValueError(
                f"not editable on {table}: {sorted(unknown)} "
                f"(editable: {sorted(self._EDITABLE[table])})"
            )
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        await self.conn.execute(
            f"UPDATE {table} SET {assignments} WHERE {id_column} = ?",
            (*fields.values(), row_id),
        )
        await self.conn.commit()

    async def set_row_state(self, table: str, row_id: int, state: str) -> None:
        """Retire or restore a row. The remover, for every entity that has one.

        Nothing here deletes. growth.db is the record of every growth ever run on this
        chamber with no restore path, and a DELETE would orphan the steps, samples and
        measurements pointing at the row. Retiring hides it from listings and leaves
        the history readable.
        """
        if table not in self._RETIRABLE:
            raise ValueError(f"{table} rows cannot be retired (no state column)")
        await self.update_row(table, row_id, state=state)

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
        created_at: str | None = None,
    ) -> int:
        substrate_uuid = substrate_uuid or str(uuid4())
        async with self.conn.execute(
            """INSERT INTO substrate
            (materials, orientation, width, height, thickness, pixel_spacing,
             positions, substrate_uuid, manufacture, manufacture_date, substrate_name,
             substrate_created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (materials, orientation, width, height, thickness, pixel_spacing,
             positions, substrate_uuid, manufacture, manufacture_date, substrate_name,
             created_at or utc_now()),
        ) as cursor:
            await self.conn.commit()
            return cursor.lastrowid

    async def update_substrate(self, substrate_id: int, **fields) -> None:
        return await self.update_row("substrate", substrate_id, **fields)

    async def get_substrates(self, include_retired: bool = False, **query):
        """Registered substrates, oldest first by default so the historical callers
        that indexed into this list are unaffected. `query` takes the same
        limit/offset/since/until/order/search `list_rows` does."""
        query.setdefault("order", "asc")
        query.setdefault("limit", 1000)
        return await self.list_rows("substrate", include_retired=include_retired, **query)

    async def count_substrate_experiments(self, substrate_id: int) -> int:
        """How many growths were run on a substrate. The gate on editing geometry:
        `width`/`pixel_spacing` renumber the positions, and a pixel index that already
        has an experiment row pointing at it cannot be renumbered without lying about
        where that growth happened."""
        async with self.conn.execute(
            "SELECT COUNT(*) FROM experiment WHERE substrate_id = ?", (substrate_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else 0

    async def delete_position_samples(self, substrate_id: int) -> int:
        """Drop a substrate's per-position sample rows, keeping the root.

        Only called from the geometry-edit path, which has already established that no
        experiment row references this substrate -- so these rows are the empty
        placeholders `_materialise_samples` created at registration and nothing points
        at them. The root row carries the substrate's uuid and stays.
        """
        async with self.conn.execute(
            "DELETE FROM sample WHERE substrate_id = ? AND kind != 'substrate'",
            (substrate_id,),
        ) as cursor:
            await self.conn.commit()
            return cursor.rowcount

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

        A retired experiment does not count. Retiring a growth that was recorded by
        mistake is the one way to hand its position back -- `reopen_position` only
        undoes the bookkeeping, and would be overruled here on the next resume if the
        experiment row still stood.
        """
        async with self.conn.execute(
            "SELECT is_pixel, pixel_location FROM experiment "
            "WHERE substrate_id = ? AND COALESCE(state, 'active') != 'retired'",
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
            "INSERT INTO record (experiment_id, record_name, record_uuid, record_created_at)"
            " VALUES (?, ?, ?, ?)",
            (experiment_id, record_name, record_uuid, utc_now()),
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

    # --- calibration: what the driver has learned about the chamber --------------

    async def get_calibration(self, name: str) -> float | None:
        async with self.conn.execute("SELECT value FROM calibration WHERE name = ?", (name,)) as cursor:
            row = await cursor.fetchone()
        return None if row is None else float(row["value"])

    async def set_calibration(self, name: str, value: float, source: str | None = None) -> None:
        await self.conn.execute(
            """INSERT INTO calibration (name, value, source) VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                value = excluded.value, source = excluded.source, set_at = CURRENT_TIMESTAMP""",
            (name, value, source),
        )
        await self.conn.commit()

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
