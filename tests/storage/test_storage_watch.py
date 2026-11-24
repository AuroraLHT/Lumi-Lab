"""What the storage node notices on its own while recording: pld events (all of them,
by time), the RHEED gun's position, a last look at stop, and no log backlog.

T0: no broker; fake sources, the real RecordingWriter.
"""

from __future__ import annotations

import time

from lumi.contracts.payloads.chamber import LogEntry
from lumi.contracts.payloads.common import Empty
from lumi.contracts.payloads.pld import (PldActivity, PldActivityList, PldChamberState, PldEvent,
                                       PldEventList, PldLaser, PldMotion, PldRheed)
from lumi.contracts.payloads.storage import StorageRequest
from lumi.storage import handlers
from lumi.storage.handlers import StorageHandler
from lumi.storage.reader import open_recording


class FakePld:
    def __init__(self) -> None:
        self.events: list[PldEvent] = []
        self.state = PldChamberState(time=time.time(), motion=PldMotion(rheed_gun_x=0.0),
                                     rheed=PldRheed(present=True, hv_on=True, hv_readback_kv=24.8))

    async def chamber_state(self, *_):
        return self.state

    async def recent_events(self, query):
        return PldEventList(events=self.events[-query.limit:])


class FakePldWithActivity(FakePld):
    def __init__(self) -> None:
        super().__init__()
        self.activity: list[PldActivity] = []

    async def recent_activity(self, query):
        return PldActivityList(entries=self.activity[-query.limit:])


class FakeLog:
    target = "log"

    def __init__(self) -> None:
        self.callback = None

    async def get_state(self):
        return type("S", (), {"columns": ["HT Temp moni", "time"], "is_streaming": True})()

    async def log(self, *_):
        return None

    async def on_log(self, cb):
        self.callback = cb

    async def unsubscribe_stream(self):
        self.callback = None


def event(t, kind="rheed", message="HV 25 kV", **detail):
    return PldEvent(time=t, kind=kind, message=message, detail={k: str(v) for k, v in detail.items()})


async def record(tmp_path, pld, **req):
    handler = StorageHandler({"pld": pld, "log": FakeLog()}, root_folder=str(tmp_path))
    status = await handler.start_recording(StorageRequest(project_name="w", save_frame=False, save_ai=False,
                                                          save_integration=False, **req))
    assert status.ok, status.message
    return handler


async def test_gun_events_and_position_land_and_the_last_look_catches_late_ones(tmp_path):
    pld = FakePld()
    t0 = time.time()
    pld.events = [event(t0 - 60, message="before the recording")]
    handler = await record(tmp_path, pld, save_log=False)
    meta, last = handler._watch_meta, handler._watch_start(handler._watch_meta)
    pld.events += [event(t0 + 1, hv_on="true", hv_kv="25.0"), event(t0 + 2, kind="rheed", focus_ma="")]
    pld.state.motion.rheed_gun_x = -3.0
    await handler._watch_once(meta, last)
    handler._watch_last = last
    # A ramp down that lands after the last regular pass, just before stop.
    pld.events.append(event(t0 + 3, message="RHEED ramped down"))
    assert (await handler.stop_recording(Empty())).ok

    with open_recording(tmp_path / "w.hdf5") as rec:
        ev = rec.events()
        kinds, messages = list(ev.kind), list(ev.message)
        assert "before the recording" not in messages
        assert kinds.count("chamber.rheed") == 3 and "RHEED ramped down" in messages
        assert "rheed_gun_x" in kinds
        assert "chamber_events_gap" not in kinds
        assert rec.attrs["rheed_energy_source"] == "gun" and rec.attrs["rheed_energy_kev"] == 24.8


async def test_a_burst_past_what_pld_keeps_is_flagged(tmp_path, monkeypatch):
    monkeypatch.setattr(handlers, "PLD_EVENTS_LIMIT", 5)
    pld = FakePld()
    handler = await record(tmp_path, pld, save_log=False)
    t0 = time.time()
    pld.events = [event(t0 + i, focus_ma=10 * i) for i in range(1, 9)]  # 8 new, 5 kept
    assert (await handler.stop_recording(Empty())).ok
    with open_recording(tmp_path / "w.hdf5") as rec:
        kinds = list(rec.events().kind)
        assert kinds.count("chamber.rheed") == 5 and "chamber_events_gap" in kinds


async def test_log_rows_from_before_the_start_are_not_kept(tmp_path):
    handler = await record(tmp_path, FakePld(), save_log=True)
    cb = handler.sources["log"].callback
    now = time.time()
    for t, temp in ((now - 30, 1.0), (now - 0.5, 2.0), (now + 1, 3.0)):
        await cb(LogEntry(time=t, time_stamp="s", values={"HT Temp moni": temp}), None)
    assert (await handler.stop_recording(Empty())).ok
    with open_recording(tmp_path / "w.hdf5") as rec:
        assert list(rec.log()["HT Temp moni"]) == [2.0, 3.0]


