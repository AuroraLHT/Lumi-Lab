"""Sequences: step lists Lumi stores, a person approves once, and pld-service runs.

T0: the chamber is a fake chamber.pld (test_pld_chamber's), extended with the sequence
status and the per-step events pld-service reports. The real run is
tests/pascal/test_pld_twin.py's and the twin E2E.
"""

from __future__ import annotations

import asyncio
import json

import pytest
import pytest_asyncio

from lumi.base.mq.context import current_actor
from lumi.contracts.payloads.common import Empty
from lumi.contracts.payloads.experiment import (
    GetSequence,
    ListSequences,
    SaveSequence,
    SequenceRef,
    StopDeposition,
)
from lumi.contracts.payloads.pld import PldEvent, PldEventList, PldExcimer, PldSequenceStatus
from lumi.experiment import sequence as seqlib
from lumi.experiment.chamber import PldChamber

from test_pld_chamber import FakePldClient, hot_state, make_handler, wait_task

WEDGE = [
    {"select_target": {"target": "A", "wait": True}},
    {"loop_start": 3},
    {"label": "sweep", "mask": {"position": 20, "wait": False, "speed_mm_s": 0.5}},
    {"fire_laser": {"pulses": 100, "rate_hz": 5, "wait": True}},
    {"wait_axis_idle": {"axis": "Mask1"}},
    {"loop_end": {}},
    {"pause": "check the film"},
]


# --- vetting -------------------------------------------------------------------------


def test_check_counts_operations_with_the_loops_multiplied_out():
    vetted = seqlib.check(WEDGE)
    assert vetted["num_steps"] == 7
    assert vetted["num_operations"] == 1 + 3 * 3 + 1
    assert vetted["fires"] and not vetted["sets_energy"]
    # The hash is of the canonical form: key order does not matter.
    reordered = [dict(reversed(list(s.items()))) for s in WEDGE]
    assert seqlib.check(reordered)["content_hash"] == vetted["content_hash"]


@pytest.mark.parametrize("steps, match", [
    ([{"laser_energy": {"mj": 250}}], "meter_energy"),
    ([{"select_target": {"target": "A"}, "fire_laser": {"pulses": 1}}], "one operation"),
    ([{"teleport": {}}], "unknown operation"),
    ([{"loop_start": 2}, {"wait_s": 1}], "without a loop_end"),
    ([{"loop_end": {}}], "without a loop_start"),
    ([{"meter_energy": {"mj": 0}}], "meter_energy needs"),
    ([{"mask": {"positon": 3}}], "not a pld.proto step list"),
    ([], "1..10000 steps"),
])
def test_check_refuses(steps, match):
    with pytest.raises(ValueError, match=match):
        seqlib.check(steps)


def test_meter_energy_becomes_laser_energy_from_the_ratio():
    steps = [{"meter_energy": {"mj": 120}}, {"wait_s": 1}]
    assert seqlib.resolve(steps, 0.75) == [
        {"label": "120 mJ at the meter", "laser_energy": {"mj": 160.0}}, {"wait_s": 1}]
    with pytest.raises(RuntimeError, match="laser test"):
        seqlib.resolve(steps, None)


# --- the driver ----------------------------------------------------------------------


class SequenceClient(FakePldClient):
    """A chamber.pld whose run_sequence runs `script`: the status pld-service would
    report after each poll, and its per-step events."""

    def __init__(self, state, script=None) -> None:
        super().__init__(state)
        self.events: list[PldEvent] = []
        self.script: list[tuple[PldSequenceStatus, list[PldEvent]]] = script or []
        self.hold = False  # keep reporting "running" until cancelled

    async def call(self, op, req=None):
        if op == "recent_events":
            return PldEventList(events=list(self.events))
        reply = await super().call(op, req)
        if op == "run_sequence" and reply.ok:
            self.state.sequence = PldSequenceStatus(name=req.name, state="running", started=1000.0,
                                                    steps=len(req.steps))
            asyncio.get_running_loop().create_task(self._play())
        if op == "cancel_sequence":
            self.hold = False
            self.state.sequence = self.state.sequence.model_copy(update={"state": "cancelled"})
        return reply

    async def _play(self):
        for status, events in self.script:
            await asyncio.sleep(0.01)
            self.state.sequence = status
            self.events.extend(events)
        while self.hold:
            await asyncio.sleep(0.01)


