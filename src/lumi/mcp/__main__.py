"""Run the experiment MCP server over stdio.

    python -m lumi.mcp

Meant to be launched by an MCP host (an agent runtime), not by hand -- stdio is the
transport, so there is no port to bind and no output on stdout other than the MCP
protocol itself (logging goes to stderr).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from mcp.server.stdio import stdio_server

from lumi.config import settings
from lumi.mcp.server import ExperimentMCPServer

log = logging.getLogger(__name__)


async def main(args: argparse.Namespace) -> None:
    server = ExperimentMCPServer(host=args.host, user=args.user, password=args.password)
    await server.connect()
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.server.run(read_stream, write_stream, server.server.create_initialization_options())
    finally:
        await server.close()


def cli() -> None:
    parser = argparse.ArgumentParser(prog="lumi-mcp", description="MCP server for the experiment node")
    parser.add_argument("--host", default=settings.rabbitmq.host)
    parser.add_argument("--user", default="guest")
    parser.add_argument("--password", default="guest")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    # stdout is the MCP transport -- every log line must go to stderr, or it
    # corrupts the protocol stream.
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    asyncio.run(main(args))


if __name__ == "__main__":
    cli()