async def test_a_laser_train_and_the_shutter_are_events_at_their_start(tmp_path):
    """pld says laser_done at a train's end and nothing at its start or at the shutter,
    so a RHEED trace could not be lined up with the pulses."""
    pld = FakePld()
    handler = await record(tmp_path, pld, save_log=False)
    meta, last = handler._watch_meta, handler._watch_start(handler._watch_meta)
    t = time.time()
    pld.state = pld.state.model_copy(update={"time": t, "sample_shutter_away": True})
    pld.state.laser = PldLaser(status="firing", pulses_fired=20, rate_hz=5.0, pulses_requested=300)
    await handler._watch_once(meta, last)
    await handler._watch_once(meta, last)  # still the same train: no second event
    # A short train begun and done between two looks, the shutter back in front.
    pld.state = pld.state.model_copy(update={"time": t + 60, "sample_shutter_away": False})
    pld.state.laser = PldLaser(status="finished", pulses_fired=10, rate_hz=10.0, pulses_requested=10)
    await handler._watch_once(meta, last)
    handler._watch_last = last
    assert (await handler.stop_recording(Empty())).ok

    with open_recording(tmp_path / "w.hdf5") as rec:
        ev = rec.events()
        rows = [(k, round(tm - t, 3), m) for k, tm, m in zip(ev.kind, ev.time, ev.message)
                if k in ("laser_started", "sample_shutter")]
        assert sorted(rows, key=lambda r: r[1]) == [
            ("laser_started", -4.0, "laser train: 300 pulses at 5 Hz, sample shutter open"),
            ("sample_shutter", 0.0, "sample shutter open (deposition)"),
            ("laser_started", 59.0, "laser train: 10 pulses at 10 Hz, sample shutter in front"),
            ("sample_shutter", 60.0, "sample shutter in front"),
        ]


async def test_the_shutter_event_is_when_pld_logged_the_move_not_the_look(tmp_path):
    pld = FakePldWithActivity()
    handler = await record(tmp_path, pld, save_log=False)
    meta, last = handler._watch_meta, handler._watch_start(handler._watch_meta)
    t = time.time()
    await handler._watch_once(meta, last)
    # Opened by the agent 1.7 s before the look; the move took 0.4 s. An older open
    # from before the last look is not this one.
    pld.activity = [
        PldActivity(seq=1, time=t - 600, op="set_sample_shutter", params={"in_front": False}, ok=True),
        PldActivity(seq=2, time=t + 0.3, op="set_sample_shutter", params={"in_front": False}, ok=True,
                    duration_s=0.4, actor="mcp:agent"),
    ]
    pld.state = pld.state.model_copy(update={"time": t + 2.0, "sample_shutter_away": True})
    await handler._watch_once(meta, last)
    # Closed from the REA GUI: not in Lumi's activity, so the look's time.
    pld.state = pld.state.model_copy(update={"time": t + 10.0, "sample_shutter_away": False})
    await handler._watch_once(meta, last)
    handler._watch_last = last
    assert (await handler.stop_recording(Empty())).ok

    with open_recording(tmp_path / "w.hdf5") as rec:
        ev = rec.events()
        rows = [(round(tm - t, 3), d) for k, tm, d in zip(ev.kind, ev.time, ev.detail) if k == "sample_shutter"]
        assert [r[0] for r in rows] == [0.7, 10.0]
        assert (rows[0][1]["time_from"], rows[0][1]["actor"]) == ("pld activity", "mcp:agent")
        assert (rows[1][1]["time_from"], rows[1][1]["within_s"]) == ("look", handlers.WATCH_EVERY_S)


async def test_a_train_lumi_fired_starts_when_pld_took_the_command(tmp_path):
    pld = FakePldWithActivity()
    handler = await record(tmp_path, pld, save_log=False)
    meta, last = handler._watch_meta, handler._watch_start(handler._watch_meta)
    t = time.time()
    await handler._watch_once(meta, last)
    pld.activity = [PldActivity(seq=3, time=t + 0.5, op="fire_laser", params={"pulses": 300, "rate_hz": 5.0},
                                running=True, actor="mcp:agent")]
    pld.state = pld.state.model_copy(update={"time": t + 2.0})
    pld.state.laser = PldLaser(status="firing", pulses_fired=7, rate_hz=5.0, pulses_requested=300)
    await handler._watch_once(meta, last)
    # One from a sequence step: not in Lumi's activity, so counted back as before.
    pld.state = pld.state.model_copy(update={"time": t + 120.0})
    pld.state.laser = PldLaser(status="firing", pulses_fired=10, rate_hz=10.0, pulses_requested=50)
    pld.activity = []
    last["laser"] = ("finished", 300)
    await handler._watch_once(meta, last)
    handler._watch_last = last
    assert (await handler.stop_recording(Empty())).ok

    with open_recording(tmp_path / "w.hdf5") as rec:
        ev = rec.events()
        rows = [(round(tm - t, 3), d) for k, tm, d in zip(ev.kind, ev.time, ev.detail) if k == "laser_started"]
        assert [r[0] for r in rows] == [0.5, 119.0]
        assert (rows[0][1]["time_from"], rows[0][1]["actor"]) == ("pld activity", "mcp:agent")
        assert rows[1][1]["time_from"] == "pulse count"


async def test_the_file_names_its_recording_and_its_project_apart(tmp_path):
    handler = await record(tmp_path, FakePld(), save_log=False,
                           context={"project_name": "HZO-dryrun"})
    assert (await handler.stop_recording(Empty())).ok
    with open_recording(tmp_path / "w.hdf5") as rec:
        request = rec.meta["request"]
        assert (request["recording_name"], request["project_name"]) == ("w", "HZO-dryrun")
