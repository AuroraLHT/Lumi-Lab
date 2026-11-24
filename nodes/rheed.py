"""The RHEED node: camera, video, integration, STFT.

    python -m nodes.rheed --src simcam [--sim-frame sto|ysz|PATH] [--follow-gun] [--follow-growth]

Was 354 lines. The hand-wiring is gone (routing keys come from the contract), the
eight server objects are four capabilities, and the shutdown code that used to sit
unreachable below `await asyncio.Future()` is now the node's drain path.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from aio_pika import ExchangeType

from lumi.base.camera.handlers import CameraHandler, VideoHandler
from lumi.config import settings
from lumi.contracts.chamber import CHAMBER
from lumi.contracts.rheed import RHEED
from lumi.generated.clients.chamber import ChamberPldClient
from lumi.node import EquipmentNode
from lumi.rheed.handlers import IntegratorHandler, STFTHandler
from lumi.rheed.hardware import (GrowthClock, GunBeam, build_camera, build_compressor,
                                 build_integrator, build_stft)
from lumi.rheed.sim_frames import SIM_FRAMES

log = logging.getLogger(__name__)


def follow_twin(beam: GunBeam | None, growth: GrowthClock | None, period_s: float = 1.0):
    """A node task: the sim image from pld-service, read off the chamber node every
    `period_s` -- its brightness from the gun (`beam`) and its oscillations from the
    deposition (`growth`). A chamber node that does not answer leaves the last reading,
    and stops a growth clock counting on."""

    async def follow_twin(node: EquipmentNode) -> None:
        chamber_x = await node.channel.declare_exchange(
            CHAMBER.exchange, ExchangeType(CHAMBER.exchange_type), durable=True)
        pld = ChamberPldClient(node.channel, chamber_x, timeout=5.0)
        await pld.start()
        try:
            while True:
                try:
                    state = await pld.chamber_state()
                except Exception as exc:
                    log.debug("the chamber's state not read: %s", exc)
                    if growth is not None:
                        growth.update(None)
                else:
                    if beam is not None:
                        beam.update(state.rheed)
                    if growth is not None:
                        growth.update(state)
                await asyncio.sleep(period_s)
        finally:
            await pld.stop()

    return follow_twin


def build(args: argparse.Namespace) -> EquipmentNode:
    beam = None
    if getattr(args, "follow_gun", False):
        if args.src != "simcam":
            raise SystemExit("--follow-gun is for --src simcam: a real camera sees the real beam")
        beam = GunBeam(float(settings.get("rheed.simcam.gun_reference_ua", 8.7)))
    growth = None
    if getattr(args, "follow_growth", False):
        if args.src != "simcam":
            raise SystemExit("--follow-growth is for --src simcam: a real camera sees the real film")
        growth = GrowthClock(
            pulses_per_monolayer=float(settings.get("rheed.simcam.pulses_per_monolayer", 40.0)),
            depth=float(settings.get("rheed.simcam.oscillation_depth", 0.4)),
            decay_ml=float(settings.get("rheed.simcam.oscillation_decay_ml", 10.0)),
            recovery=float(settings.get("rheed.simcam.oscillation_recovery", 0.6)),
            recovery_s=float(settings.get("rheed.simcam.oscillation_recovery_s", 20.0)))
    camera, frame_processing, height, width = build_camera(
        args.src, sim_frame=args.sim_frame, frame_scale=beam.scale if beam else None,
        feature_scale=growth.scale if growth else None)

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
    if beam is not None or growth is not None:
        node.task(follow_twin(beam, growth))

    return node


def cli() -> None:
    parser = argparse.ArgumentParser(prog="lumi-rheed", description="RHEED camera and analysis node")
    parser.add_argument("--src", choices=("pylon", "webcam", "simcam"), default="pylon")
    parser.add_argument("--sim-frame", default=None,
                        help="with --src simcam: what the camera shows -- "
                             f"{', '.join(SIM_FRAMES)} (real lab frames) or a .npy/image path. "
                             "Default: rheed.simcam.source")
    parser.add_argument("--follow-gun", action="store_true",
                        help="with --src simcam: the image's brightness follows pld-service's "
                             "RHEED gun (dark with it off, with the emission current on), for "
                             "a twin. Reads the gun off the chamber node")
    parser.add_argument("--follow-growth", action="store_true",
                        help="with --src simcam: RHEED oscillations from pld-service's "
                             "deposition (pulses fired with the shutter open; "
                             "rheed.simcam.pulses_per_monolayer), in place of the clock-driven "
                             "oscillations, for a twin. Reads the chamber node")
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
