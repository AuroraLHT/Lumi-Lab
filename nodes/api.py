import uvicorn

# from lumi.api.main import app

import argparse
import asyncio
from lumi.config import settings
from lumi.tls import reloading_server, uvicorn_ssl_kwargs


def run(host):
    options = dict(
        host=host,
        port=settings.api.port,
        reload=False,
        timeout_graceful_shutdown=30,
        # Above the bridge's own upload cap, so an oversized upload reaches the bridge
        # and is refused with an error reply rather than dropped mid-frame (close 1006).
        ws_max_size=int(settings.api.get("ws_max_bytes", 64 * 1024 * 1024)),
        # HTTPS/WSS when [tls] is enabled; raises rather than falling back to plain HTTP.
        **uvicorn_ssl_kwargs(),
    )
    workers = int(settings.api.workers or 1)
    if workers > 1:
        # uvicorn's supervisor restarts the workers on SIGHUP, and each one loads the
        # certificate afresh -- that is how a renewal (scripts/tailscale_cert.sh) lands.
        uvicorn.run("lumi.api.main:app", workers=workers, **options)
    else:
        # A single process has no supervisor, and an unhandled SIGHUP would kill it:
        # reload the certificate in place instead.
        asyncio.run(reloading_server(uvicorn.Config("lumi.api.main:app", **options)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the API server")
    parser.add_argument(
        "--host", default=settings.api.host, help="Host to run the server on"
    )
    args = parser.parse_args()

    run(host=args.host)
