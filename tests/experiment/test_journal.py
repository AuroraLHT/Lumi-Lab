"""Sample tracking and the step journal.

T0: no broker, no hardware. The journal writes to a real (temporary) SQLite file --
faking aiosqlite would stub out exactly the part being tested, which is that the rows
land and read back as a chain.
"""

from __future__ import annotations

import json

import pytest

from lumi.contracts.experiment import DRIVER
from lumi.experiment.db import GrowthDB
from lumi.experiment.journal import StepJournal


@pytest.fixture
async def db(tmp_path):
    db = GrowthDB(str(tmp_path / "growth.db"))
    await db.connect()
    await db.create_database()
    yield db
    await db.close()


# --- the contract flag ---------------------------------------------------------


def test_reads_are_not_journaled():
    """Polled reads stay out of the journal: get_current_log alone runs about once a
    second, which would bury the handful of rows that describe a growth."""
    journaled = {op.name for op in DRIVER.ops if op.journal}
    assert "get_current_log" not in journaled
    assert "get_current_pressure" not in journaled
    assert "is_motor_free" not in journaled


def test_world_changing_ops_are_journaled():
    journaled = {op.name for op in DRIVER.ops if op.journal}
    for name in (
        "initiate_heating_laser",   # "when do I start heating up"
        "set_mfc_flow",             # "putting gas in"
        "set_pressure",
        "to_temperature",
        "perform_deposition",
        "move_rheed_to_position",   # alignment, not deposition
        "confirm_laser_power",      # what a person did
    ):
        assert name in journaled, f"{name} should be journaled"


def test_journal_flag_is_not_on_the_wire():
    """Op.journal is server-side policy. If it leaked into the contract hash, toggling
    it would force the frontend's generated client to be regenerated."""
    from lumi.contracts.registry import _op_json

    op = next(o for o in DRIVER.ops if o.journal)
    assert "journal" not in _op_json(op)


# --- the journal ---------------------------------------------------------------


async def test_steps_chain_within_a_session(db):
    journal = StepJournal(db, node_instance="test")
    await journal.open_session()

    first = await journal.begin("initiate_heating_laser")
    await journal.end(first, ok=True)
    second = await journal.begin("to_temperature", params={"temperature": 600.0})
    await journal.end(second, ok=True)

    rows = await db.get_steps(session_id=journal.session_id)
    assert [r[5] for r in rows] == ["initiate_heating_laser", "to_temperature"]
    # step_id, step_uuid, session_id, parent_step_id, sample_id, kind, params, ...
    assert rows[0][3] is None            # first step has no parent
    assert rows[1][3] == rows[0][0]      # second chains onto the first
    assert json.loads(rows[1][6]) == {"temperature": 600.0}


async def test_a_failed_step_records_its_error(db):
    journal = StepJournal(db)
    await journal.open_session()
    step = await journal.begin("to_temperature")
    await journal.end(step, ok=False, error="RuntimeError: heating laser is off")

    row = (await db.get_steps())[0]
    assert row[8] == 0                                    # ok
    assert "heating laser is off" in row[9]               # error


async def test_journal_failure_never_raises(db):
    """A journal that cannot write must not take a growth down with it."""
    await db.close()  # every write from here on fails
    journal = StepJournal(db)
    assert await journal.open_session() is None
    step = await journal.begin("perform_deposition")
    assert step is None
    await journal.end(step, ok=True)  # must not raise

    await db.connect()  # so the fixture's close() has something to close


async def test_actor_is_recorded(db):
    journal = StepJournal(db)
    await journal.open_session()
    await journal.end(
        await journal.begin("set_mfc_flow", actor="ui:hliang16", source="notebook"),
        ok=True,
    )
    row = (await db.get_steps())[0]
    assert row[10] == "ui:hliang16"   # actor
    assert row[11] == "notebook"      # source


# --- samples -------------------------------------------------------------------


async def test_layer_stack_is_derived_from_successful_depositions(db):
    substrate_id = await db.add_substrate("SrTiO3", "(001)", 10, 10, 0.5, None, "[0.0]")
    sample_id = await db.add_sample(substrate_id, pixel_index=0, position_mm=0.0)

    journal = StepJournal(db, sample_resolver=lambda: sample_id)
    await journal.open_session()
    for material, pulses, ok in [("SrRuO3", 300, True), ("La0.7Sr0.3MnO3", 114, True),
                                 ("Hf0.5Zr0.5O2", 500, False)]:
        step = await journal.begin(
            "perform_deposition", params={"target_material": material, "num_pulse": pulses}
        )
        await journal.end(step, ok=ok, error=None if ok else "aborted")

    stack = await db.get_layer_stack(sample_id)
    # The failed HZO layer is not on the sample, because it is not on the sample.
    assert [(l["seq"], l["material"], l["num_pulse"]) for l in stack] == [
        (0, "SrRuO3", 300),
        (1, "La0.7Sr0.3MnO3", 114),
    ]


