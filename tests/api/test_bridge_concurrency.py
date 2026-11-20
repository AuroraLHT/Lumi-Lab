"""One browser's requests run side by side in the bridge: a slow op does not hold up
the next. No broker: the session is driven with a fake socket and a fake bus client."""

from __future__ import annotations

import asyncio
import json

import pytest

from lumi.api.bridge import BridgeSession, pack, unpack
from lumi.contracts.chamber import PLD
from lumi.contracts.payloads.pld import PldChamberState, PldReply

pytestmark = pytest.mark.asyncio


class FakeSocket:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.sent: list[dict] = []

    async def send_bytes(self, frame: bytes) -> None:
        self.sent.append(unpack(frame)[0])

    async def iter_bytes(self):
        while (frame := await self.incoming.get()) is not None:
            yield frame


class SlowFocus:
    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def call(self, op, req, *rest):
        if op == "set_rheed_focus":
            await self.release.wait()
            return PldReply(ok=True, message="focus 100 mA")
        return PldChamberState(backend="TWIN")


class Proxy:
    def __init__(self, client) -> None:
        self.client = client

    def resolve(self, target):
        return "chamber", PLD, self.client


def request(cid: str, op: str, body: dict) -> bytes:
    header = {"kind": "request", "target": "chamber.pld", "op": op, "correlation_id": cid}
    return pack(header, json.dumps(body).encode())


async def test_a_slow_op_does_not_hold_up_the_next_request():
    ws, bus = FakeSocket(), SlowFocus()
    session = BridgeSession(ws=ws, proxy=Proxy(bus), role="operator", user="op")
    runner = asyncio.create_task(session.run())
    await ws.incoming.put(request("slow", "set_rheed_focus", {"ma": 100}))
    await ws.incoming.put(request("quick", "chamber_state", {}))
    for _ in range(100):
        if ws.sent:
            break
        await asyncio.sleep(0.01)
    assert [h["correlation_id"] for h in ws.sent] == ["quick"]  # answered while focus ramps
    bus.release.set()
    for _ in range(100):
        if len(ws.sent) == 2:
            break
        await asyncio.sleep(0.01)
    assert ws.sent[1]["correlation_id"] == "slow" and ws.sent[1]["body"]["message"] == "focus 100 mA"
    await ws.incoming.put(None)
    await runner


async def test_a_closed_socket_cancels_requests_still_running():
    ws, bus = FakeSocket(), SlowFocus()
    session = BridgeSession(ws=ws, proxy=Proxy(bus), role="operator", user="op")
    runner = asyncio.create_task(session.run())
    await ws.incoming.put(request("slow", "set_rheed_focus", {"ma": 100}))
    await asyncio.sleep(0.05)
    (task,) = session._requests
    await ws.incoming.put(None)
    await runner
    await asyncio.sleep(0)
    assert task.cancelled() and not session._requests and not ws.sent
