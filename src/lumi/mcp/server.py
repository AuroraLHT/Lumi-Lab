"""MCP server: the lab's contract ops as tools for an LLM agent.

Generic, not hand-written per op: one Tool per (contract, capability, op), with the
op's own Pydantic request model supplying the tool's input schema directly
(`op.request.model_json_schema()`) -- a new op added to an exposed capability shows
up as a tool with no code change here, the same guarantee `lumi-codegen --check`
already enforces for the Python/TS clients. This mirrors `lumi.api.bridge.BusProxy`'s
"resolve target + call by name" dispatch, just MCP-framed instead of websocket-framed.

Three jobs, and the tools for each:

  drive a growth    every `experiment` op, plus the live RHEED camera and its
                    integration boxes, the chamber log and webcam, and who is on
                    the bus (system.registry)
  read the history  growth.db through the experiment node's list_*/get_* ops, and
                    the recordings themselves through `storage.archive` -- frames,
                    RHEED oscillation traces, the chamber log each file carries
  simulate          `simulation.rheed_sim`: structures, spot positions, patterns

Deliberately narrower than the browser bridge's fully generic REGISTRY iteration.
Beyond `experiment`, each capability -- and on some, each op -- is named in
EXPOSED, so adding an op to, say, chamber.mi_mode does NOT silently hand the agent
raw script execution, and system.supervisor's spawn/kill are never reachable. An LLM
agent operating real lab equipment is a more dangerous surface than a browser
viewer behind a role check.

Note `chamber.camera` and `rheed.camera` are not read-only -- they include
`update_camera_config`. Every tool carries a read-only hint from
`lumi.contracts.policy`, the same classification the broker enforces, so an MCP
host can auto-approve the reads and ask before anything else.
"""

from __future__ import annotations

import base64
import binascii
import copy
import difflib
import json
import logging
from collections.abc import Iterator
from typing import Any

from aio_pika import ExchangeType, connect_robust
from aio_pika.abc import AbstractChannel, AbstractConnection
from mcp import types
from mcp.server import Server
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.routing import Route

from lumi.api.db import UserStore
from lumi.base.mq.client import CapabilityClient, RemoteError
from lumi.config import settings
from lumi.contracts import Codec, policy
from lumi.contracts.chamber import CHAMBER
from lumi.contracts.experiment import EXPERIMENT
from lumi.contracts.rheed import RHEED
from lumi.contracts.simulation import SIMULATION
from lumi.contracts.spec import Capability, EquipmentContract, Op
from lumi.contracts.storage import STORAGE_NODE
from lumi.contracts.system import SYSTEM
from lumi.mcp.auth import REQUIRED_SCOPE, ROLE_SCOPES, LumiTokenVerifier
from lumi.mcp.frames import binary_content
from lumi.mcp.oauth import LOGIN_PATH, LumiAuthorizationServer
from lumi.mcp.store import OAuthStore

log = logging.getLogger(__name__)

#: (contract, {capability: ops to expose, None = all of them}) -- a None in place of
#: the dict means every capability on that contract. Only EXPERIMENT gets that.
EXPOSED: tuple[tuple[EquipmentContract, dict[str, frozenset[str] | None] | None], ...] = (
    (EXPERIMENT, None),
    # The recordings, read back. Not `storage.storage`: the experiment node's
    # start_storage/end_storage already start and stop a recording, and tie it to
    # the growth record, which a bare start_recording would not.
    (STORAGE_NODE, {"archive": None}),
    (SIMULATION, {"rheed_sim": None}),
    # The boxes the operator drew and their live intensity -- the oscillations a
    # deposition is watched by. Read-only: registering and removing boxes is the
    # operator's layout, and an agent rearranging it mid-growth helps nobody.
    (RHEED, {"camera": None, "integrator": frozenset({"bboxes", "cache"})}),
    (CHAMBER, {"log": None, "camera": None}),
    # Which nodes are up, so "the call timed out" can become "the storage node is
    # not running". Not wait_for: it blocks for its own timeout, longer than a call's.
    (SYSTEM, {"registry": frozenset({"list_nodes", "get_node"})}),
)

#: Seconds to wait for an answer, where the default 10 s is too short: a simulated
#: pattern on a large screen, or a frame out of a multi-gigabyte recording.
CALL_TIMEOUT_S: dict[str, float] = {"simulation": 60.0, "storage": 30.0}

#: The largest JSON answer sent as it is, in characters. Hosts cap what one tool
#: result may put in the context (Claude Code's default is 25k tokens), and a
#: recording's frame_times or a 4000-point trace is well past that. Over it, long
#: lists are shortened and the reply says so -- see `_compact`.
MAX_TEXT_CHARS = 40_000

#: What a tool for an upload op (a Codec.RAW request, e.g. attach_measurement_file)
#: takes for the body, beside the op's own fields. Exactly one is required.
_BODY_FIELDS = {
    "content_base64": {"type": "string", "description": "The file's bytes, base64-encoded."},
    "content_text": {"type": "string", "description": "The file's contents as UTF-8 text, "
                     "for a text file (CSV, a data export); instead of content_base64."},
}


