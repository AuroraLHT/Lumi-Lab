"""The control plane. Tier T0: no broker.

These tests pin the bug that motivated the rewrite. In the old code
`BaseControlMixin._control_callbacks` was one dict on the base class, and every
decorator was spelled `@BaseControlMixin.register_control_callback(...)`, so all
three server classes wrote into it and clobbered each other.
"""

from __future__ import annotations

import pytest

from lumi.base.mq.control import ControlPlane, ControlResponse, control


class Alpha(ControlPlane):
    @control("only_alpha")
    async def _a(self, body: bytes, headers: dict) -> ControlResponse:
        return ControlResponse.ok(b"alpha")

    @control("shared")
    async def _shared(self, body: bytes, headers: dict) -> ControlResponse:
        return ControlResponse.ok(b"from-alpha")


class Beta(ControlPlane):
    @control("only_beta")
    async def _b(self, body: bytes, headers: dict) -> ControlResponse:
        return ControlResponse.ok(b"beta")

    @control("shared")
    async def _shared(self, body: bytes, headers: dict) -> ControlResponse:
        return ControlResponse.ok(b"from-beta")


class AlphaChild(Alpha):
    @control("shared")
    async def _shared(self, body: bytes, headers: dict) -> ControlResponse:
        return ControlResponse.ok(b"from-child")


def test_verb_tables_are_not_shared_between_classes():
    """The whole point. Two sibling classes must not see each other's verbs."""
    assert Alpha._control_verbs is not Beta._control_verbs
    assert "only_alpha" in Alpha._control_verbs
    assert "only_alpha" not in Beta._control_verbs
    assert "only_beta" in Beta._control_verbs
    assert "only_beta" not in Alpha._control_verbs


async def test_same_verb_resolves_per_class():
    """`shared` used to be registered three times, and the last class defined won
    for every server type."""
    assert (await Alpha().dispatch_control("shared", b"", {})).body == b"from-alpha"
    assert (await Beta().dispatch_control("shared", b"", {})).body == b"from-beta"


async def test_subclass_overrides_the_parent_verb():
    assert (await AlphaChild().dispatch_control("shared", b"", {})).body == b"from-child"
    # ...and still inherits the ones it did not override.
    assert (await AlphaChild().dispatch_control("only_alpha", b"", {})).body == b"alpha"


async def test_unknown_verb_gets_an_answer_not_silence():
    """The old on_control_request published nothing for an unknown verb, so the
    caller sat there until its timeout expired and got an empty message back. That
    is precisely why `shutdown` looked like a slow no-op: no server ever registered
    it -- they registered `terminate`."""
    res = await Alpha().dispatch_control("does_not_exist", b"", {})
    assert res.succ is False
    assert res.error_type == "UnknownControlVerb"
    assert "only_alpha" in res.error_message  # tells you what it *does* know


async def test_a_raising_handler_does_not_kill_the_consumer():
    class Exploding(ControlPlane):
        @control("boom")
        async def _boom(self, body: bytes, headers: dict) -> ControlResponse:
            raise ValueError("kaboom")

    res = await Exploding().dispatch_control("boom", b"", {})
    assert res.succ is False
    assert res.error_type == "ValueError"
    assert "kaboom" in res.error_message


def test_control_verbs_are_discoverable():
    assert Alpha().control_verbs == ("only_alpha", "shared")
