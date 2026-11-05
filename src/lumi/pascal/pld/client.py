"""A gRPC client for pld-service, the chamber's control service on the PLD PC.

Adapted from the REA repo's reference client (`src/rea/client.py`), with two changes
for running inside a node:

- **Refusals are answers.** An operation returns the service's `OperationReply` as
  `(ok, message)` rather than raising: the bus op hands it back as `PldReply`, and the
  caller decides. The service enforces every safety rule; nothing here second-guesses it.
- **Control is taken on demand.** pld-service only lets the holder of control operate,
  and answers anyone else with FAILED_PRECONDITION. The first operation that gets that
  takes control (never with `force`) and retries once, so a node restart, or the GUI
  having released, needs no extra step. If another client holds control, the refusal
  says so.

The stubs are generated from the vendored proto (`proto/pld.proto`,
scripts/gen_pld_stubs.sh). The service listens on loopback only, by design -- the
Pascal node runs on the same PC and is how the rest of the lab reaches it.
"""

from __future__ import annotations

import logging
import math
import threading

import grpc

from lumi.pascal.pld.proto import pld_pb2 as pb
from lumi.pascal.pld.proto import pld_pb2_grpc as pb_grpc

log = logging.getLogger(__name__)

DEFAULT_ADDRESS = "127.0.0.1:50051"
#: Who this is in the service log and in control ownership. Sent as `x-client` on every
#: call and as `ControlRequest.client`.
CLIENT_NAME = "lumi-pascal"


class PldUnavailable(RuntimeError):
    """pld-service did not answer (not running, wrong address, or the call failed)."""


class PldClient:
    """One channel to pld-service. Thread-safe: gRPC stubs may be shared across
    threads, and the node calls this from its worker threads."""

    def __init__(self, address: str = DEFAULT_ADDRESS, client: str = CLIENT_NAME) -> None:
        self.address = address
        self.client = client
        self._metadata = (("x-client", client),)
        self._channel = grpc.insecure_channel(address)
        self._stub = pb_grpc.ChamberStub(self._channel)
        self._control_lock = threading.Lock()

    def close(self) -> None:
        self._channel.close()

    def wait_ready(self, timeout: float) -> bool:
        try:
            grpc.channel_ready_future(self._channel).result(timeout=timeout)
            return True
        except grpc.FutureTimeoutError:
            return False

    # --- state --------------------------------------------------------------------

    def state(self, timeout: float = 5.0) -> pb.ChamberState:
        try:
            return self._stub.GetState(pb.Empty(), timeout=timeout)
        except grpc.RpcError as e:
            raise PldUnavailable(f"pld-service at {self.address}: {_describe(e)}") from None

    def watch(self, rate_hz: float = 1.0):
        """`ChamberState` snapshots; the stream is an iterator, `.cancel()` ends it."""
        return self._stub.WatchState(pb.WatchRequest(rate_hz=rate_hz))

    def events(self):
        """Discrete `Event`s in order; `.cancel()` ends the stream."""
        return self._stub.WatchEvents(pb.Empty())

    # --- control ------------------------------------------------------------------

    def acquire(self, force: bool = False) -> pb.ControlReply:
        try:
            return self._stub.AcquireControl(
                pb.ControlRequest(client=self.client, force=force), metadata=self._metadata, timeout=10.0)
        except grpc.RpcError as e:
            raise PldUnavailable(f"AcquireControl: {_describe(e)}") from None

    def release(self) -> pb.ControlReply:
        try:
            return self._stub.ReleaseControl(
                pb.ControlRequest(client=self.client), metadata=self._metadata, timeout=10.0)
        except grpc.RpcError as e:
            raise PldUnavailable(f"ReleaseControl: {_describe(e)}") from None

    # --- operations ---------------------------------------------------------------

    def call(self, rpc: str, request) -> tuple[bool, str]:
        """Run one operation: `(ok, message)`. Takes control first if the service says
        this client does not hold it. Raises PldUnavailable only when the service could
        not be reached or failed; a refusal is `ok=False`."""
        try:
            return self._call(rpc, request)
        except grpc.RpcError as e:
            if e.code() is not grpc.StatusCode.FAILED_PRECONDITION:
                return self._failed(rpc, e)
        # Not the controller. Take control -- never by force -- and try once more.
        with self._control_lock:
            reply = self.acquire()
        if not reply.granted:
            holder = f" (held by {reply.controller})" if reply.controller else ""
            return False, f"{rpc}: control not granted{holder}: {reply.message}".rstrip(": ")
        log.info("pld-service: took control as %r", self.client)
        try:
            return self._call(rpc, request)
        except grpc.RpcError as e:
            return self._failed(rpc, e)

    def _call(self, rpc: str, request) -> tuple[bool, str]:
        reply = getattr(self._stub, rpc)(request, metadata=self._metadata)
        return bool(reply.ok), reply.message

    def _failed(self, rpc: str, e: grpc.RpcError) -> tuple[bool, str]:
        # The service's own "no": a precondition or a bad argument, with a reason.
        if e.code() in (grpc.StatusCode.FAILED_PRECONDITION, grpc.StatusCode.INVALID_ARGUMENT):
            return False, f"{rpc}: {e.details()}"
        raise PldUnavailable(f"{rpc}: {_describe(e)}") from None


def _describe(e: grpc.RpcError) -> str:
    try:
        return f"{e.code().name}: {e.details()}"
    except Exception:  # a non-call RpcError
        return str(e)


def finite(value: float) -> float | None:
    """A reading as the bus carries it: NaN (out of range, warming up) -> None."""
    return value if math.isfinite(value) else None
