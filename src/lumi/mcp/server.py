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
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from starlette.applications import Starlette

from lumi.api.db import UserStore
from lumi.base.mq.client import CapabilityClient, RemoteError
from lumi.config import settings
from lumi.contracts import policy
from lumi.contracts.chamber import CHAMBER
from lumi.contracts.experiment import EXPERIMENT
from lumi.contracts.rheed import RHEED
from lumi.contracts.spec import Capability, EquipmentContract, Op
from lumi.mcp.auth import REQUIRED_SCOPE, LumiTokenVerifier

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
        # tool name -> (contract, capability, op, the client that serves it)
        self._tools: dict[str, tuple[EquipmentContract, Capability, Op, CapabilityClient]] = {}
        # Only created for the HTTP transport (build_http_app) -- stdio needs no auth.
        self._user_store: UserStore | None = None

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
                    self._tools[_tool_name(contract.name, cap.name, op.name)] = (contract, cap, op, client)

        log.info("mcp server connected; %d tools from %s", len(self._tools), ", ".join(self._clients))

    async def build_http_app(self, *, bind_host: str = "127.0.0.1") -> Starlette:
        """The Starlette app for the streamable-HTTP transport, with bearer-token
        auth wired to the same JWT/user store the browser bridge uses. Always
        requires a valid operator-or-admin token -- unlike the browser side, this
        is not gated by settings.auth.enabled, since the whole point of this
        transport is letting a remote agent reach real equipment control. Run it
        behind a reverse proxy that terminates TLS; this returns a plain-HTTP app.
        """
        self._user_store = UserStore()
        await self._user_store.connect()
        verifier = LumiTokenVerifier(self._user_store)

        auth_settings = AuthSettings(
            # Not a real OAuth issuer -- we verify pre-issued JWTs from lumi's own
            # login (POST /auth/login), not an OAuth authorization-code flow. This
            # is only required by AuthSettings' schema; no auth_server_provider is
            # configured, so no /authorize or /token routes are ever created for it.
            issuer_url="https://lumi.internal/",
            resource_server_url=None,
            required_scopes=[REQUIRED_SCOPE],
        )
        return self.server.streamable_http_app(host=bind_host, auth=auth_settings, token_verifier=verifier)

    async def close(self) -> None:
        for client in self._clients.values():
            await client.stop()
        if self._channel is not None and not self._channel.is_closed:
            await self._channel.close()
        if self._connection is not None and not self._connection.is_closed:
            await self._connection.close()
        if self._user_store is not None:
            await self._user_store.close()

    # --- MCP handlers --------------------------------------------------------

    async def _on_list_tools(self, ctx, params) -> types.ListToolsResult:
        tools = [
            types.Tool(name=name, description=op.doc or name, input_schema=op.request.model_json_schema())
            for name, (contract, cap, op, client) in self._tools.items()
        ]
        return types.ListToolsResult(tools=tools)

    async def _on_call_tool(self, ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        entry = self._tools.get(params.name)
        if entry is None:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"unknown tool {params.name!r}; known: {sorted(self._tools)}")],
                is_error=True,
            )
        contract, cap, op, client = entry

        # Populated only under the HTTP transport (see build_http_app); stdio is a
        # local subprocess only you can spawn, same trust level as today's no-auth
        # server. RequireAuthMiddleware already demands REQUIRED_SCOPE before a
        # request reaches here, so this is defense-in-depth consistency with how
        # the browser bridge enforces the same predicate, not the only gate.
        access_token = get_access_token()
        if access_token is not None:
            role = (access_token.claims or {}).get("role")
            routing_key = cap.keys(contract.name).request(op.name)
            if role is None or not policy.permits(role, contract.exchange, routing_key):
                return types.CallToolResult(
                    content=[types.TextContent(type="text", text=f"role {role!r} may not call {params.name!r}")],
                    is_error=True,
                )

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
