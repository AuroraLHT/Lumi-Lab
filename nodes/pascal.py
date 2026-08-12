"""The Pascal chamber node: growth log, chamber config, MI mode, chamber camera.

    python -m nodes.pascal --src sim           # simulated chamber (state model)
    python -m nodes.pascal --src sim --speed 30
    python -m nodes.pascal --src test          # replay of a recorded log
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
    build_simulated_chamber,
)

log = logging.getLogger(__name__)


def build(args: argparse.Namespace) -> EquipmentNode:
    log_reader = build_log_reader(args.src, args.log)
    config_reader = build_config_reader(args.src)
    sim_workers: list = []
    if args.src == "sim":
        # One chamber model, shared: the MI backend drives it and the log writer
        # renders it. The log *reader* above is the production one, tailing the CSV the
        # writer produces -- it has no idea a simulator is on the other end.
        _model, log_writer, mi_server, mi_backend = build_simulated_chamber(
            args.log, args.mi, time_scale=args.speed
        )
        sim_workers = [log_writer, mi_backend]
        mi_simulator = None
    else:
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

    # The simulator's writer goes first: it creates the CSV the log reader is waiting
    # for a watchdog event on.
    workers = [*sim_workers, log_reader, config_reader, mi_server, camera, jpeg_encoder]
    if mi_simulator is not None:
        workers.append(mi_simulator)
    for worker in workers:
        node.thread(worker)

    return node


def cli() -> None:
    parser = argparse.ArgumentParser(prog="lumi-pascal", description="Pascal chamber node")
    parser.add_argument("--src", choices=("path", "sim", "test"), default="sim",
                        help="'path' for the real chamber, 'sim' for the state-model "
                             "simulator, 'test' to replay a recorded log")
    parser.add_argument("--log", default=None,
                        help="chamber log file (--src=path) or output directory (--src=sim)")
    parser.add_argument("--mi", default=None, help="MI mode folder (--src=path or sim)")
    parser.add_argument("--speed", type=float, default=1.0,
                        help="simulated-time multiplier for --src=sim; 1.0 is real time, "
                             "30 makes a 300s deposition take 10s")
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
