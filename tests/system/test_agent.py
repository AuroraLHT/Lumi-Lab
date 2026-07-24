"""The host agent's security boundary. Tier T0: no broker.

Supervision means a message on the bus can start a process on a lab machine. The
broker currently authenticates with guest:guest, so the agent must assume the bus is
hostile: `node` is a key into a local allowlist, never a command line.

These tests exist to make that boundary hard to erode later.
"""

from __future__ import annotations

import sys

import pytest

from lumi.contracts.payloads.common import Empty
from lumi.contracts.payloads.system import KillRequest, SpawnRequest
from lumi.system.agent import HostAgent, NodeSpec

ALLOWLIST = {
    "rheed": NodeSpec(module="nodes.rheed", allowed_flags=("--src", "--host")),
    "storage": NodeSpec(module="nodes.storage", allowed_flags=("--host",)),
}


@pytest.fixture
def agent() -> HostAgent:
    return HostAgent(ALLOWLIST, host="testhost")


async def test_spawning_something_not_on_the_allowlist_is_refused(agent):
    with pytest.raises(PermissionError, match="not in this host's allowlist"):
        await agent.spawn(SpawnRequest(host="testhost", node="definitely_not_a_node"))


async def test_a_command_string_cannot_be_smuggled_in_as_a_node_name(agent):
    """The single most important test in this file. If `node` were ever treated as a
    command, anyone who can publish to the broker owns the lab machines."""
    for evil in (
        "rm -rf /",
        "python -c 'import os; os.system(\"id\")'",
        "; id",
        "rheed; rm -rf /",
        "../../../bin/sh",
    ):
        with pytest.raises(PermissionError):
            await agent.spawn(SpawnRequest(host="testhost", node=evil))


async def test_unlisted_flags_are_refused(agent):
    with pytest.raises(PermissionError, match="not permitted"):
        await agent.spawn(SpawnRequest(host="testhost", node="rheed", args=["--eval", "evil"]))

    # ...even for a node that IS allowed, and a flag allowed for a *different* node.
    with pytest.raises(PermissionError, match="not permitted"):
        await agent.spawn(SpawnRequest(host="testhost", node="storage", args=["--src", "pylon"]))


async def test_allowed_flags_pass(monkeypatch, agent):
    seen: dict = {}

    class FakePopen:
        def __init__(self, cmd, **kw):
            seen["cmd"] = cmd
            self.pid = 4242

        def poll(self):
            return None

    monkeypatch.setattr("lumi.system.agent.subprocess.Popen", FakePopen)

    await agent.spawn(SpawnRequest(host="testhost", node="rheed", args=["--src", "simcam"]))

    # Resolved from the allowlist to a module invocation -- never a shell string.
    assert seen["cmd"] == [sys.executable, "-m", "nodes.rheed", "--src", "simcam"]


async def test_the_agent_never_uses_a_shell(monkeypatch, agent):
    captured: dict = {}

    class FakePopen:
        def __init__(self, cmd, **kw):
            captured["cmd"] = cmd
            captured["kw"] = kw
            self.pid = 1

        def poll(self):
            return None

    monkeypatch.setattr("lumi.system.agent.subprocess.Popen", FakePopen)
    await agent.spawn(SpawnRequest(host="testhost", node="rheed"))

    assert captured["kw"].get("shell") is not True
    assert isinstance(captured["cmd"], list)  # never a string


async def test_spawning_a_duplicate_instance_is_refused(monkeypatch, agent):
    """Two processes competing for the same camera is a bad afternoon."""

    class FakePopen:
        def __init__(self, cmd, **kw):
            self.pid = 7

        def poll(self):
            return None  # still running

    monkeypatch.setattr("lumi.system.agent.subprocess.Popen", FakePopen)

    await agent.spawn(SpawnRequest(host="testhost", node="rheed", instance_id="fixed"))
    with pytest.raises(RuntimeError, match="already running"):
        await agent.spawn(SpawnRequest(host="testhost", node="rheed", instance_id="fixed"))


async def test_killing_an_unknown_instance_is_an_error(agent):
    with pytest.raises(KeyError):
        await agent.kill(KillRequest(instance_id="never-existed"))


async def test_kill_sends_sigterm_first_so_the_node_can_drain(monkeypatch, agent):
    """SIGKILL would skip drain(), so the node would never announce its departure and
    the monitor would report a crash for what was a deliberate stop."""
    import signal as signal_mod

    events: list[str] = []

    class FakePopen:
        pid = 9

        def __init__(self, *a, **kw):
            self._alive = True

        def poll(self):
            return None if self._alive else 0

        def send_signal(self, sig):
            events.append(f"signal:{sig}")
            self._alive = False  # well-behaved node exits on SIGTERM

        def kill(self):
            events.append("kill")

    monkeypatch.setattr("lumi.system.agent.subprocess.Popen", FakePopen)
    await agent.spawn(SpawnRequest(host="testhost", node="rheed", instance_id="i1"))
    await agent.kill(KillRequest(instance_id="i1", grace_s=2.0))

    assert events == [f"signal:{signal_mod.SIGTERM}"]
    assert "kill" not in events


async def test_a_node_that_ignores_sigterm_is_eventually_killed(monkeypatch, agent):
    events: list[str] = []

    class StubbornPopen:
        pid = 10

        def __init__(self, *a, **kw):
            pass

        def poll(self):
            return None  # never exits

        def send_signal(self, sig):
            events.append("sigterm")

        def kill(self):
            events.append("kill")

    monkeypatch.setattr("lumi.system.agent.subprocess.Popen", StubbornPopen)
    await agent.spawn(SpawnRequest(host="testhost", node="rheed", instance_id="i2"))
    await agent.kill(KillRequest(instance_id="i2", grace_s=0.3))

    assert events == ["sigterm", "kill"]


async def test_state_reports_what_this_host_may_run(agent):
    state = agent.readout()
    assert state.host == "testhost"
    assert state.available_nodes == ["rheed", "storage"]
    assert state.n_processes == 0
