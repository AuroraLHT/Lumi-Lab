"""Who did what to the chamber: chamber.pld's activity log.

T0 for the log itself; T1 (the simulator behind a stand-in pld-service, no broker) for
the handler that serves it. MqServer's half -- calling begin/end with the request's
actor -- is the same dispatch the experiment node's step journal uses (tests/mq).
"""

from __future__ import annotations

import asyncio
import json

from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from lumi.base.mq.server import CapabilityServer
from lumi.contracts.chamber import CHAMBER
from lumi.contracts.payloads.common import Empty
from lumi.contracts.payloads.pld import PldActivityQuery
from lumi.pascal.hardware import build_pld_driver, build_sim_pld_service
from lumi.pascal.pld.activity import ActivityLog

pytestmark = pytest.mark.asyncio


async def test_an_entry_runs_then_carries_the_answer_and_is_kept_across_a_restart(tmp_path):
    path = tmp_path / "activity.jsonl"
    log = ActivityLog(path)
    seq = await log.begin("rheed_all_off_gentle", params={}, actor="mcp:hliang16", source="mcp-1")
    (entry,) = log.recent()
    assert entry.running and entry.ok is None and entry.actor == "mcp:hliang16"
    await log.end(seq, ok=True, result={"ok": False, "message": "RHEED interlock tripped"})
    (entry,) = log.recent()
    # ok is the service's answer, not only "no exception".
    assert not entry.running and entry.ok is False and entry.message == "RHEED interlock tripped"
    assert entry.duration_s is not None

    s2 = await log.begin("acquire_control", params={"force": False}, actor="hliang16")
    await log.end(s2, ok=True, result={"granted": True, "controller": "lumi-pascal", "message": ""})
    # A release that went through answers granted=false: the caller no longer holds control.
    s3 = await log.begin("release_control", params={}, actor="hliang16")
    await log.end(s3, ok=True, result={"granted": False, "controller": "", "message": "released"})
    assert log.recent()[-1].ok is True
    s3 = await log.begin("release_control", params={}, actor="hliang16")
    await log.end(s3, ok=True, result={"granted": True, "controller": "lumi-pascal", "message": "still held"})
    assert log.recent()[-1].ok is False
    assert [json.loads(line)["op"] for line in path.read_text().splitlines()] == [
        "rheed_all_off_gentle", "acquire_control", "release_control", "release_control"]

    again = ActivityLog(path)
    assert [(e.seq, e.op, e.ok) for e in again.recent()] == [
        (seq, "rheed_all_off_gentle", False), (s2, "acquire_control", True),
        (s3 - 1, "release_control", True), (s3, "release_control", False)]
    assert again.latest_seq == s3
    assert await again.begin("set_valve", params={}) == s3 + 1  # seq never repeats
    assert [e.seq for e in again.recent(since_seq=s3)] == [s3 + 1]


async def test_a_line_cut_short_by_a_crash_is_skipped(tmp_path):
    path = tmp_path / "activity.jsonl"
    log = ActivityLog(path)
    await log.end(await log.begin("set_valve", params={"valve": "MV10"}), ok=True, result={"ok": True})
    with path.open("a") as f:
        f.write('{"seq": 2, "op": "set_va')
    assert [e.op for e in ActivityLog(path).recent()] == ["set_valve"]


async def test_every_mutating_op_is_journaled_and_no_reading_is():
    ops = {op.name: op for op in CHAMBER.capability("pld").ops}
    reads = {"chamber_state", "recent_events", "recent_activity"}
    assert {n for n, op in ops.items() if not op.journal} == reads


@pytest_asyncio.fixture
async def driver(tmp_path):
    model, service = build_sim_pld_service(time_scale=20.0)
    service.start()
    handler, workers = build_pld_driver(service.address, tmp_path)
    for worker in workers:
        worker.start()
    for _ in range(100):
        if handler.state_monitor.latest is not None:
            break
        await asyncio.sleep(0.05)
    yield handler, tmp_path
    handler.release_on_shutdown()
    for worker in workers:
        worker.stop()
    service.stop()


def _request(op: str, body: dict, actor: str | None):
    msg = MagicMock()
    msg.routing_key = f"test.pld.req.{op}"
    msg.body = json.dumps(body).encode()
    msg.reply_to = "reply-q"
    msg.correlation_id = "cid"
    msg.headers = {"codec": "json", "message_source": "deck-bridge", **({"actor": actor} if actor else {})}
    return msg


async def test_ops_through_the_dispatch_land_with_their_actor_on_the_op_and_the_state(driver):
    handler, folder = driver
    channel = MagicMock()
    channel.default_exchange.publish = AsyncMock()
    # What Node.start builds: the handler's `journal` is the dispatch's journal.
    server = CapabilityServer(CHAMBER.capability("pld"), "test", handler, channel=channel,
                              exchange=MagicMock(), journal=getattr(handler, "journal", None))
    await server._handle_request(_request("set_valve", {"valve": "MV10", "open": True}, "mcp:hliang16"))
    await server._handle_request(_request("chamber_state", {}, "hliang16"))  # a reading: not recorded
    await server._handle_request(_request("set_valve", {"valve": "NOPE", "open": True}, "hliang16"))

    listed = await handler.recent_activity(PldActivityQuery(limit=10))
    agent, person = listed.entries
    assert (agent.op, agent.actor, agent.source, agent.ok) == ("set_valve", "mcp:hliang16", "deck-bridge", True)
    assert agent.params == {"valve": "MV10", "open": True, "wait": True}
    assert (person.actor, person.ok) == ("hliang16", False) and person.message  # the refusal's reason
    assert listed.latest_seq == person.seq
    assert [e.seq for e in (await handler.recent_activity(PldActivityQuery(since_seq=agent.seq))).entries] == [
        person.seq]
    state = await handler.chamber_state(Empty())
    assert [e.seq for e in state.activity] == [agent.seq, person.seq]
    # Beside the growth logs, which the log archive lists by *.csv only.
    assert len((folder / "lumi_pld_activity.jsonl").read_text().splitlines()) == 2
