"""The detection node: Cascade Mask R-CNN over the RHEED pattern.

    python -m nodes.detection

Requires the model stack (torch, mmdet, mmcv, rhana) -- see the `detection` extra in
pyproject.toml. Only this process needs it; the detection *contract* and its generated
client do not.

Unlike the old node, this one does not RPC the camera for every frame. It subscribes
to `rheed.camera`'s stream and feeds the detector as frames arrive.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from aio_pika import ExchangeType

from lumi.config import settings
from lumi.contracts.detection import DETECTION_NODE
from lumi.contracts.rheed import RHEED
from lumi.detection.handlers import DetectionHandler, OverlayHandler
from lumi.generated.clients.rheed import RheedCameraClient
from lumi.node import EquipmentNode

log = logging.getLogger(__name__)


def build_detector():
    from lumi.detection.model import DetectorConfig, DetectorServer, DetectorState

    cfg = settings.detection.detector
    config = DetectorConfig(
        input_queue_size=cfg.input_queue_size,
        output_queue_size=cfg.output_queue_size,
        detector_model_path=cfg.detector_model_path,
        detector_model_config_path=cfg.detector_model_config_path,
        classifier_model_path=cfg.classifier_model_path,
        classifier_label_mapper_path=cfg.classifier_label_mapper_path,
        classifier_transforms_path=cfg.classifier_transforms_path,
        detector_model_device=cfg.detector_model_device,
        classifier_model_device=cfg.classifier_model_device,
    )
    state = DetectorState(
        horizontal_center=None,
        pattern_dims=None,
        crop_setup={
            "sx": cfg.crop_setup.sx,
            "sy": cfg.crop_setup.sy,
            "ex": cfg.crop_setup.ex,
            "ey": cfg.crop_setup.ey,
        },
        db_track=None,
    )
    return DetectorServer(config=config, name=cfg.name, daemon=True, detector_state=state)


async def main(args: argparse.Namespace) -> None:
    detector = build_detector()
    handler = DetectionHandler(detector)

    # The overlay is the same inference, published without the masks or the pattern, on
    # its own routing key. Browsers bind that key; storage binds the heavy one. So the
    # arrays are never *sent* to a browser rather than being sent and then discarded.
    overlay = OverlayHandler()
    handler.add_sink(overlay.on_detection)

    node = EquipmentNode(
        DETECTION_NODE,
        amqp_url=f"amqp://{args.user}:{args.password}@{args.host}/",
        instance_id=args.instance,
    )
    node.mount("detection", handler)
    node.mount("overlay", overlay)
    node.thread(detector)
    await node.start()

    # Subscribe to the RHEED camera. Detection and RHEED share the RHEED exchange, so
    # we can reuse the node's own channel.
    assert node.channel is not None
    rheed_exchange = await node.channel.declare_exchange(
        RHEED.exchange, ExchangeType(RHEED.exchange_type), durable=True
    )
    camera = RheedCameraClient(node.channel, rheed_exchange, name=f"detection-{node.instance_id}")
    await camera.start()

    async def on_frame(meta, frame) -> None:
        handler.on_frame(frame, meta.model_dump())

    await camera.on_frame(on_frame)
    # The RHEED node's stream flag is global, so ask it to push. If RHEED is not up yet
    # this raises, and we would rather say so than sit silently detecting nothing.
    try:
        await camera.start_streaming()
    except Exception as exc:
        log.warning("could not start the RHEED camera stream (%s); is the rheed node up?", exc)

    node.on_drain(camera.stop)
    log.info("detection node up; consuming rheed.camera")

    try:
        await node._shutdown.wait()
    finally:
        await node.drain()


def cli() -> None:
    parser = argparse.ArgumentParser(prog="lumi-detection", description="RHEED detection node")
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
    asyncio.run(main(args))


if __name__ == "__main__":
    cli()
