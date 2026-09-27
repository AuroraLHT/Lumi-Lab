"""A browser upload through the bridge. T0: no broker -- the bus client is a fake.

The browser sends one frame: the request model in the header as `body`, the file as
the frame's payload. The bridge must hand those on as (model, bytes) untouched rather
than parsing the file as the JSON request, which is what every other op's payload is.
"""

from __future__ import annotations

from lumi.api.bridge import BridgeSession
from lumi.contracts.experiment import EXPERIMENT
from lumi.contracts.payloads.experiment import AttachMeasurementFile, MeasurementFileInfo

FILE = b"\x00\x01binary, not json\xff" * 100


class FakeClient:
    def __init__(self):
        self.calls = []

    async def call(self, op_name, req=None, payload=None):
        self.calls.append((op_name, req, payload))
        return MeasurementFileInfo(file_id=1, measurement_id=req.measurement_id, file_name=req.file_name,
                                   media_type="application/octet-stream", size_bytes=len(payload),
                                   sha256="0" * 64)


async def test_an_upload_passes_the_file_through_as_bytes():
    cap = EXPERIMENT.capability("driver")
    client = FakeClient()
    session = object.__new__(BridgeSession)
    header = {"kind": "request", "target": "experiment.driver", "op": "attach_measurement_file",
              "body": {"measurement_id": 3, "file_name": "scan.raw", "role": "raw"}}

    model, wire = await session._call(cap, client, "attach_measurement_file", FILE, header)

    ((op, req, payload),) = client.calls
    assert op == "attach_measurement_file" and payload == FILE
    assert req == AttachMeasurementFile(measurement_id=3, file_name="scan.raw", role="raw")
    assert wire is None and model.size_bytes == len(FILE)


async def test_an_upload_the_bus_cannot_carry_is_refused_with_a_reply():
    """RabbitMQ answers an oversized publish by closing the channel -- every other
    request on it would die too -- and uvicorn used to drop the frame outright, so the
    browser saw close 1006 and a timeout. The bridge now refuses it, with an answer."""
    from unittest.mock import AsyncMock

    from lumi.api.bridge import max_request_payload_bytes

    session = object.__new__(BridgeSession)
    session._error = AsyncMock()
    session.proxy = None  # never reached: the size check comes first
    header = {"kind": "request", "correlation_id": "c9", "target": "experiment.driver",
              "op": "attach_measurement_file", "body": {"measurement_id": 1, "file_name": "big.raw"}}

    await session._request(header, b"x" * (max_request_payload_bytes() + 1))

    cid, error_type, message = session._error.await_args.args
    assert (cid, error_type) == ("c9", "PayloadTooLarge")
    assert "api.max_request_payload_bytes" in message


def test_the_websocket_admits_frames_the_bridge_can_then_refuse():
    from lumi.api.bridge import max_request_payload_bytes
    from lumi.config import settings

    assert int(settings.api.get("ws_max_bytes", 64 * 1024 * 1024)) > max_request_payload_bytes()
    assert max_request_payload_bytes() < 16 * 1024 * 1024  # RabbitMQ 4's message limit
