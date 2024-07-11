from .communication import (
    DetectionMessageQueue,
    LiveDetectionMessageQueue,
    ImageMessageQueueClient,
)
from .model import DetectorServer, DetectorConfig

import time
import datetime
import asyncio
import logging

import cv2

import aio_pika
from aio_pika import ExchangeType, connect

logging.basicConfig(level=logging.INFO)

async def main(args):
    # Perform connection
    connection = await connect(f"amqp://guest:guest@{args.host}/")

    # Creating a channel
    channel = await connection.channel()

    rheed_exchange = await channel.declare_exchange(
        "RHEED",
        ExchangeType.DIRECT,
    )

    config = DetectorConfig(
        input_queue_size=10,
        output_queue_size=10,
        model_device=None,
        model_folder=None,
    )

    detector = DetectorServer(config=config, name="detection")
    detector.start()

    image_client = ImageMessageQueueClient(
        channel=channel, exchange=rheed_exchange, routing_key="image"
    )

    await image_client.start()

    detection_mq = DetectionMessageQueue(
        detector=detector,
        channel=channel,
        exchange=rheed_exchange,
        routing_key="detection",
    )
    live_detection_mq = LiveDetectionMessageQueue(
        detector=detector,
        image_client=image_client,
        channel=channel,
        exchange=rheed_exchange,
        routing_key="",
        control_routing_key="live_detection_control",
        publish_routing_key="live_detection",
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
    parser.add_argument("--host", type=str, default="localhost")
    args= parser.parse_args()

    asyncio.run(main(args))
