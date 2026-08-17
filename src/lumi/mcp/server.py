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
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from starlette.applications import Starlette
from starlette.routing import Route

from lumi.api.db import UserStore
from lumi.base.mq.client import CapabilityClient, RemoteError
from lumi.config import settings
from lumi.contracts import policy
from lumi.contracts.chamber import CHAMBER
from lumi.contracts.experiment import EXPERIMENT
from lumi.contracts.rheed import RHEED
from lumi.contracts.spec import Capability, EquipmentContract, Op
from lumi.mcp.auth import REQUIRED_SCOPE, ROLE_SCOPES, LumiTokenVerifier
from lumi.mcp.frames import binary_content
from lumi.mcp.oauth import LOGIN_PATH, LumiAuthorizationServer
from lumi.mcp.store import OAuthStore

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
        self._oauth_store: OAuthStore | None = None

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

    async def build_http_app(
        self, *, bind_host: str = "127.0.0.1", public_url: str | None = None, oauth: bool = True
    ) -> Starlette:
        """The Starlette app for the streamable-HTTP transport, with bearer-token
        auth wired to the same JWT/user store the browser bridge uses. Always
        requires a valid operator-or-admin token -- unlike the browser side, this
        is not gated by settings.auth.enabled, since the whole point of this
        transport is letting a remote agent reach real equipment control. Run it
        behind a reverse proxy that terminates TLS; this returns a plain-HTTP app.

        With `oauth` on (the default) it is also its own authorization server: an
        unauthenticated client is told where to log in and walks the browser flow
        in `lumi.mcp.oauth`, instead of needing a token minted out of band. Both
        routes end at the same JWT, so `LumiTokenVerifier` below is unchanged and
        a hand-minted token keeps working.

        `public_url` is the address clients reach this server on, and it has to be
        the *external* one -- it is published as the OAuth issuer and baked into
        every URL a client is redirected to, so behind a TLS proxy it is the
        proxy's https:// address, not this process's bind address.
        """
        self._user_store = UserStore()
        await self._user_store.connect()
        verifier = LumiTokenVerifier(self._user_store)

        if not oauth:
            auth_settings = AuthSettings(
                # Not a real OAuth issuer -- we verify pre-issued JWTs from lumi's own
                # login (POST /auth/login), and there is no authorization server here to
                # name. This is only required by AuthSettings' schema; with no
                # auth_server_provider and no resource_server_url, nothing advertises it.
                issuer_url="https://lumi.internal/",
                resource_server_url=None,
                required_scopes=[REQUIRED_SCOPE],
            )
            return self.server.streamable_http_app(
                host=bind_host, auth=auth_settings, token_verifier=verifier
            )

        base = (public_url or f"http://{bind_host}:8100").rstrip("/")
        self._oauth_store = OAuthStore()
        await self._oauth_store.connect()
        provider = LumiAuthorizationServer(user_store=self._user_store, store=self._oauth_store)

        auth_settings = AuthSettings(
            # This server is both the resource server and the authorization server,
            # so both URLs are its own. The issuer is compared as an exact string by
            # RFC 8414 clients -- if a client reports an issuer mismatch, --public-url
            # does not match the address it actually used.
            issuer_url=base,
            resource_server_url=f"{base}/mcp",
            required_scopes=[REQUIRED_SCOPE],
            client_registration_options=ClientRegistrationOptions(
                # MCP hosts register themselves; there is no console here to hand out
                # client ids in advance. Registration alone grants nothing -- a token
                # still requires somebody to log in, and carries their role, not the
                # client's.
                enabled=True,
                valid_scopes=list(ROLE_SCOPES["admin"]),
                default_scopes=[REQUIRED_SCOPE],
            ),
            # Revoking drops the refresh token, which stops silent renewal. The access
            # token is a stateless JWT and expires on its own; see oauth.revoke_token.
            revocation_options=RevocationOptions(enabled=True),
        )
        return self.server.streamable_http_app(
            host=bind_host,
            auth=auth_settings,
            token_verifier=verifier,
            auth_server_provider=provider,
            custom_starlette_routes=[
                Route(LOGIN_PATH, endpoint=provider.handle_login, methods=["GET", "POST"]),
            ],
        )

    async def close(self) -> None:
        for client in self._clients.values():
            await client.stop()
        if self._channel is not None and not self._channel.is_closed:
            await self._channel.close()
        if self._connection is not None and not self._connection.is_closed:
            await self._connection.close()
        if self._user_store is not None:
            await self._user_store.close()
        if self._oauth_store is not None:
            await self._oauth_store.close()

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

        # A binary-codec op answers `(headers, body)`: the model describes the frame
        # and the pixels are the body. Returning only `result[0]` -- which is what this
        # did -- meant `rheed.camera.image` handed the agent a shape and a dtype and
        # threw the image away, and nothing in the reply said a body had existed.
        if isinstance(result, tuple):
            model, payload = result
            content, note = binary_content(payload, str(op.response_codec))
            return types.CallToolResult(
                content=[*content, types.TextContent(type="text", text=f"{note}\n{model.model_dump_json()}")]
            )
        return types.CallToolResult(content=[types.TextContent(type="text", text=result.model_dump_json())])
