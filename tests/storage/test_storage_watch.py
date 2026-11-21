"""What the storage node notices on its own while recording: pld events (all of them,
by time), the RHEED gun's position, a last look at stop, and no log backlog.

T0: no broker; fake sources, the real RecordingWriter.
"""

from __future__ import annotations

import time

from lumi.contracts.payloads.chamber import LogEntry
from lumi.contracts.payloads.common import Empty
from lumi.contracts.payloads.pld import PldChamberState, PldEvent, PldEventList, PldMotion, PldRheed
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