async def test_measurement_attaches_to_a_sample(db):
    substrate_id = await db.add_substrate("SrTiO3", "(001)", 10, 10, 0.5, None, "[0.0]")
    sample_id = await db.add_sample(substrate_id, pixel_index=0)

    await db.add_measurement(
        sample_id, "rheed_metric", value=1.83,
        detail=json.dumps({"growth": 0.9, "speed": 0.28, "quality": -0.1, "roughness": 0.75}),
        source="analyze_rheed_video v2",
    )
    rows = await db.get_measurements(sample_id, kind="rheed_metric")
    assert len(rows) == 1
    assert rows[0][5] == pytest.approx(1.83)                     # value
    assert json.loads(rows[0][6])["growth"] == pytest.approx(0.9)  # detail


# --- actor propagation ----------------------------------------------------------


async def test_actor_reaches_a_task_started_inside_a_handler(db):
    """The actor is read off the request headers by the dispatch, but a long-running op
    journals itself several frames deeper. The contextvar is what carries it there."""
    import asyncio

    from lumi.base.mq.context import current_actor

    journal = StepJournal(db)
    await journal.open_session()
    recorded: list[int | None] = []

    async def dispatch() -> None:
        current_actor.set("hliang16")           # what MqServer._handle_request does

        async def handler() -> None:            # what _start_task does, deeper down
            recorded.append(await journal.begin("to_temperature", actor=current_actor.get()))

        await asyncio.create_task(handler())

    await dispatch()
    row = (await db.get_steps())[0]
    assert row[10] == "hliang16"


# --- what a live run against the simulator caught -------------------------------


async def test_a_dryrun_deposition_is_not_a_layer(db):
    """A dryrun fires no laser: the step happened, the film did not. Counting it would
    put a layer on a sample that is still bare."""
    substrate_id = await db.add_substrate("SrTiO3", "(001)", 10, 10, 0.5, None, "[0.0]")
    sample_id = await db.add_sample(substrate_id, pixel_index=0)
    journal = StepJournal(db, sample_resolver=lambda: sample_id)
    await journal.open_session()

    for material, dry in [("SrRuO3", True), ("La0.7Sr0.3MnO3", False)]:
        await journal.end(
            await journal.begin("perform_deposition", params={
                "target_material": material, "num_pulse": 100, "is_dryrun": dry,
            }),
            ok=True,
        )

    stack = await db.get_layer_stack(sample_id)
    assert [l["material"] for l in stack] == ["La0.7Sr0.3MnO3"]


async def test_growth_conditions_merge_the_measured_row_and_the_step(db):
    """Neither source is sufficient. The experiment row has the *measured* pressure and
    laser power a GP regresses on, which the deposition request never carried; the step
    has the material resolved at the time. The row is the base, the step overlays it."""
    substrate_id = await db.add_substrate("SrTiO3", "(001)", 10, 10, 0.5, None, "[0.0]")
    sample_id = await db.add_sample(substrate_id, pixel_index=0)
    await db.add_experiment(
        substrate_id=substrate_id, is_pixel=False, temperature=700.0,
        pressure=0.1, laser_power=88.0,
    )
    journal = StepJournal(db, sample_resolver=lambda: sample_id)
    await journal.open_session()
    await journal.end(
        await journal.begin("perform_deposition", params={
            "target_material": "Hf0.5Zr0.5O2", "num_pulse": 500,
            "laser_repetition_rate": 2.78, "is_dryrun": False,
        }),
        ok=True,
    )

    c = await db.growth_conditions(sample_id)
    assert c["temperature"] == pytest.approx(700.0)      # measured, from the row
    assert c["pressure"] == pytest.approx(0.1)
    assert c["laser_power"] == pytest.approx(88.0)
    assert c["target_material"] == "Hf0.5Zr0.5O2"        # resolved, from the step
    assert c["laser_repetition_rate"] == pytest.approx(2.78)
    assert c["num_pulse"] == 500


async def test_growth_conditions_work_without_a_journal(db):
    """A sample grown before the journal existed still has an experiment row."""
    substrate_id = await db.add_substrate("SrTiO3", "(001)", 10, 10, 0.5, None, "[0.0]")
    sample_id = await db.add_sample(substrate_id, pixel_index=0)
    await db.add_experiment(substrate_id=substrate_id, is_pixel=False, temperature=650.0)
    assert (await db.growth_conditions(sample_id))["temperature"] == pytest.approx(650.0)
