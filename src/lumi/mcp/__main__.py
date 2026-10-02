"""Run the lab MCP server.

    python -m lumi.mcp                                   # stdio, for a local MCP host
    python -m lumi.mcp --transport http --port 8100       # streamable HTTP, for a remote agent

Which broker: `--host` defaults to `settings.rabbitmq.host`, the same as every node --
localhost in the tracked config, so nothing here reaches real equipment unless you say
so. Pass `--host` (or override rabbitmq.host in cfg/.secrets.toml) for the lab broker.
A broker still holding the pre-refactor `direct` exchanges fails at startup with

    PRECONDITION_FAILED - inequivalent arg 'type' for exchange 'RHEED': received 'topic'
    but current is 'direct'

which is that broker needing its old exchanges cleared, not a fault in this server.

stdio is a local subprocess only you can spawn -- no auth needed, same trust level
as any other local tool. HTTP is for a remote agent and always requires a valid
operator-or-admin bearer token (the same JWT `POST /auth/login` on the browser
bridge issues -- see lumi.mcp.auth), independent of settings.auth.enabled.

Getting that token is the client's job, not yours: the HTTP transport is its own
OAuth authorization server (lumi.mcp.oauth), so a client with no credentials is
sent to a login page, you type a lumi username and password, and it keeps itself
renewed from there. Register it with no secrets at all --

    claude mcp add --transport http lumi http://127.0.0.1:8100/mcp

-- and log in when it asks. A hand-minted bearer token still works if you would
rather pass one (scripts/start_mcp_demo.sh --token-only); `--no-oauth` turns the
login flow off and leaves only that.

HTTP serves HTTPS itself when `[tls]` is enabled (lumi.tls; scripts/make_lab_cert.sh
makes the certificate), and plain HTTP otherwise. Off loopback it has to be HTTPS:
the login form posts a password, and the MCP SDK refuses a plain-HTTP OAuth issuer
anywhere but localhost. --public-url is the address clients actually use, published
as the OAuth issuer and baked into every redirect; the certificate must cover its
host. Binding --bind-host to anything other than 127.0.0.1/localhost means the
network path to this port is the only thing standing between the internet and real
lab equipment control, so double-check your firewall before doing that.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from lumi.config import settings
from lumi.mcp.server import LumiMCPServer
from lumi.tls import reloading_server, scheme, uvicorn_ssl_kwargs

log = logging.getLogger(__name__)


async def run_stdio(server: LumiMCPServer) -> None:
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await server.server.run(read_stream, write_stream, server.server.create_initialization_options())


async def run_http(
    server: LumiMCPServer, *, bind_host: str, port: int, public_url: str, oauth: bool
) -> None:
    import uvicorn

    app = await server.build_http_app(bind_host=bind_host, public_url=public_url, oauth=oauth)
    if oauth:
        log.info("clients with no token will be sent to %s/login to sign in", public_url.rstrip("/"))
    # HTTPS when [tls] is enabled (lumi.tls); raises rather than falling back to HTTP.
    # SIGHUP reloads a renewed certificate (scripts/tailscale_cert.sh sends it).
    config = uvicorn.Config(app, host=bind_host, port=port, log_level="info", **uvicorn_ssl_kwargs())
    await reloading_server(config)


async def main(args: argparse.Namespace) -> None:
    server = LumiMCPServer(host=args.host, user=args.user, password=args.password)
    await server.connect()
    try:
        if args.transport == "stdio":
            await run_stdio(server)
        else:
            await run_http(
                server,
                bind_host=args.bind_host,
                port=args.port,
                public_url=args.public_url or f"{scheme()}://{args.bind_host}:{args.port}",
                oauth=not args.no_oauth,
            )
    finally:
        await server.close()


def cli() -> None:
    parser = argparse.ArgumentParser(prog="lumi-mcp", description="MCP server for the experiment node")
    parser.add_argument(
        "--host",
        default=settings.rabbitmq.host,
        help=(
            f"AMQP broker host (default {settings.rabbitmq.host}, from settings.rabbitmq.host). "
            "Point it at the lab broker to drive real equipment; that broker must already "
            "be serving the contract-era topic exchanges"
        ),
    )
    parser.add_argument("--user", default="guest", help="AMQP user")
    parser.add_argument("--password", default="guest", help="AMQP password")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--bind-host", default="127.0.0.1", help="HTTP transport bind address")
    parser.add_argument("--port", type=int, default=8100, help="HTTP transport port")
    parser.add_argument(
        "--public-url",
        default=None,
        help=(
            "the base URL clients reach this server on (default http://<bind-host>:<port>). "
            "Published as the OAuth issuer and used to build every redirect, so behind a "
            "TLS reverse proxy this must be the proxy's https:// address, not the bind address"
        ),
    )
    parser.add_argument(
        "--no-oauth",
        action="store_true",
        help=(
            "do not act as an OAuth authorization server: no login page, no client "
            "registration. Clients must then present a bearer token minted elsewhere "
            "(scripts/start_mcp_demo.sh --token-only), which is how this worked before"
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    # stdout is the MCP transport in stdio mode -- every log line must go to
    # stderr, or it corrupts the protocol stream. HTTP mode has no such
    # constraint, but stderr is used unconditionally for consistency.
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    asyncio.run(main(args))


if __name__ == "__main__":
    cli()
