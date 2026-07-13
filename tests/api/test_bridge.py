"""The backend bridge, end to end. Tier T1: needs a broker.

Drives the FastAPI WebSocket with FastAPI's own TestClient (which does the ASGI
handshake properly) against a real RHEED node running on the sim camera. This is the
security-critical path: the bridge must let a viewer read but refuse a mutation, and
must let an operator mutate -- enforced here, in the backend, not at the broker.
"""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.broker


def pack(header: dict, payload: bytes = b"") -> bytes:
    h = json.dumps(header).encode()
    return struct.pack(">I", len(h)) + h + payload


def unpack(frame: bytes) -> tuple[dict, bytes]:
    (n,) = struct.unpack(">I", frame[:4])
    return json.loads(frame[4 : 4 + n]), frame[4 + n :]


@pytest.fixture(scope="module")
def rheed_node(rabbitmq_url):
    """One real RHEED node on the sim camera, shared by every test in this module.

    Module-scoped and poll-based: starting a subprocess node per test (with a fixed
    sleep) made this module take minutes. One node, readiness by polling, fast teardown.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent.parent
    host = rabbitmq_url.split("@")[1].rstrip("/").split(":")[0]
    env = {**os.environ, "PYTHONPATH": str(root)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "nodes.rheed", "--src", "simcam",
         "--host", host, "--instance", "test-bridge-rheed"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    # Poll for the node to be answering rather than sleeping a fixed interval.
    if not _wait_until_answering(rabbitmq_url, proc, timeout=20):
        proc.kill()
        pytest.fail("rheed node never came up")
    yield
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _wait_until_answering(amqp_url: str, proc, timeout: float) -> bool:
    """Block until the node answers an image request, or give up."""
    import asyncio

    from aio_pika import ExchangeType, connect_robust

    from lumi.contracts.payloads.common import Empty
    from lumi.contracts.rheed import RHEED
    from lumi.generated.clients.rheed import RheedCameraClient

    async def probe() -> bool:
        deadline = time.monotonic() + timeout
        conn = await connect_robust(amqp_url)
        try:
            ch = await conn.channel()
            ex = await ch.declare_exchange(RHEED.exchange, ExchangeType.TOPIC, durable=True)
            cam = RheedCameraClient(ch, ex, timeout=2.0)
            await cam.start()
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    return False
                try:
                    await cam.image()
                    return True
                except Exception:
                    await asyncio.sleep(0.5)
            return False
        finally:
            await conn.close()

    return asyncio.run(probe())


@pytest.fixture
def client(rabbitmq_url, monkeypatch):
    """The bridge app, pointed at the test broker, with auth stubbed by ?token=."""
    # Explicit URL override -- reliable regardless of dynaconf caching, unlike setting
    # DYNACONF_RABBITMQ__host after settings may already have been read.
    monkeypatch.setenv("LUMI_AMQP_URL", rabbitmq_url)

    from fastapi.testclient import TestClient

    import lumi.api.main as main
    from lumi.api.auth import Identity

    async def fake_auth(ws):
        token = ws.query_params.get("token")
        return Identity(user=token or "anon", role="operator" if token == "op" else "viewer")

    monkeypatch.setattr(main, "authenticate", fake_auth)
    with TestClient(main.app) as c:  # enters lifespan -> connects the BusProxy
        yield c


def _rpc(ws, target, op, body=None, *, control=False):
    header = {"kind": "request", "target": target, "op": op, "correlation_id": "c1"}
    if control:
        header["control"] = True
    ws.send_bytes(pack(header, json.dumps(body or {}).encode()))
    return unpack(ws.receive_bytes())


def test_viewer_can_read(rheed_node, client):
    with client.websocket_connect("/ws") as ws:
        header, payload = _rpc(ws, "rheed.camera", "image")
        assert header["kind"] == "response"
        assert header["meta"]["shape"] == [540, 720]
        assert len(payload) > 100_000  # the actual frame bytes


def test_viewer_is_refused_a_mutation_by_the_backend(rheed_node, client):
    with client.websocket_connect("/ws") as ws:
        header, _ = _rpc(ws, "rheed.camera", "update_camera_config", {"gain": 100})
        assert header["kind"] == "error"
        assert header["error_type"] == "Forbidden"


def test_viewer_cannot_shut_a_node_down(rheed_node, client):
    with client.websocket_connect("/ws") as ws:
        header, _ = _rpc(ws, "rheed.camera", "shutdown", control=True)
        assert header["kind"] == "error"
        assert header["error_type"] == "Forbidden"


def test_operator_may_mutate(rheed_node, client):
    with client.websocket_connect("/ws?token=op") as ws:
        header, _ = _rpc(ws, "rheed.camera", "update_camera_config", {"gain": 120})
        assert header["kind"] == "response"


def test_viewer_receives_a_live_stream(rheed_node, client):
    with client.websocket_connect("/ws") as ws:
        ws.send_bytes(pack({"kind": "subscribe", "target": "rheed.camera", "stream": "frame"}))
        _rpc(ws, "rheed.camera", "start", control=True)
        frames = 0
        for _ in range(20):
            header, payload = unpack(ws.receive_bytes())
            if header["kind"] == "stream" and len(payload) > 100_000:
                frames += 1
                if frames >= 2:
                    break
        assert frames >= 2, "no binary frames arrived on the stream"


def test_unknown_target_is_an_error_not_a_hang(rheed_node, client):
    with client.websocket_connect("/ws") as ws:
        header, _ = _rpc(ws, "rheed.nonexistent", "image")
        assert header["kind"] == "error"
        assert header["error_type"] == "UnknownTarget"