def finished(name, n=3, total=11):
    return PldSequenceStatus(name=name, state="finished", started=1000.0, steps=n, step=n,
                             done=total, total=total, message=f"{total} operations done")


def step_event(t, k, message):
    return PldEvent(time=t, kind="sequence", message=f"step {k} {message}", detail={"step": str(k)})


@pytest_asyncio.fixture
async def seq(tmp_path, monkeypatch):
    client = SequenceClient(hot_state())
    handler, db = await make_handler(tmp_path, PldChamber(client))
    monkeypatch.setattr(type(handler.manager), "SEQUENCE_POLL_S", 0.01)
    token = current_actor.set("hliang16")
    yield handler, client
    current_actor.reset(token)
    await db.close()


async def save(handler, steps=WEDGE, name="wedge"):
    return await handler.save_sequence(SaveSequence(name=name, steps=steps))


async def test_versions_never_change_and_resaving_keeps_the_approval(seq):
    handler, _ = seq
    v1 = await save(handler)
    assert (v1.version, v1.approved_at, v1.saved_by) == (1, None, "hliang16")
    approved = await handler.approve_sequence(SequenceRef(name="wedge", version=1))
    assert approved.approved_by == "hliang16" and approved.approved_at
    same = await save(handler, [dict(reversed(list(s.items()))) for s in WEDGE])
    assert same.version == 1 and same.approved_by == "hliang16"
    v2 = await save(handler, WEDGE + [{"wait_s": 5}])
    assert v2.version == 2 and v2.approved_at is None
    listed = (await handler.list_sequences(ListSequences())).sequences
    assert [(s.version, s.latest, s.approved_by) for s in listed] == [(2, True, None), (1, False, "hliang16")]
    got = await handler.get_sequence(GetSequence(name="wedge"))
    assert got.version == 2 and got.steps[-1] == {"wait_s": 5} and got.num_operations == 12


async def test_only_a_person_approves(seq):
    handler, _ = seq
    await save(handler)
    for actor in ("mcp:hliang16", None):
        token = current_actor.set(actor)
        try:
            with pytest.raises(PermissionError, match="a person approves"):
                await handler.approve_sequence(SequenceRef(name="wedge", version=1))
        finally:
            current_actor.reset(token)
    with pytest.raises(ValueError, match="look at it again"):
        await handler.approve_sequence(SequenceRef(name="wedge", version=1, content_hash="0" * 16))


async def test_an_unapproved_version_does_not_run(seq):
    handler, client = seq
    await save(handler)
    with pytest.raises(PermissionError, match="not approved"):
        await handler.run_sequence(SequenceRef(name="wedge", version=1))
    await handler.approve_sequence(SequenceRef(name="wedge", version=1))
    await save(handler, WEDGE + [{"wait_s": 5}])  # v2, unapproved: v1 still runs
    with pytest.raises(PermissionError, match="version 2 is not approved"):
        await handler.run_sequence(SequenceRef(name="wedge", version=2))
    assert "run_sequence" not in [op for op, _ in client.calls]