def exposed_ops() -> Iterator[tuple[EquipmentContract, Capability, Op]]:
    """Every op in EXPOSED that a tool can carry."""
    for contract, caps in EXPOSED:
        for cap in contract.capabilities:
            if caps is not None and cap.name not in caps:
                continue
            ops = None if caps is None else caps[cap.name]
            for op in cap.ops:
                if ops is None or op.name in ops:
                    yield contract, cap, op


def _tool_name(contract: str, cap: str, op: str) -> str:
    return f"{contract}.{cap}.{op}"


def tool_schema(op: Op) -> dict[str, Any]:
    """The op's request schema; for an upload, with the body fields added."""
    schema = op.request.model_json_schema()
    if op.request_codec is Codec.JSON:
        return schema
    schema = copy.deepcopy(schema)
    schema.setdefault("properties", {}).update(_BODY_FIELDS)
    return schema


def tool_for(contract: EquipmentContract, cap: Capability, op: Op) -> types.Tool:
    read_only = policy.effect(op) is policy.Effect.READ
    # Many experiment ops carry no doc of their own; the capability's first
    # sentence at least says what they belong to.
    description = op.doc or f"{op.name} ({contract.name}.{cap.name}: {cap.doc.split('. ')[0]})"
    return types.Tool(
        name=_tool_name(contract.name, cap.name, op.name),
        description=description,
        input_schema=tool_schema(op),
        annotations=types.ToolAnnotations(read_only_hint=read_only),
    )


def split_body(op: Op, arguments: dict[str, Any]) -> tuple[dict[str, Any], bytes | None]:
    """`(the op's own fields, the upload body)` from a tool call's arguments."""
    if op.request_codec is Codec.JSON:
        return arguments, None
    fields = dict(arguments)
    b64, text = fields.pop("content_base64", None), fields.pop("content_text", None)
    if (b64 is None) == (text is None):
        raise ValueError("give exactly one of content_base64 and content_text")
    if text is not None:
        return fields, text.encode("utf-8")
    try:
        return fields, base64.b64decode(b64, validate=True)
    except binascii.Error as exc:
        raise ValueError(f"content_base64 is not valid base64: {exc}") from None


def render_json(model: BaseModel, *, budget: int = MAX_TEXT_CHARS) -> str:
    """The model as JSON, whole if it fits in `budget` characters, else compacted."""
    text = model.model_dump_json()
    if len(text) <= budget:
        return text
    data = model.model_dump(mode="json")
    for keep in (200, 50, 12, 4):
        compact = json.dumps(_compact(data, keep), separators=(",", ":"))
        if len(compact) <= budget:
            break
    note = (
        f"[This answer was {len(text):,} characters, over the {budget:,} a tool result "
        f"may use, so every list longer than {keep} items is shown as "
        f'{{"elided_list": {{"length", "first", "last"}}}}. Ask for less to get it whole: '
        "max_points, since/until, limit/offset, or naming the ids you want.]\n"
    )
    if len(compact) > budget:
        note += f"[Still over at {keep} items per list, so cut at {budget:,} characters.]\n"
    return note + compact[:budget]


