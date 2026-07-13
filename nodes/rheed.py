"""The RHEED node: camera, video, integration, STFT.

    python -m nodes.rheed --src simcam

Was 354 lines. The hand-wiring is gone (routing keys come from the contract), the
eight server objects are four capabilities, and the shutdown code that used to sit
unreachable below `await asyncio.Future()` is now the node's drain path.
"""

from __future__ import annotations

import argparse
import logging

from lumi.base.camera.handlers import CameraHandler, VideoHandler
from lumi.config import settings
from lumi.contracts.rheed import RHEED
from lumi.node import EquipmentNode
from lumi.rheed.handlers import IntegratorHandler, STFTHandler
from lumi.rheed.hardware import build_camera, build_compressor, build_integrator, build_stft

log = logging.getLogger(__name__)


def build(args: argparse.Namespace) -> EquipmentNode:
    camera, frame_processing, height, width = build_camera(args.src)

    # Each consumer gets its own fan-out queue from the camera thread. This is the
    # camera's existing in-process pub/sub, and it is why one grab feeds the encoder,
    # the live feed and the integrator without copying the frame three times.
    video_q = camera.register_queue("video")
    frames_q = camera.register_queue("frames")
    integration_q = camera.register_queue("integration")

    compressor = build_compressor(camera, video_q, frame_processing, height, width)
    integrator = build_integrator(integration_q)
    stft = build_stft(integrator)

    node = EquipmentNode(
        RHEED,
        amqp_url=f"amqp://{args.user}:{args.password}@{args.host}/",
        instance_id=args.instance,
    )

    node.mount("camera", CameraHandler(camera, frames_q))
    node.mount("video", VideoHandler(compressor, compressor.fragments))
    node.mount("integrator", IntegratorHandler(integrator, integrator.output_queue))
    node.mount("stft", STFTHandler(stft, stft.output_queue))

    for worker in (camera, compressor, integrator, stft):
        node.thread(worker)

    return node


def cli() -> None:
    parser = argparse.ArgumentParser(prog="lumi-rheed", description="RHEED camera and analysis node")
    parser.add_argument("--src", choices=("pylon", "webcam", "simcam"), default="pylon")
    parser.add_argument("--host", default=settings.rabbitmq.host)
    parser.add_argument("--user", default="guest")
    parser.add_argument("--password", default="guest")
    parser.add_argument("--instance", default=None, help="override the instance id")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    build(args).run()


if __name__ == "__main__":
    cli()
