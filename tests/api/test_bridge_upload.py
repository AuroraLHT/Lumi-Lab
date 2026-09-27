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