def _compact(value: Any, keep: int) -> Any:
    if isinstance(value, dict):
        return {k: _compact(v, keep) for k, v in value.items()}
    if isinstance(value, list):
        if len(value) <= keep:
            return [_compact(v, keep) for v in value]
        head = max(1, keep // 2)
        tail = max(1, keep - head - 1) if keep > 2 else 1
        return {"elided_list": {
            "length": len(value),
            "first": [_compact(v, keep) for v in value[:head]],
            "last": [_compact(v, keep) for v in value[-tail:]],
        }}
    return value


class LumiMCPServer:
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
            "lumi",
            version="1.1",
            instructions=INSTRUCTIONS,
            on_list_tools=self._on_list_tools,
            on_call_tool=self._on_call_tool,
        )

    async def connect(self) -> None:
        self._connection = await connect_robust(f"amqp://{self.user}:{self.password}@{self.host}/")
        self._channel = await self._connection.channel()

        exchanges: dict[str, Any] = {}
        for contract, cap, op in exposed_ops():
            key = f"{contract.name}.{cap.name}"
            client = self._clients.get(key)
            if client is None:
                if contract.exchange not in exchanges:
                    exchanges[contract.exchange] = await self._channel.declare_exchange(
                        contract.exchange, ExchangeType(contract.exchange_type), durable=True
                    )
                client = CapabilityClient(
                    cap, contract.name, channel=self._channel, exchange=exchanges[contract.exchange],
                    timeout=CALL_TIMEOUT_S.get(contract.name, 10.0),
                )
                await client.start()
                self._clients[key] = client
            self._tools[_tool_name(contract.name, cap.name, op.name)] = (contract, cap, op, client)

        log.info("mcp server connected; %d tools from %s", len(self._tools), ", ".join(self._clients))

    async def build_http_app(
        self, *, bind_host: str = "127.0.0.1", public_url: str | None = None, oauth: bool = True
    ) -> Starlette:
        """The Starlette app for the streamable-HTTP transport, with bearer-token
        auth wired to the same JWT/user store the browser bridge uses. Always
        requires a valid operator-or-admin token -- unlike the browser side, this
        is not gated by settings.auth.enabled, since the whole point of this
        transport is letting a remote agent reach real equipment control. TLS is
        not this app's job: the uvicorn serving it terminates it (lumi.tls).

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
        return types.ListToolsResult(
            tools=[tool_for(contract, cap, op) for contract, cap, op, _ in self._tools.values()]
        )

    async def _on_call_tool(self, ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        entry = self._tools.get(params.name)
        if entry is None:
            near = difflib.get_close_matches(params.name, self._tools, n=5, cutoff=0.5)
            return _error(f"unknown tool {params.name!r}" + (f"; did you mean {near}?" if near else ""))
        contract, cap, op, client = entry

        # Populated only under the HTTP transport (see build_http_app); stdio is a
        # local subprocess only you can spawn, same trust level as today's no-auth
        # server. RequireAuthMiddleware already demands REQUIRED_SCOPE before a
        # request reaches here, so this is defense-in-depth consistency with how
        # the browser bridge enforces the same predicate, not the only gate.
        access_token = get_access_token()
        actor = "mcp"
        if access_token is not None:
            claims = access_token.claims or {}
            role = claims.get("role")
            routing_key = cap.keys(contract.name).request(op.name)
            if role is None or not policy.permits(role, contract.exchange, routing_key):
                return _error(f"role {role!r} may not call {params.name!r}")
            # From the verified token, never from the arguments: the step journal
            # credits whoever this is with what the agent does.
            actor = f"mcp:{claims.get('user') or access_token.client_id}"

        try:
            fields, body = split_body(op, params.arguments or {})
            req = op.request.model_validate(fields)
            result = await client.call(op.name, req, body, actor=actor)
        except RemoteError as exc:
            return _error(str(exc))
        except Exception as exc:
            log.exception("tool %s failed", params.name)
            return _error(f"{type(exc).__name__}: {exc}")

        # A binary-codec op answers `(headers, body)`: the model describes the frame
        # and the pixels are the body. Returning only `result[0]` -- which is what this
        # did -- meant `rheed.camera.image` handed the agent a shape and a dtype and
        # threw the image away, and nothing in the reply said a body had existed.
        if isinstance(result, tuple):
            model, payload = result
            content, note = binary_content(payload, str(op.response_codec), text_budget=MAX_TEXT_CHARS)
            return types.CallToolResult(
                content=[*content, types.TextContent(type="text", text=f"{note}\n{render_json(model)}")]
            )
        return types.CallToolResult(content=[types.TextContent(type="text", text=render_json(result))])


def _error(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=True)


INSTRUCTIONS = """\
Tools for a PLD/RHEED lab, named contract.capability.op. Three jobs:

Driving a growth -- experiment.driver.*. Long-running ops (to_temperature, cool_down,
perform_preablation, perform_deposition, anneal, auto_align_center_mask) return at once
with a task_id; poll experiment.driver.get_current_log (or the driver's state) until
current_task clears before assuming the step finished, and then check it succeeded --
read the temperature or pressure back rather than trusting that it cleared. Ops that need
a human to act or read something physical (laser power, mask alignment, RHEED gain, a
per-pixel visual check) return with pending_confirmation set; resolve it with the matching
confirm_*/resolve_* tool (or the generic `confirm` for a plain proceed-gate) before
continuing. rheed.camera.image and chamber.camera.image show the live pictures;
rheed.integrator.cache is the live intensity of each box the operator drew (the growth
oscillations); chamber.log.log the latest chamber readings. system.registry.list_nodes
says which nodes are up -- a call that times out usually means its node is not.

Reading the history -- the growth database through experiment.driver: list_projects,
list_substrates, list_samples, get_sample, sample_history (every step a sample went
through), list_records, list_measurements (measurements joined to the growth conditions).
A record's record_name is the name of its recording: storage.archive.recording_info
gives its frames and log columns, recording_frame_jpeg a frame to look at,
recording_integration the RHEED oscillation traces, recording_log the chamber log
during it. chamber.log.list_log_files / log_window reach the chamber log outside any
recording.

Simulating RHEED -- simulation.rheed_sim.*. list_structures for what is built in or saved;
rheed_spots for where rods and spots land (fast, labelled, no image);
simulate_rheed_jpeg for the pattern to look at. Leave `screen` out to get the lab
camera's own geometry, so a simulation compares directly with rheed.camera.image or a
recorded frame; compare using the spot list, not the rendering's brightness.

Answers over the size limit come back with long lists shortened and a note saying so;
narrow the query (max_points, since/until, limit) to get them whole.
"""
