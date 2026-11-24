"""The integrator's cache: no frames from before a box existed, and less on request.

T0: no broker, no thread; the integrator's generator driven by hand.
"""

from __future__ import annotations

import queue

import numpy as np

from lumi.contracts.payloads.rheed import IntegrationCacheQuery
from lumi.rheed.handlers import IntegratorHandler
from lumi.rheed.integrator import MultiBoxIntegrator, MultiBoxIntegratorConfig


def frame(t: float):
    return np.full((20, 20), t, dtype=np.uint16), {"time": str(t), "uuid": f"u{t}",
                                                     "time_stamp": str(t)}


def integrator(frames: "queue.Queue") -> MultiBoxIntegrator:
    return MultiBoxIntegrator(camera=None, camera_queue=frames, daemon=True,
                              config=MultiBoxIntegratorConfig(idle_time=0.0, output_queue_size=10))


async def test_a_new_box_gets_no_frames_from_before_it():
    frames: queue.Queue = queue.Queue(maxsize=2)
    integ = integrator(frames)
    frames.put(frame(1.0))
    frames.put(frame(2.0))  # the restart's frames, with nobody integrating
    integ._stop_event.set()
    list(integ.yield_integration())  # one pass with no box: dropped
    assert frames.empty()


async def test_the_cache_gives_less_on_request():
    frames: queue.Queue = queue.Queue()
    integ = integrator(frames)
    integ.register_bbox([0, 0, 10, 10], 1000)
    ticks = integ.yield_integration()
    for t in range(1, 11):
        frames.put(frame(float(t)))
        next(ticks)
    handler = IntegratorHandler(integ)

    async def times(**query):
        cache = await handler.cache(IntegrationCacheQuery(bbox_id=1000, **query))
        return [s.header.time for s in cache.samples]

    assert await times() == [float(t) for t in range(1, 11)]
    assert await times(max_points=3) == [8.0, 9.0, 10.0]
    assert await times(since=6.0) == [7.0, 8.0, 9.0, 10.0]
    assert await times(since=6.0, max_points=2) == [9.0, 10.0]
