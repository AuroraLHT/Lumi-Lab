"""Shared test fixtures.

Test tiers, selected by marker:

  T0  (no marker)  registry invariants, codec round-trips, dispatch construction,
                   `lumi-codegen --check`. No broker, no hardware. Must stay green
                   on every commit -- this is what CI gates on.
  T1  @broker      real RabbitMQ via testcontainers, driven against the simulated
                   hardware already configured in cfg/settings.toml.
  T2  @hardware    real cameras and chambers. Never runs in CI.

We deliberately do not fake the broker. The behaviours most likely to break in
this refactor -- exclusive vs. named queues, reply_to semantics, prefetch,
competing consumers, message TTL -- are exactly the ones an in-process fake
would stub out.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# --- Legacy quarantine ------------------------------------------------------
# The pre-refactor tests/ tree is a collection of `if __name__ == "__main__"`
# integration scripts that import torch / cv2 / pypylon / mmdet at module scope
# and require live nodes. They are not a suite and cannot be collected. Each
# entry is deleted from this list as its area is ported.
# Pre-refactor `__main__` experiment scripts, unrelated to the messaging layer, kept
# out of collection. (The old-layer tests that imported the now-deleted transport were
# removed outright rather than quarantined.)
collect_ignore = [
    "aio_gather_test.py",
    "fft.py",
    "nodes.py",
    "opencv_live.py",
    "test_pypylon.py",
    "video_compression.py",
]
collect_ignore_glob = [
    "asyncio/*",
    "speed/*",
]


# --- T1: broker -------------------------------------------------------------


@pytest.fixture(scope="session")
def rabbitmq_url() -> str:
    """A live RabbitMQ for @broker tests.

    Honours LUMI_TEST_AMQP_URL if set, so the suite can be pointed at an existing
    broker (useful on the lab machines, where Docker may not be available).
    Otherwise a container is started for the session (~3s) and torn down after.
    """
    if url := os.environ.get("LUMI_TEST_AMQP_URL"):
        yield url
        return

    try:
        from testcontainers.rabbitmq import RabbitMqContainer
    except ImportError:  # pragma: no cover
        pytest.skip("testcontainers not installed; set LUMI_TEST_AMQP_URL to use a live broker")

    try:
        container = RabbitMqContainer("rabbitmq:3.13-alpine")
        container.start()
    except Exception as exc:  # no docker daemon, no image, no network
        pytest.skip(
            f"cannot start a RabbitMQ container ({type(exc).__name__}: {exc}). "
            f"Install/start Docker, or point LUMI_TEST_AMQP_URL at a broker."
        )

    try:
        params = container.get_connection_params()
        yield f"amqp://guest:guest@{params.host}:{params.port}/"
    finally:
        container.stop()


@pytest.fixture
def project_root() -> Path:
    return Path(__file__).resolve().parent.parent
