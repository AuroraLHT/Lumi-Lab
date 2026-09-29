"""The simulation node: RHEED patterns computed from a crystal structure.

    python -m nodes.simulation

No hardware and no other node to wait for. Structures saved through it live in
`simulation.rheed.structures_path`; a request that names no screen gets the lab
camera's, from `[simulation.rheed.screen]`.
"""

from __future__ import annotations

import argparse
import logging

from lumi.config import settings
from lumi.contracts.payloads.simulation import ScreenSpec
from lumi.contracts.simulation import SIMULATION
from lumi.node import EquipmentNode
from lumi.rheed.sim_frames import SIM_FRAMES
from lumi.rheedsim import StructureStore
from lumi.rheedsim.handlers import RheedSimHandler

log = logging.getLogger(__name__)


def build(args: argparse.Namespace) -> EquipmentNode:
    cfg = settings.get("simulation.rheed", {}) or {}
    store = StructureStore(args.structures or cfg.get("structures_path") or None)
    screen = ScreenSpec(**{k.lower(): v for k, v in dict(cfg.get("screen", {}) or {}).items()})
    if args.sim_frame:
        # The simulated camera is showing a real frame; its shadow edge is where that
        # recording's was, so the default screen has to put the origin there too.
        frame = SIM_FRAMES[args.sim_frame]
        screen = screen.model_copy(update={"origin_x_px": frame.origin_px[0],
                                           "origin_y_px": frame.origin_px[1]})
    log.info("structures in %s; default screen %s", store.root, screen)

    node = EquipmentNode(
        SIMULATION,
        amqp_url=f"amqp://{args.user}:{args.password}@{args.host}/",
        instance_id=args.instance,
    )
    energy = float(settings.get("rheed.energy_kev", 25.0) or 25.0)
    node.mount("rheed_sim", RheedSimHandler(store, screen, energy_kev=energy))
    return node


def cli() -> None:
    parser = argparse.ArgumentParser(prog="lumi-simulation", description="RHEED simulation node")
    parser.add_argument("--host", default=settings.rabbitmq.host)
    parser.add_argument("--user", default="guest")
    parser.add_argument("--password", default="guest")
    parser.add_argument("--instance", default=None, help="override the instance id")
    parser.add_argument("--structures", default=None,
                        help="folder for saved structures (default: simulation.rheed.structures_path)")
    parser.add_argument("--sim-frame", choices=tuple(SIM_FRAMES), default=None,
                        help="the simulated RHEED camera is showing this frame: use its screen "
                             "origin as the default (the simulation stack passes the same "
                             "--sim-frame to nodes/rheed.py)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    build(args).run()


if __name__ == "__main__":
    cli()