async def test_a_run_is_followed_to_the_end_and_journaled(seq):
    handler, client = seq
    info = await save(handler)
    await handler.approve_sequence(SequenceRef(name="wedge", version=1))
    journaled = []

    class Recorder:
        async def begin(self, kind, *, params=None, **_):
            journaled.append((kind, params or {}))
            return len(journaled)

        async def end(self, step_id, *, ok, result=None, error=None):
            journaled[step_id - 1][1]["ok"] = ok

    handler.journal = Recorder()
    client.events = [PldEvent(time=999.0, kind="sequence", message="step 9 done: an older run")]
    client.script = [
        (PldSequenceStatus(name="wedge v1", state="running", started=1000.0, step=1, steps=7, total=11, done=1),
         [step_event(1000.5, 1, "of 7: target A"), step_event(1001.0, 1, "done: target A")]),
        (finished("wedge v1", 7), [step_event(1001.5, 3, "(loop 1/3) done: Mask1 20"),
                                   PldEvent(time=1001.6, kind="sequence", message="pause asked by gui"),
                                   step_event(1002.0, 4, "(loop 1/3) FAILED: no")]),
    ]
    ack = await handler.run_sequence(SequenceRef(name="wedge", version=1, content_hash=info.content_hash))
    result = await wait_task(handler)
    assert result["ok"] and result["state"] == "finished" and result["done"] == 11
    sent = next(kw for op, kw in client.calls if op == "run_sequence")
    assert sent["name"] == "wedge v1" and sent["wait"] is False and sent["steps"] == json.loads(
        seqlib.check(WEDGE)["steps_json"])
    run = handler.readout().sequence
    assert (run.run_id, run.state, run.done, run.version) == (ack.task_id and run.run_id, "finished", 11, 1)
    # One row per step outcome: not as a step starts, nor the older run's step 9.
    steps = [p for kind, p in journaled if kind == "sequence_step"]
    assert [(p["step"], p["loops"], p["ok"], p["run_id"]) for p in steps] == [
        (1, "", True, run.run_id), (3, "1/3", True, run.run_id), (4, "1/3", False, run.run_id)]
    assert [kind for kind, _ in journaled if kind == "sequence_event"] == ["sequence_event"]
    assert journaled[0][0] == "run_sequence"


async def test_meter_energy_needs_the_laser_test_and_holds_the_excimer(seq):
    handler, client = seq
    steps = [{"meter_energy": {"mj": 120}}, {"fire_laser": {"pulses": 10, "rate_hz": 1, "wait": True}}]
    await save(handler, steps, name="e")
    await handler.approve_sequence(SequenceRef(name="e", version=1))
    with pytest.raises(RuntimeError, match="no laser test today"):
        await handler.run_sequence(SequenceRef(name="e", version=1))
    client.state.excimer = PldExcimer(control=False)
    await handler.growth_db.add_laser_test(tested_at=__import__("time").time(), hv_kv=21.0, pulses=100,
                                           rate_hz=1.0, fired_by="lumi", source_mj=200.0, meter_mj=150.0)
    client.script = [(finished("e v1", 2, 2), [])]
    await handler.run_sequence(SequenceRef(name="e", version=1))
    assert (await wait_task(handler))["ok"]
    calls = [(op, kw) for op, kw in client.calls if op in (
        "take_laser_control", "set_laser_mode", "run_sequence", "release_laser")]
    assert [op for op, _ in calls] == ["take_laser_control", "set_laser_mode", "run_sequence", "release_laser"]
    assert calls[1][1] == {"mode": "energy"}
    assert calls[2][1]["steps"][0] == {"label": "120 mJ at the meter", "laser_energy": {"mj": 160.0}}
    assert handler.manager._laser_power_set is None  # a deposition after it sets its own


async def test_a_failed_step_fails_the_task(seq):
    handler, client = seq
    await save(handler)
    await handler.approve_sequence(SequenceRef(name="wedge", version=1))
    client.script = [(PldSequenceStatus(name="wedge v1", state="failed", started=1000.0, step=2, steps=7,
                                        message="step 2 (sweep): Mask1 move refused"), [])]
    await handler.run_sequence(SequenceRef(name="wedge", version=1))
    result = await wait_task(handler)
    assert not result["ok"] and "Mask1 move refused" in result["error"]


async def test_stop_deposition_cancels_the_run_on_pld_service(seq):
    handler, client = seq
    await save(handler)
    await handler.approve_sequence(SequenceRef(name="wedge", version=1))
    client.hold = True
    await handler.run_sequence(SequenceRef(name="wedge", version=1))
    await asyncio.sleep(0.05)
    stopped = await handler.stop_deposition(StopDeposition(reason="test"))
    assert stopped.task_kind == "run_sequence"
    assert [op for op, _ in client.calls if op in ("stop_fire", "cancel_sequence")][:2] == ["stop_fire", "cancel_sequence"]
    assert handler.readout().sequence.state == "cancelled"


async def test_pause_and_resume_go_to_pld_service(seq):
    handler, client = seq
    await handler.pause_sequence(Empty())
    await handler.resume_sequence(Empty())
    await handler.cancel_sequence(Empty())
    assert [op for op, _ in client.calls] == ["pause_sequence", "resume_sequence", "cancel_sequence"]
