"""MCP server exposing the `experiment` node's ops as tools for an LLM agent.

Generic, not hand-written per op: one Tool per (contract, capability, op), with the
op's own Pydantic request model supplying the tool's input schema directly
(`op.request.model_json_schema()`) -- a new op added to contracts/experiment.py
shows up as a tool with no code change here, the same guarantee `lumi-codegen
--check` already enforces for the Python/TS clients. This mirrors
`lumi.api.bridge.BusProxy`'s "resolve target + call by name" dispatch, just MCP-
framed instead of websocket-framed.

Deliberately narrower than the browser bridge's fully generic REGISTRY iteration:
only the `experiment` contract's ops, plus two read-only capabilities (RHEED's
camera, the chamber's log) for situational awareness. An LLM agent operating real
lab equipment is a more dangerous surface than a browser viewer behind a role check;
there is no reason to hand it e.g. system.supervisor.spawn/kill just because the
generic pattern would technically allow it.
"""

from __future__ import annotations

import logging

from aio_pika import ExchangeType, connect_robust
from aio_pika.abc import AbstractChannel, AbstractConnection
from mcp import types
from mcp.server import Server

from lumi.base.mq.client import CapabilityClient, RemoteError
from lumi.config import settings
from lumi.contracts.chamber import CHAMBER
from lumi.contracts.experiment import EXPERIMENT
from lumi.contracts.rheed import RHEED
from lumi.contracts.spec import Capability, EquipmentContract, Op

log = logging.getLogger(__name__)

#: (contract, capability names to expose) -- None means every capability on that
#: contract. Only EXPERIMENT gets that; the others are named explicitly so adding an
#: op to, say, chamber.mi_mode does NOT silently hand the agent raw script execution.
EXPOSED: tuple[tuple[EquipmentContract, tuple[str, ...] | None], ...] = (
    (EXPERIMENT, None),
    (RHEED, ("camera",)),
    (CHAMBER, ("log",)),
)


def _tool_name(contract: str, cap: str, op: str) -> str:
    return f"{contract}.{cap}.{op}"


class ExperimentMCPServer:
    """Owns the AMQP connection and the `mcp.server.Server` instance. `connect()`
    must run before `server` can answer anything -- tools are only known once the
    capability clients exist."""

    def __init__(self, *, host: str | None = None, user: str = "guest", password: str = "guest") -> None:
        self.host = host or settings.rabbitmq.host
        self.user = user
        self.password = password

        self._connection: AbstractConnection | None = None
        self._channel: AbstractChannel | None = None
        self._clients: dict[str, CapabilityClient] = {}
        # tool name -> (capability, op, the client that serves it)
        self._tools: dict[str, tuple[Capability, Op, CapabilityClient]] = {}

        self.server: Server = Server(
            "lumi-experiment",
            version="1.0",
            instructions=(
                "Drive a PLD/RHEED growth through the `experiment` node's contract ops. "
                "Long-running ops (to_temperature, cool_down, perform_preablation, "
                "perform_deposition, anneal) return immediately with a task_id; call "
                "experiment.driver.get_current_log or watch experiment.driver's state "
                "for current_task to clear before assuming the step finished. Ops that "
                "need a human to act or read something physical (laser power, mask "
                "alignment, RHEED gain, a per-pixel visual check) return with "
                "pending_confirmation set; resolve it with the matching "
                "confirm_*/resolve_* tool (or the generic `confirm` tool for a plain "
                "proceed-gate) before continuing."
            ),
            on_list_tools=self._on_list_tools,
            on_call_tool=self._on_call_tool,
        )

    async def connect(self) -> None:
        self._connection = await connect_robust(f"amqp://{self.user}:{self.password}@{self.host}/")
        self._channel = await self._connection.channel()

        for contract, cap_names in EXPOSED:
            exchange = await self._channel.declare_exchange(
                contract.exchange, ExchangeType(contract.exchange_type), durable=True
            )
            for cap in contract.capabilities:
                if cap_names is not None and cap.name not in cap_names:
                    continue
                client = CapabilityClient(cap, contract.name, channel=self._channel, exchange=exchange)
                await client.start()
                self._clients[f"{contract.name}.{cap.name}"] = client
                for op in cap.ops:
                    self._tools[_tool_name(contract.name, cap.name, op.name)] = (cap, op, client)

        log.info("mcp server connected; %d tools from %s", len(self._tools), ", ".join(self._clients))

    async def close(self) -> None:
        for client in self._clients.values():
            await client.stop()
        if self._channel is not None and not self._channel.is_closed:
            await self._channel.close()
        if self._connection is not None and not self._connection.is_closed:
            await self._connection.close()

    # --- MCP handlers --------------------------------------------------------

    async def _on_list_tools(self, ctx, params) -> types.ListToolsResult:
        tools = [
            types.Tool(name=name, description=op.doc or name, input_schema=op.request.model_json_schema())
            for name, (cap, op, client) in self._tools.items()
        ]
        return types.ListToolsResult(tools=tools)

    async def _on_call_tool(self, ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        entry = self._tools.get(params.name)
        if entry is None:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"unknown tool {params.name!r}; known: {sorted(self._tools)}")],
                is_error=True,
            )
        cap, op, client = entry
        try:
            req = op.request.model_validate(params.arguments or {})
            result = await client.call(op.name, req)
        except RemoteError as exc:
            return types.CallToolResult(content=[types.TextContent(type="text", text=str(exc))], is_error=True)
        except Exception as exc:
            log.exception("tool %s failed", params.name)
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"{type(exc).__name__}: {exc}")], is_error=True,
            )

        model = result[0] if isinstance(result, tuple) else result
        return types.CallToolResult(content=[types.TextContent(type="text", text=model.model_dump_json())])
