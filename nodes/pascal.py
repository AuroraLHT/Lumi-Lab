"""The Pascal chamber node: growth log, chamber config, MI mode, chamber camera.

    python -m nodes.pascal --src test          # simulated chamber
    python -m nodes.pascal --src path --log ... --mi ...

Was 316 lines.
"""

from __future__ import annotations

import argparse
import logging

from lumi.base.camera.handlers import JpegCameraHandler
from lumi.config import settings
from lumi.contracts.chamber import CHAMBER
from lumi.node import EquipmentNode
from lumi.pascal.handlers import ChamberConfigHandler, ChamberLogHandler, MIModeHandler
from lumi.pascal.hardware import (
    build_camera,
    build_config_reader,
    build_jpeg_encoder,
    build_log_reader,
    build_mi_mode,
)

log = logging.getLogger(__name__)


def build(args: argparse.Namespace) -> EquipmentNode:
    log_reader = build_log_reader(args.src, args.log)
    config_reader = build_config_reader(args.src)
    mi_server, mi_simulator = build_mi_mode(args.src, args.mi)
    camera, _, _ = build_camera(args.src)

    # The camera's in-process fan-out: one grab feeds every consumer. The stream is
    # served from the encoder's output, not from a camera queue directly, so `frames`
    # is the encoder's input rather than the handler's.
    frames_q = camera.register_queue("frames")
    jpeg_encoder = build_jpeg_encoder(camera, frames_q)

    node = EquipmentNode(
        CHAMBER,
        amqp_url=f"amqp://{args.user}:{args.password}@{args.host}/",
        instance_id=args.instance,
    )

    node.mount("log", ChamberLogHandler(log_reader, getattr(log_reader, "queue", None)))
    node.mount("config", ChamberConfigHandler(config_reader))
    node.mount("mi_mode", MIModeHandler(mi_server))
    node.mount("camera", JpegCameraHandler(camera, jpeg_encoder))

    workers = [log_reader, config_reader, mi_server, camera, jpeg_encoder]
    if mi_simulator is not None:
        workers.append(mi_simulator)
    for worker in workers:
        node.thread(worker)

    return node


def cli() -> None:
    parser = argparse.ArgumentParser(prog="lumi-pascal", description="Pascal chamber node")
    parser.add_argument("--src", choices=("path", "test"), default="test",
                        help="'path' for the real chamber, 'test' for the simulator")
    parser.add_argument("--log", default=None, help="chamber log file (with --src=path)")
    parser.add_argument("--mi", default=None, help="MI mode folder (with --src=path)")
    parser.add_argument("--host", default=settings.rabbitmq.host)
    parser.add_argument("--user", default="guest")
    parser.add_argument("--password", default="guest")
    parser.add_argument("--instance", default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    build(args).run()


if __name__ == "__main__":
    cli()
