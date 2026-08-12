"""Run the experiment MCP server.

    python -m lumi.mcp                                   # stdio, for a local MCP host
    python -m lumi.mcp --transport http --port 8100       # streamable HTTP, for a remote agent

stdio is a local subprocess only you can spawn -- no auth needed, same trust level
as any other local tool. HTTP is for a remote agent and always requires a valid
operator-or-admin bearer token (the same JWT `POST /auth/login` on the browser
bridge issues -- see lumi.mcp.auth), independent of settings.auth.enabled.

HTTP serves plain HTTP; put a reverse proxy (nginx/Caddy) in front for TLS. Binding
--bind-host to anything other than 127.0.0.1/localhost means the proxy (or whatever
network path reaches this port) is the only thing standing between the internet and
real lab equipment control, so double-check your firewall before doing that.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from lumi.config import settings
from lumi.mcp.server import ExperimentMCPServer

log = logging.getLogger(__name__)


async def run_stdio(server: ExperimentMCPServer) -> None:
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await server.server.run(read_stream, write_stream, server.server.create_initialization_options())


async def run_http(server: ExperimentMCPServer, *, bind_host: str, port: int) -> None:
    import uvicorn

    app = await server.build_http_app(bind_host=bind_host)
    config = uvicorn.Config(app, host=bind_host, port=port, log_level="info")
    await uvicorn.Server(config).serve()


async def main(args: argparse.Namespace) -> None:
    server = ExperimentMCPServer(host=args.host, user=args.user, password=args.password)
    await server.connect()
    try:
        if args.transport == "stdio":
            await run_stdio(server)
        else:
            await run_http(server, bind_host=args.bind_host, port=args.port)
    finally:
        await server.close()


def cli() -> None:
    parser = argparse.ArgumentParser(prog="lumi-mcp", description="MCP server for the experiment node")
    parser.add_argument("--host", default=settings.rabbitmq.host, help="AMQP broker host")
    parser.add_argument("--user", default="guest", help="AMQP user")
    parser.add_argument("--password", default="guest", help="AMQP password")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--bind-host", default="127.0.0.1", help="HTTP transport bind address")
    parser.add_argument("--port", type=int, default=8100, help="HTTP transport port")
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
