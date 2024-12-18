import uvicorn

# from lumi.api.main import app

import argparse
from lumi.config import settings


def run(host):
    uvicorn.run(
        "lumi.api.main:app",
        host=host,
        port=settings.api.port,
        workers=settings.api.workers,
        reload=False,
        timeout_graceful_shutdown=30,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the API server")
    parser.add_argument(
        "--host", default=settings.api.host, help="Host to run the server on"
    )
    args = parser.parse_args()

    run(host=args.host)
