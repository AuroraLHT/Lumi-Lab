"""RHEED analysis handlers: box integration and its STFT.

Both replace an 80-line `if headers["request_type"] == ...` ladder in which every arm
re-spelled the op name three times (once to match, twice more in
`create_response_message(request_type=X, response_type=X)`).

Note the two capabilities have the same four ops (register / remove / bboxes / cache)
because they are the same idea applied to different data. They still get their own
handlers -- but they no longer get their own transport classes.
"""

from __future__ import annotations

import logging
import queue
from collections import deque

from lumi.contracts.payloads.common import Ack
from lumi.contracts.payloads.rheed import (
    BBox,
    BBoxId,
    BBoxList,
    FFTResult,
    IntegrationCache,
    IntegrationHeader,
    IntegrationResult,
    IntegrationSample,
    IntegratorReadout,
    RegisterBBox,
    STFTCache,
    STFTSample,
    STFTReadout,
)

log = logging.getLogger(__name__)


def _bbox_to_model(raw) -> BBox:
    """The integrator stores a bbox as [x1, y1, x2, y2]."""
    x1, y1, x2, y2 = raw
    return BBox(x=int(x1), y=int(y1), width=int(x2 - x1), height=int(y2 - y1))


def _bbox_to_raw(bbox: BBox) -> list[int]:
    return [bbox.x, bbox.y, bbox.x + bbox.width, bbox.y + bbox.height]


class IntegratorHandler:
    def __init__(self, integrator, stream_queue: "queue.Queue | None" = None) -> None:
        self.integrator = integrator
        self.stream_queue = stream_queue

    async def register(self, req: RegisterBBox) -> Ack:
        self.integrator.register_bbox(bbox=_bbox_to_raw(req.bbox), bbox_id=req.bbox_id)
        return Ack()

    async def remove(self, req: BBoxId) -> Ack:
        self.integrator.remove_bbox(req.bbox_id)
        return Ack()

    async def bboxes(self, req) -> BBoxList:
        return BBoxList(
            bboxes={bid: _bbox_to_model(raw) for bid, raw in self.integrator.bboxes.items()}
        )

    async def cache(self, req: BBoxId) -> IntegrationCache:
        cached = self.integrator.get_integration_cache(req.bbox_id)
        if cached is None:
            # A bbox that was never registered is a client error, not a server one.
            raise KeyError(f"bbox {req.bbox_id} is not registered")
        # The cache and the stream carry *different* shapes: the per-bbox cache holds a
        # single IntegrationResult per entry, while the stream yields every registered
        # box for one frame ({bbox_id: result}). Same producer, two payloads -- which is
        # the kind of thing an untyped dict on the wire lets you get wrong for months.
        return IntegrationCache(
            bbox_id=req.bbox_id,
            samples=[
                _sample({req.bbox_id: result}, header) for result, header in cached
            ],
        )

    async def next(self) -> IntegrationSample | None:
        if self.stream_queue is None:
            return None
        try:
            result, header = self.stream_queue.get_nowait()
        except queue.Empty:
            return None
        return _sample(result, header)

    def readout(self) -> IntegratorReadout:
        return IntegratorReadout(
            registered_bboxes=sorted(self.integrator.bboxes),
        )


def _sample(result, header) -> IntegrationSample:
    header = dict(header or {})
    results = {
        int(bid): IntegrationResult(**dict(res))
        for bid, res in (result or {}).items()
    }
    return IntegrationSample(
        header=IntegrationHeader(
            time=float(header.get("time", 0.0)),
            uuid=str(header.get("uuid", "")),
            time_stamp=str(header.get("time_stamp", "")),
            bbox_id=header.get("bbox_id", -1),
            integration_uuid=str(header.get("integration_uuid", "")),
        ),
        results=results,
    )


class STFTHandler:
    """The STFT of a box is computed from that box's *integration* series, so the
    calculator tracks a subset of the integrator's boxes rather than boxes of its own.
    Its API is therefore register_integration/remove_integration keyed on a bbox_id --
    this handler used to call register_bbox/remove_bbox/.bboxes, none of which exist
    on STFTCalculator, so every register raised AttributeError.
    """

    def __init__(self, calculator, stream_queue: "queue.Queue | None" = None) -> None:
        self.calculator = calculator
        self.stream_queue = stream_queue
        #: The calculator emits one dict of {bbox_id: result} per hop, but the contract's
        #: stream is one box per message, so flatten into here and drain one at a time.
        self._pending: "deque[STFTSample]" = deque()

    async def register(self, req: RegisterBBox) -> Ack:
        # register_integration reads integrator.bboxes[bbox_id], so the box has to exist
        # on the integrator first -- otherwise it is a KeyError. Registering it here lets
        # a client subscribe to the STFT of a box directly, as the frontend does.
        integrator = self.calculator.integrator
        if req.bbox_id not in integrator.bboxes:
            integrator.register_bbox(bbox=_bbox_to_raw(req.bbox), bbox_id=req.bbox_id)
        self.calculator.register_integration(req.bbox_id)
        return Ack()

    async def remove(self, req: BBoxId) -> Ack:
        # Only stops the STFT. The underlying integration is left registered: a client
        # may still be plotting its oscillation, and integrator.remove drops it properly.
        self.calculator.remove_integration(req.bbox_id)
        return Ack()

    async def bboxes(self, req) -> BBoxList:
        return BBoxList(
            bboxes={
                bid: _bbox_to_model(raw)
                for bid, raw in self.calculator.registered_integrations.items()
            }
        )

    async def cache(self, req: BBoxId) -> STFTCache:
        cached = self.calculator.get_cache(req.bbox_id)
        if cached is None:
            raise KeyError(f"bbox {req.bbox_id} has no STFT cache")
        return STFTCache(
            bbox_id=req.bbox_id,
            samples=[STFTSample(bbox_id=req.bbox_id, result=FFTResult(**dict(r))) for r in cached],
        )

    async def next(self) -> STFTSample | None:
        if self._pending:
            return self._pending.popleft()
        if self.stream_queue is None:
            return None
        try:
            results, _header = self.stream_queue.get_nowait()
        except queue.Empty:
            return None
        # `results` is {bbox_id: FFTResult} for one hop, and the header carries a
        # stft_uuid rather than a bbox_id -- reading bbox_id off the header always gave
        # -1 and treating the whole dict as a single FFTResult raised on every sample.
        self._pending.extend(
            STFTSample(bbox_id=int(bid), result=FFTResult(**dict(res)))
            for bid, res in (results or {}).items()
        )
        return self._pending.popleft() if self._pending else None

    def readout(self) -> STFTReadout:
        config = self.calculator.config
        return STFTReadout(
            registered_bboxes=sorted(self.calculator.registered_integrations),
            window_size=int(config.window_size),
            time_resolution=float(config.time_resolution),
        )
