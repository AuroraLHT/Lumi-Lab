"""An op that declares `timeout_s` is waited on for that long, whatever the client's
default. No broker: the exchange swallows the request, so the call can only time out,
and the message says how long it waited."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lumi.base.mq import CapabilityClient
from lumi.contracts import Capability, Kind
from lumi.contracts.payloads.common import Ack, Empty, ServerStateBase
from lumi.contracts.spec import Op


class SilentExchange:
    async def publish(self, message, routing_key):
        return None


def _client(timeout: float) -> CapabilityClient:
    cap = Capability(
        name="driver",
        kind=Kind.RPC,
        state=ServerStateBase,
        ops=(Op("quick", Empty, Ack), Op("move", Empty, Ack, timeout_s=0.3)),
    )
    client = CapabilityClient(cap, "probe", channel=None, exchange=SilentExchange(), timeout=timeout)
    client._reply = SimpleNamespace(queue=SimpleNamespace(name="reply"))
    return client


async def test_an_op_with_its_own_timeout_waits_that_long():
    client = _client(timeout=0.05)
    with pytest.raises(TimeoutError, match=r"quick did not answer within 0\.05s"):
        await client.call("quick")
    with pytest.raises(TimeoutError, match=r"move did not answer within 0\.3s"):
        await client.call("move")


async def test_a_longer_client_timeout_is_not_shortened_by_the_op():
    client = _client(timeout=0.5)
    with pytest.raises(TimeoutError, match=r"move did not answer within 0\.5s"):
        await client.call("move")


def test_the_motion_ops_wait_longer_than_a_revolve():
    from lumi.contracts.experiment import DRIVER, MOTION_TIMEOUT_S

    for name in ("set_target", "rotate_sample_to", "rotate_sample_by", "move_mask_to_position",
                 "move_rheed_to_position", "to_pixel", "to_current_pixel", "begin_set_laser_power",
                 "begin_align_center_mask", "confirm_center_mask", "begin_check_mask_center",
                 "confirm_mask_center"):
        assert DRIVER.op(name).timeout_s == MOTION_TIMEOUT_S, name
