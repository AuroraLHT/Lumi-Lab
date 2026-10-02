"""What the MCP server hands an agent, and how a call reaches the bus.

Tier T0: no broker. The tool list is computed from the contracts, and a call is
checked against a stand-in client, so these pin the surface -- which ops are tools,
which are not -- and the plumbing between a tool call and `CapabilityClient.call`.
"""

from __future__ import annotations

import base64
import json

import numpy as np
import pytest
from mcp import types

from lumi.contracts.simulation import SIMULATION
from lumi.contracts.storage import STORAGE_NODE
from lumi.mcp.frames import binary_content
from lumi.mcp.server import (
    MAX_TEXT_CHARS,
    LumiMCPServer,
    exposed_ops,
    render_json,
    split_body,
    tool_for,
)


def tools() -> dict[str, types.Tool]:
    return {t.name: t for t in (tool_for(c, cap, op) for c, cap, op in exposed_ops())}


def test_the_three_jobs_each_have_their_tools():
    names = set(tools())
    # Driving a growth.
    for name in ("experiment.driver.perform_deposition", "experiment.driver.to_temperature",
                 "rheed.camera.image", "rheed.integrator.cache", "chamber.log.log",
                 "system.registry.list_nodes"):
        assert name in names
    # The history: growth.db, and every archive op.
    for name in ("experiment.driver.list_samples", "experiment.driver.sample_history"):
        assert name in names
    archive = next(cap for cap in STORAGE_NODE.capabilities if cap.name == "archive")
    assert {f"storage.archive.{op.name}" for op in archive.ops} <= names
    # The simulation, all of it.
    sim = SIMULATION.capabilities[0]
    assert {f"simulation.rheed_sim.{op.name}" for op in sim.ops} <= names


@pytest.mark.parametrize("name", [
    "system.supervisor.spawn",           # starting processes on the lab machines
    "system.supervisor.kill",
    "system.registry.wait_for",          # blocks longer than a call may
    "chamber.mi_mode.register_commands",  # raw script execution on the chamber
    "storage.storage.start_recording",   # use experiment.driver.start_storage
    "rheed.integrator.register",         # the operator's box layout
    "rheed.integrator.remove",
])
def test_what_an_agent_is_not_given(name):
    assert name not in tools()


def test_every_experiment_op_is_a_tool_including_the_upload():
    from lumi.contracts.experiment import EXPERIMENT

    names = set(tools())
    for op in EXPERIMENT.capabilities[0].ops:
        assert f"experiment.driver.{op.name}" in names


def test_reads_are_marked_read_only_and_nothing_else_is():
    t = tools()
    for name in ("simulation.rheed_sim.simulate_rheed_jpeg", "storage.archive.recording_log",
                 "rheed.camera.image", "system.registry.list_nodes"):
        assert t[name].annotations.read_only_hint is True, name
    for name in ("experiment.driver.perform_deposition", "simulation.rheed_sim.save_structure",
                 "simulation.rheed_sim.delete_structure", "rheed.camera.update_camera_config"):
        assert t[name].annotations.read_only_hint is False, name


def test_an_op_without_a_doc_still_says_what_it_belongs_to():
    description = tools()["experiment.driver.get_current_pressure"].description
    assert description.startswith("get_current_pressure (experiment.driver:")


def test_an_upload_takes_its_body_as_text_or_base64():
    schema = tools()["experiment.driver.attach_measurement_file"].input_schema
    assert {"measurement_id", "file_name", "content_base64", "content_text"} <= set(schema["properties"])

    from lumi.contracts.experiment import EXPERIMENT
    op = EXPERIMENT.capabilities[0].op("attach_measurement_file")
    fields, body = split_body(op, {"measurement_id": 3, "file_name": "a.csv", "content_text": "t,R\n1,2\n"})
    assert fields == {"measurement_id": 3, "file_name": "a.csv"}
    assert body == b"t,R\n1,2\n"

    raw = bytes(range(256))
    _, body = split_body(op, {"measurement_id": 3, "file_name": "a.bin",
                              "content_base64": base64.b64encode(raw).decode()})
    assert body == raw

    for bad in ({}, {"content_text": "x", "content_base64": "eA=="}, {"content_base64": "not base64!"}):
        with pytest.raises(ValueError):
            split_body(op, {"measurement_id": 3, "file_name": "a", **bad})


