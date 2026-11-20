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
from lumi.base.mq.client import RemoteError
from lumi.contracts.payloads.common import Empty
from lumi.contracts.payloads.experiment import (
    GetSequence,
    RegisterProject,
    RegisterSubstrate,
    RunSequence,
    SequenceRecording,
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
    ([{"fire_laser": {"pulses": 10, "rate_hz": 25, "wait": True}}], "at most 20 Hz"),
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
        self.down = False  # pld-service not answering

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

    async def chamber_state(self):
        if self.down:
            raise RemoteError("chamber.pld", "chamber_state", "RuntimeError", "pld-service is not answering")
        return self.state

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
        await handler.run_sequence(RunSequence(name="wedge", version=1, no_sample=True))
    await handler.approve_sequence(SequenceRef(name="wedge", version=1))
    await save(handler, WEDGE + [{"wait_s": 5}])  # v2, unapproved: v1 still runs
    with pytest.raises(PermissionError, match="version 2 is not approved"):
        await handler.run_sequence(RunSequence(name="wedge", version=2, no_sample=True))
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
    ack = await handler.run_sequence(RunSequence(name="wedge", version=1, content_hash=info.content_hash, no_sample=True))
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
        await handler.run_sequence(RunSequence(name="e", version=1, no_sample=True))
    client.state.excimer = PldExcimer(control=False)
    await handler.growth_db.add_laser_test(tested_at=__import__("time").time(), hv_kv=21.0, pulses=100,
                                           rate_hz=1.0, fired_by="lumi", source_mj=200.0, meter_mj=150.0)
    client.script = [(finished("e v1", 2, 2), [])]
    await handler.run_sequence(RunSequence(name="e", version=1, no_sample=True))
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
    await handler.run_sequence(RunSequence(name="wedge", version=1, no_sample=True))
    result = await wait_task(handler)
    assert not result["ok"] and "Mask1 move refused" in result["error"]


async def test_stop_deposition_cancels_the_run_on_pld_service(seq):
    handler, client = seq
    await save(handler)
    await handler.approve_sequence(SequenceRef(name="wedge", version=1))
    client.hold = True
    await handler.run_sequence(RunSequence(name="wedge", version=1, no_sample=True))
    await asyncio.sleep(0.05)
    stopped = await handler.stop_deposition(StopDeposition(reason="test"))
    assert stopped.task_kind == "run_sequence"
    assert [op for op, _ in client.calls if op in ("stop_fire", "cancel_sequence")][:2] == ["stop_fire", "cancel_sequence"]
    assert handler.readout().sequence.state == "cancelled"


async def test_a_short_outage_is_ridden_out(seq, monkeypatch):
    handler, client = seq
    monkeypatch.setattr(type(handler.manager), "SEQUENCE_LOST_S", 1.0)
    await save(handler)
    await handler.approve_sequence(SequenceRef(name="wedge", version=1))
    client.hold = True
    await handler.run_sequence(RunSequence(name="wedge", version=1, no_sample=True))
    await asyncio.sleep(0.05)
    client.down = True
    await asyncio.sleep(0.2)
    assert handler.readout().sequence.state == "running"
    client.down, client.hold = False, False
    client.state.sequence = finished("wedge v1")
    result = await wait_task(handler)
    assert result["ok"], result


async def test_a_long_outage_leaves_the_run_unknown(seq, monkeypatch):
    handler, client = seq
    monkeypatch.setattr(type(handler.manager), "SEQUENCE_LOST_S", 0.1)
    await save(handler)
    await handler.approve_sequence(SequenceRef(name="wedge", version=1))
    client.hold = True
    await handler.run_sequence(RunSequence(name="wedge", version=1, no_sample=True))
    await asyncio.sleep(0.05)
    client.down = True
    result = await wait_task(handler)
    client.hold = False
    assert not result["ok"] and "has not answered" in result["error"]
    run = handler.readout().sequence
    assert run.state == "unknown" and "check the chamber" in run.message


async def test_a_restarted_pld_service_leaves_the_run_unknown(seq):
    handler, client = seq
    await save(handler)
    await handler.approve_sequence(SequenceRef(name="wedge", version=1))
    client.hold = True
    await handler.run_sequence(RunSequence(name="wedge", version=1, no_sample=True))
    await asyncio.sleep(0.05)
    client.state.sequence = PldSequenceStatus()  # a fresh service: no run since its start
    result = await wait_task(handler)
    client.hold = False
    assert not result["ok"] and "no longer shows it" in result["error"]
    assert handler.readout().sequence.state == "unknown"


async def test_pause_and_resume_go_to_pld_service(seq):
    handler, client = seq
    await handler.pause_sequence(Empty())
    await handler.resume_sequence(Empty())
    await handler.cancel_sequence(Empty())
    assert [op for op, _ in client.calls] == ["pause_sequence", "resume_sequence", "cancel_sequence"]


# --- what a run grows: layers, growth records, the recording --------------------------


@pytest_asyncio.fixture
async def loaded(seq):
    """A project and a single-position substrate loaded; slots A = SRO, B = STO."""
    handler, client = seq
    handler.sources["chamber_config"].tg_values.update({"TG1name": "SRO", "TG2name": "STO"})
    handler.manager.target_mapper["B"] = "TG2name"
    await handler.register_project(RegisterProject(project_name="superlattice"))
    await handler.register_substrate(RegisterSubstrate(
        materials="SrTiO3", orientation="(001)", width=5, positions=[0.0], substrate_name="STO-SL-01"))
    client.state.sample_shutter_away = False
    return handler, client


SUPERLATTICE = [
    {"sample_shutter": {"in_front": True}},
    {"select_target": {"target": "B", "wait": True}},
    {"fire_laser": {"pulses": 50, "rate_hz": 5, "wait": True}},   # pre-ablation: shutter in front
    {"sample_shutter": {"in_front": False}},
    {"loop_start": 2},
    {"select_target": {"target": "A", "wait": True}},
    {"fire_laser": {"pulses": 100, "rate_hz": 5, "wait": True}},
    {"fire_laser": {"pulses": 20, "rate_hz": 5, "wait": True}},   # same material: merges
    {"select_target": {"target": "B", "wait": True}},
    {"fire_laser": {"pulses": 30, "rate_hz": 2, "wait": True}},
    {"loop_end": {}},
]


def ended(t, k, outcome="done", message="", loops=""):
    at = f" (loop {loops})" if loops else ""
    return PldEvent(time=t, kind="sequence", message=f"step {k}{at} {outcome}: {message}", detail={"step": str(k)})


def superlattice_events():
    t = iter(range(1001, 2000))
    out = [ended(next(t), 1), ended(next(t), 2), ended(next(t), 3, message="fired 50 of 50 pulses"),
           ended(next(t), 4)]
    for loop in ("1/2", "2/2"):
        out += [ended(next(t), 6, loops=loop), ended(next(t), 7, message="fired 100 of 100 pulses", loops=loop),
                ended(next(t), 8, message="fired 20 of 20 pulses", loops=loop), ended(next(t), 9, loops=loop),
                ended(next(t), 10, message="fired 30 of 30 pulses", loops=loop)]
    return out


async def test_a_superlattice_grows_alternating_layers_with_growth_records(loaded):
    from lumi.contracts.payloads.experiment import ListExperiments, SampleId
    from lumi.experiment.journal import StepJournal

    handler, client = loaded
    handler.journal = StepJournal(handler.growth_db, sample_resolver=handler.current_sample_id)
    await save(handler, SUPERLATTICE, name="SL")
    await handler.approve_sequence(SequenceRef(name="SL", version=1))
    client.script = [(finished("SL v1", 11, 15), superlattice_events())]
    await handler.run_sequence(RunSequence(name="SL", version=1,
                                           record=SequenceRecording(project_name="superlattice")))
    result = await wait_task(handler)
    assert result["ok"], result
    # The pre-ablation (shutter in front) is no layer; 100 + 20 on SRO merge.
    layers = [(layer["target_material"], layer["num_pulse"]) for layer in result["layers"]]
    assert layers == [("SRO", 120), ("STO", 30), ("SRO", 120), ("STO", 30)]
    run = handler.readout().sequence
    assert run.recording_name == result["recording_name"] and run.sample_name.startswith("STO-SL-01")

    rows = (await handler.list_experiments(ListExperiments())).experiments
    assert sorted((e.target_material, e.num_pulse, e.sequence_name, e.sequence_version, e.sequence_run_id)
                  for e in rows) == sorted([("SRO", 120, "SL", 1, run.run_id), ("STO", 30, "SL", 1, run.run_id)] * 2)
    assert all(e.temperature == 300.0 for e in rows)  # hot_state's, read at each layer's first pulse

    sample_id = await handler.current_sample_id()
    sample = await handler.get_sample(SampleId(sample_id=sample_id))
    assert [(layer.material, layer.num_pulse) for layer in sample.layers] == layers

    storage = handler.sources["storage"]
    assert len(storage.requests) == 1  # one recording, from before step 1
    assert any(e.kind == "sequence" and "started" in e.message for e in storage.events)


async def test_a_failed_run_keeps_the_layer_it_fired(loaded):
    handler, client = loaded
    steps = [{"select_target": {"target": "A", "wait": True}}, {"sample_shutter": {"in_front": False}},
             {"fire_laser": {"pulses": 1000, "rate_hz": 10, "wait": True}}]
    await save(handler, steps, name="cut")
    await handler.approve_sequence(SequenceRef(name="cut", version=1))
    client.script = [(PldSequenceStatus(name="cut v1", state="failed", started=1000.0, step=3, steps=3,
                                        message="step 3: stopped by request: fired 27 of 1000 pulses"),
                      [ended(1001, 1), ended(1002, 2),
                       ended(1003, 3, "FAILED", "stopped by request: fired 27 of 1000 pulses, 0 missed")])]
    await handler.run_sequence(RunSequence(name="cut", version=1))
    result = await wait_task(handler)
    assert not result["ok"]
    assert [(layer.target_material, layer.num_pulse) for layer in handler.readout().sequence.layers] == [("SRO", 27)]


async def test_a_run_needs_a_substrate_and_project_unless_rehearsed(seq):
    handler, _ = seq
    await save(handler)
    await handler.approve_sequence(SequenceRef(name="wedge", version=1))
    with pytest.raises(RuntimeError, match="no substrate loaded"):
        await handler.run_sequence(RunSequence(name="wedge", version=1))
    await handler.register_substrate(RegisterSubstrate(materials="SrTiO3", orientation="(001)", width=5,
                                                       positions=[0.0], substrate_name="S1"))
    with pytest.raises(RuntimeError, match="no project"):
        await handler.run_sequence(RunSequence(name="wedge", version=1))


async def test_a_move_during_a_pause_is_journaled(loaded):
    handler, client = loaded
    journaled = []

    class Recorder:
        async def begin(self, kind, *, params=None, **_):
            journaled.append((kind, params or {}))
            return len(journaled)

        async def end(self, *a, **k):
            pass

    handler.journal = Recorder()
    await save(handler, [{"wait_s": 1}, {"pause": "look"}, {"wait_s": 1}], name="p")
    await handler.approve_sequence(SequenceRef(name="p", version=1))
    paused = PldSequenceStatus(name="p v1", state="paused", started=1000.0, step=2, steps=3, message="look")
    running = paused.model_copy(update={"state": "running", "step": 3})
    client.script = [(paused, [])]
    client.hold = True
    await handler.run_sequence(RunSequence(name="p", version=1))
    for _ in range(200):
        if handler.readout().sequence.state == "paused":
            break
        await asyncio.sleep(0.01)
    client.state.motion = client.state.motion.model_copy(update={"sample_angle": 12.0})
    client.state.sequence = running
    await asyncio.sleep(0.1)
    client.hold = False
    client.state.sequence = finished("p v1", 3, 2)
    assert (await wait_task(handler))["ok"]
    moved = [p["moved_during_pause"] for kind, p in journaled if "moved_during_pause" in p]
    assert moved == [{"sample_angle": [350.0, 12.0]}]


def test_the_tracker_prefers_the_events_detail_to_the_text():
    steps = [{"fire_laser": {"pulses": 100, "rate_hz": 5, "wait": True}}]
    tracker = seqlib.RunTracker(steps, target="C", shutter_away=False)
    # pld-service reporting target, shutter and pulses: no step list or message needed.
    did, _ = tracker.ended(1, True, "step 1 done: whatever wording",
                           {"op": "fire_laser", "target": "A", "shutter_away": "true", "pulses_fired": "60"})
    assert did["deposition"] and did["pulses_fired"] == 60 and did["target"] == "A"
    did, closed = tracker.ended(1, True, "", {"op": "fire_laser", "target": "B", "shutter_away": "true",
                                              "pulses_fired": "10"})
    assert closed["target_id"] == "A" and closed["num_pulse"] == 60
    assert tracker.finish()["num_pulse"] == 10
    # At Clear nothing is deposited.
    tracker = seqlib.RunTracker(steps, target="Clear", shutter_away=True)
    assert not tracker.ended(1, True, "fired 100 of 100", {})[0]["deposition"]


async def test_a_second_run_is_refused_and_leaves_the_first_ones_status(seq):
    handler, client = seq
    await save(handler)
    await handler.approve_sequence(SequenceRef(name="wedge", version=1))
    client.hold = True
    await handler.run_sequence(RunSequence(name="wedge", version=1, no_sample=True))
    first = handler.readout().sequence.run_id
    with pytest.raises(RuntimeError, match="still moving"):
        await handler.run_sequence(RunSequence(name="wedge", version=1, no_sample=True))
    assert handler.readout().sequence.run_id == first
    await handler.stop_deposition(StopDeposition(reason="test"))


async def test_a_fire_paused_mid_train_counts_both_parts(loaded):
    # REA's structured detail: the "fire paused" event has that part's pulses_fired,
    # the step's final event the rest.
    handler, client = loaded
    steps = [{"select_target": {"target": "A", "wait": True}}, {"sample_shutter": {"in_front": False}},
             {"fire_laser": {"pulses": 30, "rate_hz": 10, "wait": True}}]
    await save(handler, steps, name="pp")
    await handler.approve_sequence(SequenceRef(name="pp", version=1))
    fire = {"op": "fire_laser", "target": "A", "shutter_away": "true", "step": "3"}
    client.script = [(finished("pp v1", 3, 3), [
        PldEvent(time=1001, kind="sequence", message="step 1 done: TG", detail={"step": "1", "op": "select_target", "ok": "true", "target": "A", "shutter_away": "false"}),
        PldEvent(time=1002, kind="sequence", message="step 2 done: away", detail={"step": "2", "op": "sample_shutter", "ok": "true", "target": "A", "shutter_away": "true"}),
        PldEvent(time=1003, kind="sequence", message="fire paused: fired 9 of 30", detail={**fire, "part": "paused", "pulses_fired": "9"}),
        PldEvent(time=1004, kind="sequence", message="step 3 done: fired 21 of 21", detail={**fire, "ok": "true", "pulses_fired": "21", "pulses_fired_total": "30"}),
    ])]
    await handler.run_sequence(RunSequence(name="pp", version=1))
    assert (await wait_task(handler))["ok"]
    assert [(layer.target_material, layer.num_pulse) for layer in handler.readout().sequence.layers] == [("SRO", 30)]


def test_fires_are_capped_at_20_hz():
    from pydantic import ValidationError

    from lumi.contracts.payloads.experiment import PerformDeposition
    from lumi.contracts.payloads.pld import PldFireRequest
    assert PldFireRequest(pulses=1, rate_hz=20).rate_hz == 20
    with pytest.raises(ValidationError):
        PldFireRequest(pulses=1, rate_hz=20.5)
    with pytest.raises(ValidationError):
        PerformDeposition(num_pulse=1, laser_repetition_rate=50, target_id="A")
