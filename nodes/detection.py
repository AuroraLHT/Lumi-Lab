from lumi.detection.communication import (
    DetectionMessageQueueServer,
    LiveDetectionMessageQueueServer,
    CameraMessageQueueClient,
)
from lumi.detection.model import DetectorServer, DetectorConfig

import time
import datetime
import asyncio
import logging

import cv2

import aio_pika
from aio_pika import ExchangeType, connect
from lumi.config import settings

FORMAT = '%(asctime)s %(levelname)s:%(message)s'
logging.basicConfig(level=logging.INFO, format=FORMAT)

async def main(args):
    # Perform connection
    connection = await connect(f"amqp://guest:guest@{args.host}/")

    # Creating a channel
    channel = await connection.channel()

    rheed_exchange = await channel.declare_exchange(
        settings.rheed.exchange,
        ExchangeType(settings.rheed.exchange_type),
        # ExchangeType.DIRECT,
    )

    config = DetectorConfig(
        input_queue_size=settings.detection.detector.input_queue_size,
        output_queue_size=settings.detection.detector.output_queue_size,
        model_device=settings.detection.detector.model_device,
        model_folder=settings.detection.detector.model_folder,
    )

    detector = DetectorServer(config=config, name=settings.detection.detector.name, daemon=True)
    detector.start()

    camera_client = CameraMessageQueueClient(
        channel=channel, 
        exchange=rheed_exchange, 
        routing_key=settings.rheed.mq.image.request,
        control_routing_key=settings.rheed.mq.image.ctrl,
        state_routing_key=settings.rheed.mq.image.state,
        client_name=settings.rheed.mq.image.name,
        time_out=10,
        on_state_callback=None,
    )

    await camera_client.start()

    detection_mq = DetectionMessageQueueServer(
        detector=detector,
        camera_client= camera_client,
        channel=channel,
        exchange=rheed_exchange,
        routing_key=settings.detection.mq.detection.request,
        control_routing_key=settings.detection.mq.detection.ctrl,
        state_routing_key=settings.detection.mq.detection.state,
        server_name=settings.detection.mq.detection.name,
    )
    live_detection_mq = LiveDetectionMessageQueueServer(
        detector=detector,
        camera_client=camera_client,
        channel=channel,
        exchange=rheed_exchange,
        control_routing_key=settings.detection.mq.live_detection.ctrl,
        publish_routing_key=settings.detection.mq.live_detection.publish,
        state_routing_key=settings.detection.mq.live_detection.state,
        server_name=settings.detection.mq.live_detection.name,
    )
    await detection_mq.start()
    await live_detection_mq.start()

    print("message queue started")
    await asyncio.Future()

    detector.join()
    print("program exit")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
                        prog='Detection Node',
                        description='...',
                        epilog='...')
    parser.add_argument("--host", type=str, default=settings.rabbitmq.host)
    args= parser.parse_args()

    asyncio.run(main(args))