def test_a_small_answer_is_sent_whole():
    from lumi.contracts.payloads.storage import RecordingInfo

    info = RecordingInfo(name="r", size_bytes=1, modified=0.0, frame_times=[0.0, 1.0])
    assert json.loads(render_json(info)) == info.model_dump(mode="json")


def test_an_answer_over_the_limit_shortens_its_lists_and_says_so():
    from lumi.contracts.payloads.storage import RecordingInfo

    times = [float(i) for i in range(20_000)]
    info = RecordingInfo(name="r", size_bytes=1, modified=0.0, frame_times=times, log_columns=["T", "P"])

    text = render_json(info)

    assert len(text) < MAX_TEXT_CHARS + 1000
    note, body = text.split("\n", 1)
    assert "20,000" not in note and "over the" in note and "max_points" in note
    data = json.loads(body)
    assert data["name"] == "r"
    assert data["log_columns"] == ["T", "P"]  # short lists untouched
    elided = data["frame_times"]["elided_list"]
    assert elided["length"] == 20_000
    assert elided["first"][0] == 0.0 and elided["last"][-1] == 19_999.0


def test_a_text_file_comes_back_as_text():
    content, note = binary_content(b"t,R\n1,2\n", "raw")
    assert content[0].type == "text" and content[0].text == "t,R\n1,2\n"
    assert "8 bytes of text" in note

    content, note = binary_content(b"x" * 100, "raw", text_budget=10)
    assert content[0].text == "x" * 10
    assert "cut" in note


class FakeClient:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    async def call(self, op_name, request=None, payload=None, *, actor=None):
        self.calls.append((op_name, request, payload, actor))
        return self.reply


def serve(name, reply) -> tuple[LumiMCPServer, FakeClient]:
    server = LumiMCPServer(host="nowhere")
    client = FakeClient(reply)
    for contract, cap, op in exposed_ops():
        if f"{contract.name}.{cap.name}.{op.name}" == name:
            server._tools[name] = (contract, cap, op, client)
    return server, client


async def test_a_call_is_credited_to_the_agent_and_carries_its_body():
    from lumi.contracts.payloads.experiment import MeasurementFileInfo

    reply = MeasurementFileInfo(file_id=1, measurement_id=3, file_name="a.csv",
                                media_type="text/csv", size_bytes=8, sha256="0")
    server, client = serve("experiment.driver.attach_measurement_file", reply)

    result = await server._on_call_tool(None, types.CallToolRequestParams(
        name="experiment.driver.attach_measurement_file",
        arguments={"measurement_id": 3, "file_name": "a.csv", "content_text": "t,R\n1,2\n"},
    ))

    assert not result.is_error
    (op_name, request, payload, actor), = client.calls
    assert op_name == "attach_measurement_file"
    assert request.measurement_id == 3
    assert payload == b"t,R\n1,2\n"
    # stdio: no token, so the journal says an agent did it rather than nobody.
    assert actor == "mcp"


async def test_a_simulated_pattern_comes_back_as_a_picture():
    from lumi.contracts.payloads.simulation import RheedSimMeta

    meta = RheedSimMeta.model_construct(energy_kev=25.0, spots=[])
    server, _ = serve("simulation.rheed_sim.simulate_rheed", (meta, np.eye(64, dtype=np.float32)))

    result = await server._on_call_tool(None, types.CallToolRequestParams(
        name="simulation.rheed_sim.simulate_rheed", arguments={"structure": {"name": "SrTiO3"}},
    ))

    assert not result.is_error
    assert [c.type for c in result.content] == ["image", "text"]
    assert "float32" in result.content[1].text


async def test_a_snapshot_comes_back_as_a_picture():
    from lumi.contracts.payloads.experiment import SnapshotInfo

    info = SnapshotInfo(snapshot_id=1, camera="rheed", stage="depo_end", width=8, height=8,
                        dtype="uint16", raw_bytes=10, jpeg_bytes=4)
    server, _ = serve("experiment.driver.snapshot_jpeg", (info, b"\xff\xd8\xff\xe0jpeg"))

    result = await server._on_call_tool(None, types.CallToolRequestParams(
        name="experiment.driver.snapshot_jpeg", arguments={"snapshot_id": 1},
    ))

    assert not result.is_error
    assert result.content[0].type == "image"
    assert tools()["experiment.driver.snapshot_jpeg"].annotations.read_only_hint is True
    assert tools()["experiment.driver.take_snapshot"].annotations.read_only_hint is False
