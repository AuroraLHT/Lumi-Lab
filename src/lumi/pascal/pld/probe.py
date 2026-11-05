"""`python -m lumi.pascal.pld.probe [ADDRESS]`: is pld-service up? For the launch scripts.

Prints one line and exits 0 either way:

    up|<address>|<TWIN or HARDWARE>|<operable or read-only>|<controller or nobody>
    down|<address>|<why>
"""

from __future__ import annotations

import sys

from lumi.config import settings
from lumi.pascal.pld.client import PldClient, PldUnavailable
from lumi.pascal.pld.proto import pld_pb2 as pb


def main() -> None:
    address = (sys.argv[1] if len(sys.argv) > 1 else "") or settings.get("pascal.pld.address", "127.0.0.1:50051")
    client = PldClient(address)
    try:
        state = client.state(timeout=3.0)
    except PldUnavailable as e:
        print(f"down|{address}|{e}")
        return
    finally:
        client.close()
    print("|".join((
        "up", address, pb.Backend.Name(state.backend),
        "read-only" if state.read_only else "operable", state.controller or "nobody",
    )))


if __name__ == "__main__":
    main()
